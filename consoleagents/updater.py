"""App updater and rollback engine for ConsoleAgents.

Manages updating the ConsoleAgents installation itself via Git from GitHub,
storing pre-update checkpoints in .autoagents/update_history.json, and
providing rollback/revert mechanisms to restore previous versions if needed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _git(root: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def get_app_root(start_path: Path | None = None) -> Path:
    """Find the root directory of the ConsoleAgents application repository."""
    if "CONSOLEAGENTS_APP_ROOT" in os.environ:
        return Path(os.environ["CONSOLEAGENTS_APP_ROOT"]).resolve()

    if start_path is not None:
        p = Path(start_path).resolve()
    else:
        # __file__ is in consoleagents/updater.py -> parent.parent is repo root
        p = Path(__file__).resolve().parent.parent

    for candidate in [p, *p.parents]:
        if (candidate / "ca.py").is_file() or (candidate / "aa.py").is_file():
            return candidate
        if (candidate / ".git").is_dir() and ((candidate / "consoleagents").is_dir() or (candidate / "autoagents").is_dir()):
            return candidate
    return p


def is_git_repo(root: Path) -> bool:
    try:
        r = _git(root, "rev-parse", "--is-inside-work-tree", timeout=10)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def get_current_branch(root: Path) -> str:
    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def get_current_commit(root: Path) -> str:
    r = _git(root, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def get_short_commit(root: Path, commit: str = "HEAD") -> str:
    r = _git(root, "rev-parse", "--short", commit)
    return r.stdout.strip() if r.returncode == 0 else commit[:7]


def get_commit_subject(root: Path, commit: str = "HEAD") -> str:
    r = _git(root, "log", "-1", "--format=%s", commit)
    return r.stdout.strip() if r.returncode == 0 else ""


def is_dirty(root: Path, include_untracked: bool = False) -> bool:
    """Returns True if there are uncommitted changes."""
    args = ["status", "--porcelain"] if include_untracked else ["status", "--porcelain", "-uno"]
    r = _git(root, *args)
    return bool(r.stdout.strip())


def get_remote_info(root: Path) -> tuple[str, str]:
    """Returns (remote_name, remote_url)."""
    remotes = _git(root, "remote").stdout.split()
    if not remotes:
        return "", ""
    remote = "origin" if "origin" in remotes else remotes[0]
    url_res = _git(root, "remote", "get-url", remote)
    url = url_res.stdout.strip() if url_res.returncode == 0 else ""
    return remote, url


@dataclass
class UpdateStatus:
    is_repo: bool
    branch: str
    current_commit: str
    short_commit: str
    current_subject: str
    remote: str
    remote_url: str
    behind: int = 0
    ahead: int = 0
    incoming_commits: list[dict[str, str]] = field(default_factory=list)
    dirty: bool = False
    error: str | None = None

    @property
    def has_update(self) -> bool:
        return self.behind > 0


@dataclass
class Checkpoint:
    id: str
    timestamp: float
    iso_time: str
    commit: str
    short_commit: str
    branch: str
    message: str
    action: str  # "pre_update", "pre_rollback", "manual"
    target: str | None = None


@dataclass
class UpdateResult:
    ok: bool
    message: str
    previous_commit: str = ""
    new_commit: str = ""
    checkpoint: Checkpoint | None = None


@dataclass
class RollbackResult:
    ok: bool
    message: str
    previous_commit: str = ""
    target_commit: str = ""
    checkpoint: Checkpoint | None = None


def get_history_file(root: Path) -> Path:
    return root / ".autoagents" / "update_history.json"


def load_checkpoints(root: Path) -> list[Checkpoint]:
    history_file = get_history_file(root)
    if not history_file.is_file():
        return []
    try:
        data = json.loads(history_file.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        checkpoints: list[Checkpoint] = []
        for item in data:
            if isinstance(item, dict) and "commit" in item:
                checkpoints.append(Checkpoint(
                    id=item.get("id", ""),
                    timestamp=item.get("timestamp", 0.0),
                    iso_time=item.get("iso_time", ""),
                    commit=item.get("commit", ""),
                    short_commit=item.get("short_commit", item.get("commit", "")[:7]),
                    branch=item.get("branch", ""),
                    message=item.get("message", ""),
                    action=item.get("action", "unknown"),
                    target=item.get("target"),
                ))
        return checkpoints
    except (OSError, json.JSONDecodeError):
        return []


def save_checkpoint(root: Path, action: str = "pre_update", target: str | None = None) -> Checkpoint:
    commit = get_current_commit(root)
    short = get_short_commit(root, commit)
    branch = get_current_branch(root)
    subject = get_commit_subject(root, commit)
    now = time.time()
    iso = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    cp_id = f"cp_{dt.datetime.fromtimestamp(now).strftime('%Y%m%d_%H%M%S')}_{short}"

    cp = Checkpoint(
        id=cp_id,
        timestamp=now,
        iso_time=iso,
        commit=commit,
        short_commit=short,
        branch=branch,
        message=subject,
        action=action,
        target=target,
    )

    history_file = get_history_file(root)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    existing = load_checkpoints(root)
    existing.append(cp)

    # Keep at most 50 checkpoints
    if len(existing) > 50:
        existing = existing[-50:]

    history_file.write_text(
        json.dumps([asdict(c) for c in existing], indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return cp


def check_update(root: Path, fetch: bool = True, timeout: float = 30) -> UpdateStatus:
    """Check if updates are available on GitHub remote."""
    if not is_git_repo(root):
        return UpdateStatus(
            is_repo=False,
            branch="",
            current_commit="",
            short_commit="",
            current_subject="",
            remote="",
            remote_url="",
            error="Not a git repository",
        )

    branch = get_current_branch(root)
    commit = get_current_commit(root)
    short = get_short_commit(root, commit)
    subject = get_commit_subject(root, commit)
    remote, url = get_remote_info(root)
    dirty = is_dirty(root)

    if not remote:
        return UpdateStatus(
            is_repo=True,
            branch=branch,
            current_commit=commit,
            short_commit=short,
            current_subject=subject,
            remote="",
            remote_url="",
            dirty=dirty,
            error="No git remote configured",
        )

    if not branch or branch == "HEAD":
        return UpdateStatus(
            is_repo=True,
            branch=branch or "HEAD (detached)",
            current_commit=commit,
            short_commit=short,
            current_subject=subject,
            remote=remote,
            remote_url=url,
            dirty=dirty,
            error="Detached HEAD state; switch to a branch before updating",
        )

    # Upstream branch or remote branch name
    upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip()
    target_ref = upstream if upstream else f"{remote}/{branch}"

    if fetch:
        # Fetch remote updates
        f_res = _git(root, "fetch", remote, branch, timeout=timeout)
        if f_res.returncode != 0:
            err = (f_res.stderr or f_res.stdout).strip().splitlines()
            err_msg = err[-1] if err else f"git fetch failed with exit code {f_res.returncode}"
            return UpdateStatus(
                is_repo=True,
                branch=branch,
                current_commit=commit,
                short_commit=short,
                current_subject=subject,
                remote=remote,
                remote_url=url,
                dirty=dirty,
                error=f"Failed to fetch updates from remote: {err_msg}",
            )

    # Count commits behind/ahead
    behind_res = _git(root, "rev-list", "--count", f"HEAD..{target_ref}")
    behind = int(behind_res.stdout.strip()) if behind_res.returncode == 0 and behind_res.stdout.strip().isdigit() else 0

    ahead_res = _git(root, "rev-list", "--count", f"{target_ref}..HEAD")
    ahead = int(ahead_res.stdout.strip()) if ahead_res.returncode == 0 and ahead_res.stdout.strip().isdigit() else 0

    incoming: list[dict[str, str]] = []
    if behind > 0:
        log_res = _git(root, "log", "--format=%H\t%h\t%s\t%an\t%cd", "--date=short", f"HEAD..{target_ref}")
        if log_res.returncode == 0:
            for line in log_res.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 5:
                    incoming.append({
                        "hash": parts[0],
                        "short": parts[1],
                        "subject": parts[2],
                        "author": parts[3],
                        "date": parts[4],
                    })

    return UpdateStatus(
        is_repo=True,
        branch=branch,
        current_commit=commit,
        short_commit=short,
        current_subject=subject,
        remote=remote,
        remote_url=url,
        behind=behind,
        ahead=ahead,
        incoming_commits=incoming,
        dirty=dirty,
    )


def apply_update(
    root: Path,
    target: str | None = None,
    allow_dirty: bool = False,
    create_checkpoint: bool = True,
    timeout: float = 60,
) -> UpdateResult:
    """Pull and apply updates from remote repository or to a target commit."""
    if not is_git_repo(root):
        return UpdateResult(False, "Not a git repository.")

    if not allow_dirty and is_dirty(root):
        return UpdateResult(
            False,
            "Working directory has uncommitted changes. Stash or commit them before updating (or pass --allow-dirty).",
        )

    branch = get_current_branch(root)
    prev_commit = get_current_commit(root)

    cp = None
    if create_checkpoint and prev_commit:
        cp = save_checkpoint(root, action="pre_update", target=target)

    if target:
        # Update/checkout specific target ref or commit
        r = _git(root, "checkout", target, timeout=timeout)
        if r.returncode != 0:
            why = (r.stderr or r.stdout).strip().splitlines()
            return UpdateResult(False, f"git checkout {target} failed: {why[-1] if why else r.returncode}", checkpoint=cp)
    else:
        remote, _ = get_remote_info(root)
        if not remote:
            return UpdateResult(False, "No git remote configured.", checkpoint=cp)
        # Pull with fast-forward only
        r = _git(root, "pull", "--ff-only", remote, branch, timeout=timeout)
        if r.returncode != 0:
            why = (r.stderr or r.stdout).strip().splitlines()
            err_msg = " | ".join(x for x in why[-2:] if x.strip())
            return UpdateResult(
                False,
                f"git pull --ff-only failed (diverged history or network issue): {err_msg}",
                checkpoint=cp,
            )

    new_commit = get_current_commit(root)
    if new_commit == prev_commit:
        return UpdateResult(True, "Already up to date.", previous_commit=prev_commit, new_commit=new_commit, checkpoint=cp)

    new_short = get_short_commit(root, new_commit)
    prev_short = get_short_commit(root, prev_commit)
    return UpdateResult(
        True,
        f"Updated successfully from {prev_short} to {new_short}.",
        previous_commit=prev_commit,
        new_commit=new_commit,
        checkpoint=cp,
    )


def apply_rollback(
    root: Path,
    target: str | None = None,
    checkpoint_id: str | None = None,
    allow_dirty: bool = False,
    timeout: float = 60,
) -> RollbackResult:
    """Revert/rollback changes to a previous checkpoint or commit."""
    if not is_git_repo(root):
        return RollbackResult(False, "Not a git repository.")

    if not allow_dirty and is_dirty(root):
        return RollbackResult(
            False,
            "Working directory has uncommitted changes. Stash or commit them before rollback (or pass --allow-dirty).",
        )

    checkpoints = load_checkpoints(root)
    target_commit: str | None = None
    selected_cp: Checkpoint | None = None

    if checkpoint_id:
        for c in reversed(checkpoints):
            if c.id == checkpoint_id or c.id.startswith(checkpoint_id) or c.short_commit == checkpoint_id:
                selected_cp = c
                target_commit = c.commit
                break
        if not target_commit:
            return RollbackResult(False, f"Checkpoint '{checkpoint_id}' not found.")
    elif target:
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(checkpoints):
                # 1 is most recent
                selected_cp = list(reversed(checkpoints))[idx - 1]
                target_commit = selected_cp.commit
            else:
                return RollbackResult(False, f"Invalid checkpoint index {idx} (available: 1..{len(checkpoints)}).")
        else:
            # Check if target is a valid commit or ref
            verify = _git(root, "rev-parse", "--verify", target)
            if verify.returncode == 0:
                target_commit = verify.stdout.strip()
            else:
                return RollbackResult(False, f"Invalid target commit or ref: '{target}'.")
    else:
        # Default to the most recent pre_update checkpoint
        for c in reversed(checkpoints):
            if c.action == "pre_update":
                selected_cp = c
                target_commit = c.commit
                break
        if not target_commit and checkpoints:
            selected_cp = checkpoints[-1]
            target_commit = selected_cp.commit

    if not target_commit:
        return RollbackResult(False, "No previous checkpoint found to roll back to.")

    prev_commit = get_current_commit(root)
    if prev_commit == target_commit:
        return RollbackResult(
            True,
            f"Already at target commit {get_short_commit(root, target_commit)}.",
            previous_commit=prev_commit,
            target_commit=target_commit,
        )

    # Save a safety checkpoint before rolling back
    safety_cp = save_checkpoint(root, action="pre_rollback", target=target_commit)

    # Hard reset to target commit
    r = _git(root, "reset", "--hard", target_commit, timeout=timeout)
    if r.returncode != 0:
        why = (r.stderr or r.stdout).strip().splitlines()
        return RollbackResult(
            False,
            f"git reset --hard failed: {why[-1] if why else r.returncode}",
            previous_commit=prev_commit,
            target_commit=target_commit,
        )

    prev_short = get_short_commit(root, prev_commit)
    target_short = get_short_commit(root, target_commit)
    msg = f"Rolled back from {prev_short} to {target_short}"
    if selected_cp and selected_cp.message:
        msg += f" ('{selected_cp.message}')"
    return RollbackResult(True, msg + ".", previous_commit=prev_commit, target_commit=target_commit, checkpoint=safety_cp)
