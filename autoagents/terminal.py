"""Optional pinned status and a single-line editor for ANSI interactive terminals.

Only the interactive shell owns the scroll region. Redirected output and dumb
terminals retain ordinary input()/print() behavior. No quota requests run here.
"""
from __future__ import annotations

import codecs
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager

from .ui import ANSI, agent_tag, cell_width, visible_width, wrap_ansi


def clip(text: str, width: int) -> str:
    """Keep SGR colors, but never let the header wrap into the output area."""
    result, used = "", 0
    for token in re.findall(r"\033\[[0-9;]*m|.", text):
        if ANSI.fullmatch(token):
            result += token
        elif used + cell_width(token) <= width:
            result += token
            used += cell_width(token)
        else:
            break
    return result + ("\033[0m" if "\033" in result else "")


def header_lines(text: str, width: int) -> list[str]:
    """Keep each vendor and its percentage together when the header spans rows."""
    lines, current = [], ""
    for field in text.split(" · "):
        candidate = current + (" · " if current else "") + field
        if current and visible_width(candidate) > width:
            lines.append(current)
            current = ""
        if visible_width(field) > width:
            lines.extend(wrap_ansi(field, width))
        else:
            current += (" · " if current else "") + field
    if current:
        lines.append(current)
    return lines or [""]


def local_status(session) -> str:
    from . import cli, procs
    from .project import ProjectState
    from .usage import UsageStore

    # Reload the ledger each time: background workers update it independently.
    agents = []
    try:
        cfg, store = cli.load_config(session.root), UsageStore()
        for name, acfg in cfg["agents"].items():
            if not acfg.get("enabled", True):
                continue
            usage, info = store.agent_usage(name, acfg), store.refresh_info(name)
            values, unknown = [], False
            for wu in usage.windows.values():
                text = ANSI.sub("", cli.usage_cell(wu, info, short=True, compact=True))
                match = re.match(r"(\d+)%", text)
                if match:
                    suffix = "*" if "stale" in text or "cached" in text else "~" if "est" in text else ""
                    values.append((int(match[1]), suffix))
                elif text != "disabled":
                    unknown = True
            if values:
                value, suffix = max(values)
                token = f"{value}%{suffix}" + ("?" if unknown else "")
            else:
                token = "?" if unknown else "-"
            agents.append(f"{agent_tag(name)} {token}")
    except Exception:
        agents = ["Usage ?"]

    try:
        records = []
        for root in session.watched_roots():
            base = ProjectState(root)
            for chat_id in base.chat_ids():
                records.extend(procs.load(base.for_chat(chat_id).dir))
        table = procs.table() if records else {}
        count = len({r["pid"] for r in records if procs.alive(r, table)})
    except Exception:
        count = "?"
    # Process count is first so it survives even at very small widths.
    return f"P:{count} " + " · ".join(agents)


def supported() -> bool:
    if os.environ.get("CONSOLEAGENTS_HEADER", "1").lower() in ("0", "off", "false"):
        return False
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "").lower() in ("dumb", "unknown"):
        return False
    if os.name != "nt":
        return bool(os.environ.get("TERM"))
    # Classic Windows consoles need virtual terminal output enabled explicitly.
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = msvcrt.get_osfhandle(sys.stdout.fileno())
        mode = wintypes.DWORD()
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                    and kernel.SetConsoleMode(handle, mode.value | 4))
    except (OSError, ValueError, AttributeError):
        return False


@contextmanager
def key_reader():
    """Yield a key reader; POSIX echo/canonical mode is restored on every exit."""
    if os.name == "nt":
        import msvcrt

        def read():
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                return {"K": "LEFT", "M": "RIGHT", "H": "UP", "P": "DOWN",
                        "G": "HOME", "O": "END", "S": "DELETE"}.get(msvcrt.getwch(), "IGNORE")
            if "\ud800" <= char <= "\udbff":
                # getwch exposes UTF-16 code units, including emoji surrogate pairs.
                char = (char + msvcrt.getwch()).encode('utf-16', errors='surrogatepass').decode('utf-16', errors='replace')
            return char
        yield read
        return

    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    decoder, pending = codecs.getincrementaldecoder("utf-8")(errors="replace"), deque()

    def char(timeout=None):
        while not pending:
            if not select.select([fd], [], [], timeout)[0]:
                return ""
            data = os.read(fd, 128)
            if not data:
                raise EOFError
            pending.extend(decoder.decode(data))
        return pending.popleft()

    def read():
        first = char()
        if first != "\033":
            return first
        seq = char(.03)
        if seq in ("[", "O"):
            for _ in range(12):
                more = char(.03)
                seq += more
                if not more or more.isalpha() or more == "~":
                    break
        return {"[D": "LEFT", "[C": "RIGHT", "[A": "UP", "[B": "DOWN",
                "[H": "HOME", "OH": "HOME", "[1~": "HOME", "[F": "END",
                "OF": "END", "[4~": "END", "[3~": "DELETE", "[200~": "PASTE",
                "[201~": "PASTE_END"}.get(seq, "IGNORE")

    try:
        tty.setcbreak(fd)  # keep signals (Ctrl+C), turn off kernel echo and line editing
        yield read
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


class _Output:
    def __init__(self, owner, stream):
        self.owner, self.stream = owner, stream

    def write(self, text):
        return self.owner.write(text, self.stream)

    def flush(self):
        with self.owner.lock:
            self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class PinnedStatus:
    def __init__(self, session):
        self.session = session
        self.lock = threading.RLock()
        self.halt = threading.Event()
        self.active = self.editing = False
        self.thread = None
        self.size = None
        self.drawn = None
        self.header_rows = 1
        self.status = "P:? Usage ?"
        self.history = []
        self.prompt, self.text, self.cursor = "", "", 0

    def __enter__(self):
        self.stdout, self.stderr = sys.stdout, sys.stderr
        if not supported():
            return self
        size = shutil.get_terminal_size()
        if size.columns < 16 or size.lines < 4:
            return self
        self.active = True
        try:
            self.status = local_status(self.session)
            self.draw(initial=True)
            sys.stdout = _Output(self, self.stdout)
            if self.stderr.isatty():
                sys.stderr = _Output(self, self.stderr)
            self.thread = threading.Thread(target=self.run, name="console-status", daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise
        return self

    def draw(self, initial=False):
        with self.lock:
            if not self.active:
                return
            size = shutil.get_terminal_size()
            if not initial and self.drawn == (size, self.status):
                return
            resize = size != self.size
            width, height = max(1, size.columns - 1), size.lines
            if height < 4 or width < 15:
                self.stdout.write("\0337\033[r\033[1;1H\033[2K\0338")
                self.size, self.drawn = size, (size, self.status)
                if self.editing:
                    self.render_input()
                self.stdout.flush()
                return
            lines = header_lines(self.status, width)[:min(3, height - 3)]
            resize = resize or len(lines) != self.header_rows
            previous_rows, self.header_rows = self.header_rows, len(lines)
            # Save/restore both cursor and rendition; resizing a region homes the cursor.
            out = "\0337"
            if resize:
                out += f"\033[{self.header_rows + 1};{height}r"
                self.size = size
            for row in range(max(previous_rows, self.header_rows)):
                out += f"\033[{row + 1};1H\033[2K\033[0m"
                if row < len(lines):
                    out += lines[row]
            out += "\0338"
            if initial:
                out += f"\033[{height};1H"
            self.stdout.write(out)
            self.drawn = size, self.status
            if self.editing and resize:
                self.render_input()
            self.stdout.flush()

    def run(self):
        next_sample = time.monotonic() + 3
        while not self.halt.wait(.25):
            try:
                if time.monotonic() >= next_sample:
                    value = local_status(self.session)
                    with self.lock:
                        self.status = value
                    next_sample = time.monotonic() + 3
                self.draw()
            except (OSError, ValueError):
                return  # terminal was closed; main thread still owns cleanup

    def write(self, text, stream):
        with self.lock:
            if self.editing and text:
                self.stdout.write("\r\033[2K")
                stream.write(text)
                if not text.endswith("\n"):
                    self.stdout.write("\n")
                self.render_input()
                self.stdout.flush()
                return len(text)
            return stream.write(text)

    def render_input(self):
        width = max(1, self.size.columns - 1)
        prompt = ANSI.sub("", self.prompt).replace("\n", "")
        prompt = clip(prompt, width // 2)
        room = max(1, width - visible_width(prompt))
        left = 0
        while left < self.cursor and visible_width(self.text[left:self.cursor]) >= room:
            left += 1
        shown = clip(self.text[left:], room)
        position = visible_width(prompt) + visible_width(self.text[left:self.cursor])
        self.stdout.write("\r\033[2K" + prompt + shown + "\r"
                          + (f"\033[{position}C" if position else ""))

    def input(self, prompt):
        if not self.active:
            return input(prompt)
        with key_reader() as read:
            with self.lock:
                self.prompt, self.text, self.cursor = prompt, "", 0
                self.editing = True
                self.stdout.write("\n\033[?2004h")
                self.render_input()
                self.stdout.flush()
            index, saved, paste = len(self.history), "", False
            try:
                while True:
                    key = read()
                    with self.lock:
                        if key == "PASTE":
                            paste = True
                        elif key == "PASTE_END":
                            paste = False
                        elif key in ("\r", "\n") and not paste:
                            if self.text and (not self.history or self.history[-1] != self.text):
                                self.history.append(self.text)
                                self.history = self.history[-100:]
                            return self.text
                        elif key == "\x03":
                            raise KeyboardInterrupt
                        elif key in ("\x04", "\x1a") and not self.text:
                            raise EOFError
                        elif key == "LEFT":
                            self.cursor = max(0, self.cursor - 1)
                        elif key == "RIGHT":
                            self.cursor = min(len(self.text), self.cursor + 1)
                        elif key == "HOME" or key == "\x01":
                            self.cursor = 0
                        elif key == "END" or key == "\x05":
                            self.cursor = len(self.text)
                        elif key in ("\b", "\x7f") and self.cursor:
                            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
                            self.cursor -= 1
                        elif key == "DELETE" or key == "\x04":
                            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
                        elif key == "\x15":
                            self.text, self.cursor = "", 0
                        elif key in ("UP", "DOWN"):
                            if index == len(self.history):
                                saved = self.text
                            index = max(0, min(len(self.history), index + (-1 if key == "UP" else 1)))
                            self.text = self.history[index] if index < len(self.history) else saved
                            self.cursor = len(self.text)
                        elif len(key) == 1 and (key.isprintable() or paste and key in ("\r", "\n", "\t")):
                            key = " " if key.isspace() else key
                            self.text = self.text[:self.cursor] + key + self.text[self.cursor:]
                            self.cursor += len(key)
                        self.render_input()
                        self.stdout.flush()
            finally:
                with self.lock:
                    self.editing = False
                    self.stdout.write("\033[?2004l\033[0m\n")
                    self.stdout.flush()

    def close(self):
        self.halt.set()
        with self.lock:
            if self.active:
                self.active = False
                try:
                    clear = "".join(f"\033[{row + 1};1H\033[2K" for row in range(self.header_rows))
                    self.stdout.write("\0337\033[r" + clear + "\0338\033[?2004l\033[0m")
                    self.stdout.flush()
                except (OSError, ValueError):
                    pass  # a closed terminal must not prevent stream restoration
                finally:
                    sys.stdout, sys.stderr = self.stdout, self.stderr
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)

    def __exit__(self, *_):
        self.close()
