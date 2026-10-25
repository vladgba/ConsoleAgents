"""Which agents are running a turn right now, across every chat and project on this machine.

Each turn writes ~/.autoagents/busy/<agent>.<pid>.json while it runs. Other chats see it and pick another
agent (or wait), so one account is not driven from two chats at once. Files of dead processes are ignored
and cleaned up.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .chats import pid_alive
from .config import home_dir
from .jsonio import read_json, write_json


def _dir() -> Path:
    return home_dir() / "busy"


def mark(agent: str, project: str, chat: int | None, chat_id: str | None) -> Path:
    path = _dir() / f"{agent}.{os.getpid()}.json"
    write_json(path, {"agent": agent, "pid": os.getpid(), "project": project, "chat": chat,
                      "chat_id": chat_id, "started": time.time()})
    return path


def unmark(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def busy_agents(exclude_pid: int | None = None) -> dict[str, list[dict]]:
    """agent -> the turns it is running in OTHER processes (this one excluded by default)."""
    exclude_pid = os.getpid() if exclude_pid is None else exclude_pid
    out: dict[str, list[dict]] = {}
    d = _dir()
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        info = read_json(f, None)
        if not isinstance(info, dict):
            continue
        if not pid_alive(info.get("pid")):
            unmark(f)  # its process died without cleaning up
            continue
        if info.get("pid") == exclude_pid:
            continue
        out.setdefault(info.get("agent", f.name.split(".")[0]), []).append(info)
    return out


def where(infos: list[dict]) -> str:
    return ", ".join(f"{i.get('project') or '?'}#{i.get('chat') or '?'}" for i in infos)
