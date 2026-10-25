"""Console output: colours (ANSI, honouring NO_COLOR), progress lines, tables, countdowns."""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import unicodedata
import sys
import time

if os.name == "nt":
    os.system("")  # turns on ANSI escape processing in the classic Windows console
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# AA_COLOR=1: a background chat writes colours into its log, for when you open it.
_COLOR = (sys.stdout.isatty() or os.environ.get("AA_COLOR") == "1") and not os.environ.get("NO_COLOR")
_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
          "magenta": "35", "cyan": "36", "gray": "90", "orange": "38;5;208"}
AGENT_COLORS = {"claude": "orange", "codex": "gray", "antigravity": "blue"}
AGENT_NAMES = {"claude": "Claude", "codex": "GPT", "antigravity": "Gemini"}


def c(text: str, *styles: str) -> str:
    if not _COLOR or not styles:
        return str(text)
    return "\033[" + ";".join(_CODES[s] for s in styles if s in _CODES) + "m" + str(text) + "\033[0m"


def agent_tag(name: str) -> str:
    return c(AGENT_NAMES.get(name, name), "bold", AGENT_COLORS.get(name, "cyan"))


ANSI = re.compile(r"\033\[[0-9;]*m")


def cell_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in ("Cc", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def visible_width(text: str) -> int:
    return sum(cell_width(ch) for ch in ANSI.sub("", str(text)))


def wrap_ansi(text: str, width: int) -> list[str]:
    """Wrap terminal cells without splitting SGR codes or leaking styles between lines."""
    width = max(1, width)
    result, line, styles, used = [], "", "", 0
    # Process explicit newlines too, including trailing empty lines.
    tokens = re.findall(r"\033\[[0-9;]*m|\n|[^\S\n]+|[^\s\033]+|.", str(text).expandtabs(4))
    for token in tokens:
        if ANSI.fullmatch(token):
            line += token
            styles = "" if token in ("\033[0m", "\033[m") else styles + token
            continue
        size = visible_width(token)
        if token == "\n" or (used and size <= width and used + size > width):
            result.append(line + ("\033[0m" if styles else ""))
            line, used = styles, 0
            if token == "\n":
                continue
        for char in token:
            size = cell_width(char)
            if used and used + size > width:
                result.append(line + ("\033[0m" if styles else ""))
                line, used = styles, 0
            # A double-width glyph cannot fit in a one-cell terminal.
            line += char if size <= width else "?"
            used += min(size, width)
    result.append(line + ("\033[0m" if styles else ""))
    return result


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    d = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    return d.strftime("%H:%M") if d.date() == today else d.strftime("%a %d %b %H:%M")


def fmt_in(ts: float | None) -> str:
    if not ts:
        return "-"
    left = int(ts - time.time())
    if left <= 0:
        return "now"
    h, rem = divmod(left, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def bar(util: float, width: int = 12) -> str:
    util = max(0.0, util)
    filled = min(width, int(round(min(util, 1.0) * width)))
    color = "green" if util < 0.6 else "yellow" if util < 0.85 else "red"
    return c("█" * filled, color) + c("░" * (width - filled), "gray") + f" {util:4.0%}"


class UI:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.in_shell = False  # set by the interactive shell, so hints show shell syntax

    @property
    def width(self) -> int:
        # Reserve the last column to avoid a terminal's automatic extra line wrap.
        return max(1, shutil.get_terminal_size(fallback=(120, 24)).columns - 1)

    @property
    def narrow(self) -> bool:
        return self.width < 80

    def line(self, text: str = "") -> None:
        lines = wrap_ansi(str(text), self.width) if self.narrow else [str(text)]
        print("\n".join(lines), flush=True)

    def info(self, text: str) -> None:
        self.line(c("• ", "cyan") + text)

    def ok(self, text: str) -> None:
        self.line(c("✔ ", "green") + text)

    def warn(self, text: str) -> None:
        self.line(c("! ", "yellow") + c(text, "yellow"))

    def error(self, text: str) -> None:
        self.line(c("✖ ", "red") + c(text, "red"))

    def header(self, text: str) -> None:
        self.line()
        self.line(c(f"━━ {text} ", "bold", "cyan") + c("━" * max(0, min(60, self.width - 4) - visible_width(text)), "cyan"))

    def block(self, title: str, body: str) -> None:
        self.line(c(title, "bold"))
        for ln in (body or "").splitlines():
            self.line("  " + ln)

    def progress_printer(self, agent: str):
        start = time.time()

        def emit(msg: str) -> None:
            if self.quiet:
                return
            el = int(time.time() - start)
            self.line(c(f"  [{AGENT_NAMES.get(agent, agent)} {el // 60:02d}:{el % 60:02d}] ", AGENT_COLORS.get(agent, "cyan")) + msg)

        return emit

    def table(self, headers: list[str], rows: list[list[str]],
              compact: list[int] | None = None) -> None:
        if self.narrow and compact is not None:
            headers = [headers[i] for i in compact]
            rows = [[r[i] for i in compact] for r in rows]
        if self.narrow:
            widths = [max([visible_width(h)] + [visible_width(r[i]) for r in rows])
                      for i, h in enumerate(headers)]
            if sum(widths) + 2 * (len(headers) - 1) > self.width:
                # Cards keep long tasks/commands readable instead of squeezing every column.
                for row in rows:
                    self.line(" · ".join(f"{h}: {v}" if h else str(v)
                                         for h, v in zip(headers, row)))
                    self.line()
                return
        widths = [max(visible_width(h), *(visible_width(r[i]) for r in rows)) if rows else visible_width(h)
                  for i, h in enumerate(headers)]

        def fmt(cells):
            return "  ".join(str(cell) + " " * (w - visible_width(cell)) for cell, w in zip(cells, widths))

        self.line(c(fmt(headers), "bold"))
        for r in rows:
            self.line(fmt(r))

    def countdown(self, until: float, reason: str, check_every: float = 300) -> None:
        """Sleep until `until` (or at most check_every seconds), showing a live countdown.
        Ctrl+C propagates to the caller."""
        stop = min(until, time.time() + check_every)
        if not sys.stdout.isatty():  # a background chat: one line in its log, no live redraw
            self.line(c(f"  ⏳ {reason} - next agent available in {fmt_in(until)} (at {fmt_time(until)}).", "yellow"))
            time.sleep(max(0.0, stop - time.time()))
            return
        while True:
            left = until - time.time()
            if time.time() >= stop:
                break
            msg = f"  ⏳ {reason} - next agent available in {fmt_in(until)} (at {fmt_time(until)}). Ctrl+C to stop."
            sys.stdout.write("\r" + c(msg, "yellow") + " " * 4)
            sys.stdout.flush()
            time.sleep(min(5.0, max(0.2, left)))
        sys.stdout.write("\r" + " " * 110 + "\r")
        sys.stdout.flush()
