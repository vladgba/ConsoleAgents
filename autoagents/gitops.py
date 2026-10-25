"""Git actions aa does itself, without an agent (no tokens): pushing finished work.

`run.push`: "off" (default) or "on_done" (push when a chat finishes). Optional `run.push_branches`:
only push these branches (e.g. ["dev"]); empty = any branch. Never forces, never prompts for credentials.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PushResult:
    ok: bool
    message: str
    pushed: int = 0  # commits pushed


def _git(root: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # fail instead of waiting
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def is_repo(root: Path) -> bool:
    try:
        r = _git(root, "rev-parse", "--is-inside-work-tree", timeout=10)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def push(root: Path, branches: list[str] | None = None) -> PushResult:
    """Push the current branch if it has commits the remote does not have."""
    try:
        if not is_repo(root):
            return PushResult(False, "not a git repository")
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not branch or branch == "HEAD":
            return PushResult(False, "detached HEAD: nothing pushed")
        if branches and branch not in branches:
            return PushResult(False, f"branch {branch} is not in run.push_branches ({', '.join(branches)}): not pushed")
        upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream.returncode == 0:
            ahead = _git(root, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
            count = int(ahead) if ahead.isdigit() else 0
            if count == 0:
                return PushResult(True, f"{branch} is already up to date with {upstream.stdout.strip()}")
            r = _git(root, "push", timeout=180)
            target = upstream.stdout.strip()
        else:
            remotes = _git(root, "remote").stdout.split()
            if not remotes:
                return PushResult(False, "no git remote configured: nothing pushed")
            remote = "origin" if "origin" in remotes else remotes[0]
            count = int(_git(root, "rev-list", "--count", "HEAD").stdout.strip() or 0)
            r = _git(root, "push", "-u", remote, branch, timeout=180)  # first push of this branch
            target = f"{remote}/{branch}"
        if r.returncode != 0:
            why = " | ".join(x for x in (r.stderr or r.stdout).strip().splitlines()[-3:] if x.strip())
            return PushResult(False, f"git push failed: {why or 'exit ' + str(r.returncode)}")
        return PushResult(True, f"pushed {count} commit(s) of {branch} to {target}", count)
    except subprocess.TimeoutExpired:
        return PushResult(False, "git push timed out (network, or waiting for credentials)")
    except (OSError, subprocess.SubprocessError) as exc:
        return PushResult(False, f"git push failed: {exc}")
