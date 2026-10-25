"""Shut down when finished: /swf arms it, /qqq disarms it (and cancels a shutdown already scheduled).

When armed and the last working chat (in any project) ends, aa:
  1. locks the screen of every Android device connected through adb (agents testing apps leave it on)
  2. powers off this PC after a grace period (run.shutdown_grace_s, default 60 s), so you can still stop it
     with /qqq (or `shutdown /a` on Windows)
"Ends" means done, stopped, or waiting for your answer; a chat waiting for usage limits is still working.
The armed state lives in ~/.autoagents/shutdown.json, so it works even after you close the shell: the
background process of the last chat triggers it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import home_dir
from .jsonio import file_lock, read_json, write_json


def _path() -> Path:
    return home_dir() / "shutdown.json"


def state() -> dict:
    data = read_json(_path(), {})
    return data if isinstance(data, dict) else {}


def armed() -> bool:
    return bool(state().get("armed"))


def arm(work_now: bool = False) -> None:
    """seen_work: a chat has been working since it was armed (only then does "all finished" mean something)."""
    write_json(_path(), {"armed": True, "armed_at": time.time(), "triggered_at": None, "seen_work": work_now})


def working_chats() -> int:
    from .chats import list_chats
    from .registry import Registry
    total = 0
    for info in Registry().all().values():
        root = Path(info["path"])
        if root.is_dir():
            total += sum(1 for c in list_chats(root) if c.running)
    return total


def disarm() -> bool:
    """Returns True when a scheduled shutdown was cancelled."""
    st = state()
    write_json(_path(), {"armed": False, "armed_at": None, "triggered_at": None})
    if st.get("triggered_at"):
        return cancel_shutdown()
    return False


def _run(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def android_devices() -> list[str]:
    if not shutil.which("adb"):
        return []
    r = _run(["adb", "devices"])
    if not r:
        return []
    return [ln.split()[0] for ln in r.stdout.splitlines()[1:] if ln.strip().endswith("device")]


def _screen_on(serial: str) -> bool | None:
    r = _run(["adb", "-s", serial, "shell", "dumpsys", "power"])
    if not r or r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if "mWakefulness=" in line:
            return "Awake" in line
    return None


def lock_android() -> list[str]:
    """Lock the screen of every Android device connected through adb (USB or Wi-Fi)."""
    locked = []
    for serial in android_devices():
        on = _screen_on(serial)
        if on is False:
            locked.append(serial)  # screen already off: the device's own lock is in place
            continue
        # POWER (26) locks at once on most phones ("power button instantly locks"), but it toggles,
        # so it is only sent to an awake screen. SLEEP (223) is the fallback: it never wakes a screen.
        key = "26" if on else "223"
        r = _run(["adb", "-s", serial, "shell", "input", "keyevent", key])
        if r and r.returncode == 0 and key == "26":
            time.sleep(1.0)
            if _screen_on(serial):
                _run(["adb", "-s", serial, "shell", "input", "keyevent", "223"])
        if r and r.returncode == 0:
            locked.append(serial)
    return locked


def schedule_shutdown(grace_s: int) -> bool:
    msg = "AutoAgents: all tasks are finished. Run /qqq in aa (or shutdown /a) to cancel."
    if os.name == "nt":
        r = _run(["shutdown", "/s", "/t", str(max(0, grace_s)), "/c", msg])
    elif sys.platform == "darwin":
        r = _run(["osascript", "-e", f'delay {max(0, grace_s)}', "-e", 'tell app "System Events" to shut down'])
    else:
        r = _run(["shutdown", "-h", f"+{max(1, round(grace_s / 60))}", msg])
    return bool(r and r.returncode == 0)


def cancel_shutdown() -> bool:
    if os.name == "nt":
        r = _run(["shutdown", "/a"])
    elif sys.platform == "darwin":
        return False
    else:
        r = _run(["shutdown", "-c"])
    return bool(r and r.returncode == 0)


def maybe_finish(cfg_run: dict, log=print, chat_ended: bool = False, force: bool = False) -> bool:
    """Triggers once when armed, some chat has worked since, and no chat is working any more.

    Called when a chat's process ends (chat_ended), and every few seconds by an open shell, which also
    covers chats whose process runs older code without this check. force = do it now (the user said so)."""
    if not armed():
        return False
    with file_lock(_path()):  # two chats ending at the same moment must not both trigger
        st = state()
        if not st.get("armed") or st.get("triggered_at"):
            return False
        if working_chats():
            if not st.get("seen_work"):
                st["seen_work"] = True
                write_json(_path(), st)
            return False  # something still works
        if not (st.get("seen_work") or chat_ended or force):
            return False  # armed while idle: wait for the next chat to run and finish
        st["triggered_at"] = time.time()
        write_json(_path(), st)
    grace = int(cfg_run.get("shutdown_grace_s", 60))
    locked = lock_android() if cfg_run.get("finish_lock_android", True) else []
    if locked:
        log(f"⏻ All tasks finished: locked {len(locked)} Android device(s) ({', '.join(locked)}).")
    if cfg_run.get("finish_shutdown", True):
        ok = schedule_shutdown(grace)
        log(f"⏻ All tasks finished: this PC shuts down in {grace} s. Cancel: /qqq in aa, or `shutdown /a`."
            if ok else "⏻ All tasks finished, but the shutdown could not be scheduled (no permission?).")
    return True
