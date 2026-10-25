"""Chats that run in the background, like agent tabs: start one, leave it, come back to it.

Each running chat is a detached worker process (`aa ... worker`) whose console output goes to
chats/<id>/live.log. runner.json holds its pid. Messages you type while it works are appended to
chats/<id>/inbox.jsonl; the worker reads them before the next turn (and before it would stop).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .jsonio import append_jsonl, read_json, read_jsonl, write_json
from .project import ProjectState

PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)


# -- processes ------------------------------------------------------------------------------------
def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)  # signal 0 only checks (on Windows os.kill would terminate it)
        from .procs import process_state
        if process_state(int(pid)) in {"Z", "X", "x"}:
            return False
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def runner(state: ProjectState) -> dict | None:
    """The live worker of this chat, or None."""
    info = read_json(state.dir / "runner.json", None)
    if isinstance(info, dict) and pid_alive(info.get("pid")):
        return info
    return None


def is_running(state: ProjectState) -> bool:
    return runner(state) is not None


def mark_worker(state: ProjectState) -> None:
    write_json(state.dir / "runner.json", {"pid": os.getpid(), "started": time.time()})


def unmark_worker(state: ProjectState) -> None:
    info = read_json(state.dir / "runner.json", None)
    if isinstance(info, dict) and info.get("pid") == os.getpid():
        try:
            (state.dir / "runner.json").unlink()
        except OSError:
            pass


def spawn(state: ProjectState, worker_args: list[str]) -> int:
    """Start this chat's worker in the background. Returns its pid."""
    cmd = [sys.executable, "-m", "consoleagents", "-C", str(state.root), "--chat", state.chat_id,
           "worker", *worker_args]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "AA_COLOR": "1",
           "PYTHONPATH": PACKAGE_PARENT + os.pathsep + os.environ.get("PYTHONPATH", "")}
    kw: dict = {}
    if os.name == "nt":
        # Own process group (Ctrl+C in the shell does not reach it), no console window.
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    state.dir.mkdir(parents=True, exist_ok=True)
    with open(state.live_log, "a", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=str(state.root), env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, **kw)
    write_json(state.dir / "runner.json", {"pid": proc.pid, "started": time.time()})
    return proc.pid


def stop(state: ProjectState) -> bool:
    """Stop the chat's worker and the agent CLI it runs. Returns False if it was not running."""
    info = runner(state)
    if not info:
        return False
    pid = int(info["pid"])
    # Which agent was mid-turn (its busy marker), so the next one gets a proper handover.
    working = None
    from .config import home_dir
    for f in (home_dir() / "busy").glob(f"*.{pid}.json"):
        working = (read_json(f, {}) or {}).get("agent") or working
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    else:
        import signal
        from .procs import tree
        # Providers start CLIs in separate sessions; the worker's group alone
        # does not include those agents (or their helpers).
        for child in reversed(tree(pid)):
            if child.pid != pid:
                try:
                    os.kill(child.pid, signal.SIGTERM)
                except OSError:
                    pass
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            # Stand-in or externally started workers may not lead a process group.
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    for _ in range(50):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    try:
        (state.dir / "runner.json").unlink()
    except OSError:
        pass
    task = state.load_task()
    if task:
        task["status"] = "stopped"
        agent = working or task.get("last_agent")
        if working:
            # The turn never finished, so its output is not in the transcript yet: keep what it showed.
            partial = current_turn_output(state.live_log)
            if partial:
                state.add_transcript("agent", working, "[turn stopped before it finished; its live output]\n"
                                    + partial, task.get("turns", 0))
            task["last_agent"] = working
        if agent:
            task["handover"] = {"from": agent, "reason": "the chat was stopped mid-turn (stopped by the user, or "
                                "it hung); its output so far is in the history. Check the working tree and "
                                "git log: part of the work may be done or committed"}
        state.save_task(task)
        state.log_step("stop", working, "stopped by the user" + (f" while {working} was working" if working else ""))
    with open(state.live_log, "a", encoding="utf-8") as fh:
        fh.write("\n■ Stopped by you. Continue it with a message, or /continue.\n")
    return True


# -- inbox ------------------------------------------------------------------------------------------
def send(state: ProjectState, text: str) -> None:
    append_jsonl(state.dir / "inbox.jsonl", {"ts": time.time(), "text": text})


def has_inbox(state: ProjectState) -> bool:
    path = state.dir / "inbox.jsonl"
    return path.exists() and path.stat().st_size > 0


def drain(state: ProjectState) -> list[str]:
    """Take every queued message (atomically: a message sent meanwhile goes to a new inbox)."""
    path = state.dir / "inbox.jsonl"
    if not path.exists():
        return []
    tmp = path.with_name(f"inbox.{os.getpid()}.{time.time_ns()}.taking")
    for _ in range(20):
        try:
            os.replace(path, tmp)
            break
        except FileNotFoundError:
            return []
        except PermissionError:  # Windows: a sender is appending right now
            time.sleep(0.05)
    else:
        return []
    msgs = [str(r.get("text")) for r in read_jsonl(tmp) if str(r.get("text") or "").strip()]
    try:
        tmp.unlink()
    except OSError:
        pass
    return msgs


# -- overview ---------------------------------------------------------------------------------------
@dataclass
class ChatInfo:
    root: Path
    chat_id: str
    number: int
    text: str
    status: str  # working | waiting | needs input | done | stopped | new
    agent: str | None
    question: str | None
    updated: float
    running: bool


def chat_info(state: ProjectState) -> ChatInfo | None:
    task = state.load_task()
    if not task:
        return None
    alive = is_running(state)
    raw = task.get("status")
    if alive:
        status = "waiting" if raw == "waiting" else "working"
    else:
        status = {"blocked": "needs input", "done": "done", "waiting": "waiting", "new": "new"}.get(raw, "stopped")
    try:
        updated = max(task.get("updated") or 0, state.live_log.stat().st_mtime if state.live_log.exists() else 0)
    except OSError:
        updated = task.get("updated") or 0
    return ChatInfo(state.root, state.chat_id, state.number() or 0, task.get("text", ""), status,
                    task.get("last_agent"), task.get("question"), updated, alive)


def list_chats(root: Path) -> list[ChatInfo]:
    base = ProjectState(root)
    out = []
    for cid in base.chat_ids():
        info = chat_info(base.for_chat(cid))
        if info:
            out.append(info)
    return out


def current_turn_output(path: Path, max_chars: int = 20000) -> str:
    """The live-log text of the last turn (after its '━━ Turn' header), without colour codes."""
    import re
    text = "\n".join(tail(path, 400))
    cut = text.rfind("━━ Turn")
    text = text[cut:] if cut >= 0 else text
    text = re.sub(r"\033\[[0-9;]*m", "", text).strip()
    return text[-max_chars:]


def tail(path: Path, max_lines: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 200_000))
            lines = fh.read().splitlines()
    except OSError:
        return []
    return lines[-max_lines:]
