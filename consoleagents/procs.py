"""Processes started by agents: found while a turn runs, listed and killed on request.

An agent CLI often starts long-lived helpers (a Gradle daemon, an adb server, a dev server, a test
watcher). They outlive the agent, keep ports and memory busy, and used to keep a turn's output pipe
open. While a turn runs, its process tree is sampled every few seconds and every process found is
recorded in chats/<id>/procs.json with its start time, so it can be found (and safely killed: the
start time guards against a reused pid) after the agent itself has exited.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

IGNORED = {"conhost.exe"}  # console hosts Windows adds to every console process


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    created: float | None
    pgid: int | None = None  # POSIX group survives reparenting after the CLI exits


# -- process table ---------------------------------------------------------------------------------
def _table_windows() -> dict[int, Proc]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    out: dict[int, Proc] = {}
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            out[pid] = Proc(pid, int(entry.th32ParentProcessID), entry.szExeFile, None)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _created_windows(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return ticks / 1e7 - 11644473600  # FILETIME (1601, 100 ns) -> Unix time
    finally:
        k32.CloseHandle(h)


def _cmdline_windows(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = wintypes.HANDLE
    query = ntdll.NtQueryInformationProcess
    query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    query.restype = wintypes.LONG
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.ULONG(0)
        query(h, 60, None, 0, ctypes.byref(size))  # 60 = ProcessCommandLineInformation (Windows 8.1+)
        if not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if query(h, 60, buf, size, ctypes.byref(size)) != 0:
            return ""
        us = UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
    finally:
        k32.CloseHandle(h)


def cmdline(pid: int) -> str:
    """A process's command line, fast enough to ask for every process a turn starts ("" if unknown)."""
    try:
        if os.name == "nt":
            return _cmdline_windows(pid)
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace").strip()
        except OSError:
            pass  # non-Linux POSIX systems need ps
        return subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


_WRAPPED = re.compile(r"\beval (.*?)(?: < /dev/null)?(?: && pwd -P >\| \S+)?\"?\s*$", re.S)


def describe(cmd: str, limit: int = 110) -> str:
    """What a command line does, in a few words: the program without its folder and, for the shell
    wrapper Claude Code puts around each command (source snapshot && ... && eval '<cmd>'), only <cmd>."""
    cmd = " ".join(cmd.split())
    if not cmd:
        return ""
    m = _WRAPPED.search(cmd)
    if m:
        arg = m.group(1).replace('\\"', '"')
        try:
            parts = shlex.split(arg)
            cmd = parts[0] if parts else arg
        except ValueError:
            cmd = arg
        cmd = " ".join(cmd.split())
    else:
        # "C:\path\prog.exe" args  ->  prog args
        m = re.match(r'^(?:"([^"]+)"|(\S+))\s*(.*)$', cmd)
        if m:
            exe = re.split(r"[\\/]+", m.group(1) or m.group(2))[-1]
            exe = re.sub(r"\.(exe|cmd|bat)$", "", exe, flags=re.I)
            rest = m.group(3)
            script = _SHELL_SCRIPT.match(rest) if exe.lower() in SHELLS else None
            cmd = _unquote(script.group(1)) if script else f"{exe} {rest}".strip()
    return cmd if len(cmd) <= limit else cmd[:limit - 1] + "…"


# A shell that only runs one script: pwsh -NoProfile -Command '<script>', cmd /c <script>, bash -lc "<script>".
SHELLS = {"pwsh", "powershell", "cmd", "bash", "sh", "zsh"}
_SHELL_SCRIPT = re.compile(r"(?:[-/]\w+(?: (?![-/])\S+)? )*?(?:-command|-c|-lc|/c) (.+)$", re.I | re.S)


def _unquote(script: str) -> str:
    """'adb devices' or "a"'b' (one quoted word) -> its text; anything else is left as it is."""
    try:
        parts = shlex.split(script)
    except ValueError:  # quoting shlex cannot read: just drop the outer quotes
        return script[1:-1] if len(script) > 1 and script[0] == script[-1] and script[0] in "'\"" else script
    return " ".join(parts[0].split()) if len(parts) == 1 else script


def _read_stat(pid: int) -> tuple[str, list[str]]:
    # comm is parenthesized and can itself contain spaces and closing parentheses.
    text = Path(f"/proc/{pid}/stat").read_text()
    end = text.rindex(")")
    return text[text.index("(") + 1:end], text[end + 2:].split()


def process_state(pid: int) -> str | None:
    """Linux/Android process state, or None if procfs is unavailable/inaccessible."""
    try:
        return _read_stat(pid)[1][0]
    except (OSError, ValueError, IndexError):
        return None


def _table_procfs() -> dict[int, Proc]:
    out: dict[int, Proc] = {}
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        # Android blocks /proc/uptime even for apps which can read their own stat.
        uptime = (time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, "CLOCK_BOOTTIME")
                  else float(Path("/proc/uptime").read_text().split()[0]))
        boot = time.time() - uptime
        entries = list(Path("/proc").iterdir())
    except (OSError, ValueError, IndexError):
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            name, fields = _read_stat(pid)
            if fields[0] in {"Z", "X", "x"}:
                continue  # exited processes must not remain in the live-process list
            out[pid] = Proc(pid, int(fields[1]), name, boot + int(fields[19]) / ticks, int(fields[2]))
        except (OSError, ValueError, IndexError):
            continue  # another app's process, or one which exited during the scan
    return out


def _table_posix() -> dict[int, Proc]:
    if Path("/proc/self/stat").is_file():
        tab = _table_procfs()
        if tab:
            return tab
    out: dict[int, Proc] = {}
    try:
        text = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,comm="], capture_output=True, text=True,
                              timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    now = time.time()
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit():
            try:
                out[int(parts[0])] = Proc(int(parts[0]), int(parts[1]), os.path.basename(parts[3]),
                                          now - float(parts[2]))
            except ValueError:
                continue
    return out


def table() -> dict[int, Proc]:
    return _table_windows() if os.name == "nt" else _table_posix()


def created(pid: int, tab: dict[int, Proc] | None = None) -> float | None:
    if os.name == "nt":
        return _created_windows(pid)
    p = (tab or table()).get(pid)
    return p.created if p else None


def tree(root_pid: int, tab: dict[int, Proc] | None = None) -> list[Proc]:
    """root and all its descendants (a child must be younger than its parent: guards pid reuse)."""
    tab = tab if tab is not None else table()
    if root_pid not in tab:
        return []
    found = [tab[root_pid]]
    found[0].created = found[0].created or created(root_pid, tab)
    i = 0
    while i < len(found):
        parent = found[i]
        for p in tab.values():
            if p.ppid == parent.pid and p.pid != parent.pid and p.name.lower() not in IGNORED \
                    and all(p.pid != f.pid for f in found):
                p.created = p.created or created(p.pid, tab)
                if parent.created and p.created and p.created + 1 < parent.created:
                    continue  # an old process whose parent pid was reused
                found.append(p)
        i += 1
    return found


def orphans_of(pids: set[int], since: float, tab: dict[int, Proc] | None = None) -> list[Proc]:
    """Processes (and their children) whose parent is one of `pids`, started after `since`.
    Finds what an agent started even after the agent itself has exited. On Linux,
    orphaned children lose their original ppid but retain the CLI's process group."""
    tab = tab if tab is not None else table()
    found: list[Proc] = []
    parents = set(pids)
    changed = True
    while changed:
        changed = False
        for p in tab.values():
            if (p.ppid in parents or p.pgid in pids) and p.pid not in parents and p.name.lower() not in IGNORED:
                p.created = p.created or created(p.pid, tab)
                if p.created and p.created + 1 < since:
                    continue  # older than the turn: its parent pid was merely reused
                found.append(p)
                parents.add(p.pid)
                changed = True
    return found


def alive(rec: dict, tab: dict[int, Proc] | None = None) -> bool:
    """Is the recorded process still the same running process?"""
    tab = tab if tab is not None else table()
    if rec["pid"] not in tab:
        return False
    if rec.get("created"):
        now_created = created(rec["pid"], tab)
        if now_created and abs(now_created - rec["created"]) > 2:
            return False  # the pid now belongs to another process
    return True


def kill(rec: dict) -> bool:
    """Kill a recorded process and its children, only if it is still the same process."""
    if not alive(rec):
        return False
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(rec["pid"])], capture_output=True,
                           encoding="utf-8", errors="replace")  # its messages use the console code page
        return r.returncode == 0
    import signal
    try:
        os.kill(rec["pid"], signal.SIGTERM)
        return True
    except OSError:
        return False


def details(pids: list[int]) -> dict[int, dict]:
    """Command line and memory for display (one slower query, only when you list them)."""
    if not pids:
        return {}
    out: dict[int, dict] = {}
    if os.name == "nt":
        ids = ",".join(str(p) for p in pids)
        ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance Win32_Process | Where-Object { @(" + ids + ") -contains $_.ProcessId } | "
              "Select-Object ProcessId, CommandLine, WorkingSetSize | ConvertTo-Json -Compress")
        try:
            text = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                                  encoding="utf-8", errors="replace", timeout=30).stdout.strip()
            data = json.loads(text) if text else []
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return out
        for d in data if isinstance(data, list) else [data]:
            out[int(d["ProcessId"])] = {"cmd": d.get("CommandLine") or "", "mem": int(d.get("WorkingSetSize") or 0)}
        return out
    for pid in pids:
        try:
            r = subprocess.run(["ps", "-o", "rss=,args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
            rss, _, args = r.stdout.strip().partition(" ")
            out[pid] = {"cmd": args.strip(), "mem": int(rss) * 1024 if rss.isdigit() else 0}
        except (OSError, subprocess.SubprocessError):
            pass
    return out


# -- what a chat's agents started (chats/<id>/procs.json) ------------------------------------------
def load(chat_dir) -> list[dict]:
    from .jsonio import read_json
    data = read_json(chat_dir / "procs.json", {})
    return list(data.get("procs") or []) if isinstance(data, dict) else []


def record(chat_dir, found: list[Proc], agent: str, turn: int, cli_pid: int | None = None) -> list[dict]:
    """Add newly seen processes (cli_pid = the agent CLI itself). Returns the new records."""
    from .jsonio import write_json
    known = load(chat_dir)
    seen = {(r["pid"], round(r.get("created") or 0)) for r in known}
    new = []
    for p in found:
        key = (p.pid, round(p.created or 0))
        if key in seen:
            continue
        rec = {"pid": p.pid, "name": p.name, "created": p.created, "agent": agent, "turn": turn,
               "agent_cli": p.pid == cli_pid, "seen": time.time(),
               "cmd": "" if p.pid == cli_pid else cmdline(p.pid)}
        known.append(rec)
        new.append(rec)
    if new:
        write_json(chat_dir / "procs.json", {"procs": known[-500:]})
    return new


def running(chat_dir, include_agent_cli: bool = True) -> list[dict]:
    tab = table()
    return [r for r in load(chat_dir) if (include_agent_cli or not r.get("agent_cli")) and alive(r, tab)]
