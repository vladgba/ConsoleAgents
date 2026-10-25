"""Console output: colours (ANSI, honouring NO_COLOR), progress lines, tables, countdowns."""
from __future__ import annotations

import datetime as dt
import os
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
          "magenta": "35", "cyan": "36", "gray": "90"}
AGENT_COLORS = {"claude": "magenta", "codex": "green", "antigravity": "blue"}


def c(text: str, *styles: str) -> str:
    if not _COLOR or not styles:
        return str(text)
    return "\033[" + ";".join(_CODES[s] for s in styles if s in _CODES) + "m" + str(text) + "\033[0m"


def agent_tag(name: str) -> str:
    return c(name, "bold", AGENT_COLORS.get(name, "cyan"))


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

    def line(self, text: str = "") -> None:
        print(text, flush=True)

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
        self.line(c(f"━━ {text} ", "bold", "cyan") + c("━" * max(0, 60 - len(text)), "cyan"))

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
            self.line(c(f"  [{agent} {el // 60:02d}:{el % 60:02d}] ", AGENT_COLORS.get(agent, "cyan")) + msg)

        return emit

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        import re
        strip = lambda s: re.sub(r"\033\[[0-9;]*m", "", str(s))  # noqa: E731
        widths = [max(len(strip(h)), *(len(strip(r[i])) for r in rows)) if rows else len(h)
                  for i, h in enumerate(headers)]

        def fmt(cells):
            return "  ".join(str(cell) + " " * (w - len(strip(cell))) for cell, w in zip(cells, widths))

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
