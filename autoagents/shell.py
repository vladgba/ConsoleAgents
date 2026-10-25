"""The interactive shell: chats work in the background, like agent tabs.

  home       type a task -> a new chat starts in the background and opens
  in a chat  you see its output live; whatever you type goes to that chat (an answer, a follow-up,
             a correction); /back (or Ctrl+C) leaves it working and returns home
  always     a line appears when another chat needs your input, finishes or stops
"""
from __future__ import annotations

import codecs
import os
import shlex
import sys
import threading
import time
from pathlib import Path

from . import __version__, chats
from . import cli
from .project import ProjectState
from .registry import Registry, same_path
from .ui import UI, c

HELP = """In the shell (short forms in brackets):
  <task>                    at home: start a new chat for it, in the background, and open it
  <message>                 in a chat: send it to that chat (answer, follow-up, correction)
  /chats [all]       (/ls)  chats of this project (or of every project) and what they are doing
  /open <n|proj#n>   (/o)   open a chat: see its output live and type into it
  /back              (/b)   leave the chat (it keeps working); Ctrl+C does the same
  /new <task>   (/n, /task, /t)   start a new chat (also from inside a chat)
  /plan <task>  /noplan <task>   new chat that must / must not get a roadmap
  /continue [msg]    (/c)   continue the open (or current) chat
  /stop [n]          (/x)   stop a chat that is working
  /ps [--all]               processes agents started that still run (Gradle, adb, servers...)
  /kill <pid ...> | all     kill them (/k); "all" in the open chat only when a chat is open
  /rm <n ...> | --done      delete chats (asks first; -y skips, -f also deletes a working one)
  /status /roadmap /log     (/s /rd /l)  about the open chat
  /usage /agents            (/u /a)
  /usage refresh [agent]    (/u refresh) fetch current account quotas without model prompts
  /project [use <n|name>]   (/p)   your projects (numbered); /p 2 or /p shop switches
  /memory [add "text" [-g] | rm p3]   (/m)
  /settings [set <key> <value> [-g]]  (/st)
  /swf   /qqq               shut down when finished: on (lock phone screens, power off PC) / off
  /cd <dir>   /help (/?)   /quit (/q)
  aa <command> ...          any command-line command works too"""


def split_args(text: str) -> list[str]:
    """Split a typed command line like a shell: quotes group words and are removed, but backslashes stay
    as they are, so a quoted Windows path with spaces arrives as one argument, unchanged."""
    lex = shlex.shlex(text, posix=True)
    lex.whitespace_split = True
    lex.escape = ""  # a backslash is part of a path, not an escape
    lex.commenters = ""
    return list(lex)


# Commands only the shell has (the rest come from the command-line parser).
SHELL_WORDS = ["new", "n", "task", "t", "plan", "noplan", "open", "o", "back", "b", "home", "continue", "c",
               "stop", "x", "chats", "ls", "rm", "delete", "cd", "help", "quit", "q"]


VALUE_OPTIONS = {"--agent", "--max-turns", "--permission"}
FLAG_OPTIONS = {"--plan", "--no-plan", "--no-wait"}


def split_options(text: str) -> tuple[list[str], str]:
    """'/new --agent claude --no-plan fix the bug' -> (['--agent', 'claude', '--no-plan'], 'fix the bug').
    Only leading options are taken, so the task text itself is never re-parsed (quotes, apostrophes)."""
    flags: list[str] = []
    rest = text.strip()
    while rest.startswith("--"):
        word, _, after = rest.partition(" ")
        name = word.split("=", 1)[0]
        if name in FLAG_OPTIONS or (name in VALUE_OPTIONS and "=" in word):
            flags.append(word)
            rest = after.strip()
        elif name in VALUE_OPTIONS:
            value, _, after = after.strip().partition(" ")
            flags += [word, value]
            rest = after.strip()
        else:
            break
    return flags, rest


# The start screen's cheat sheet: (command, what it does).
QUICK = [
    ("<task>", "start a chat"), ("/o <n>", "open chat"), ("/b", "leave (keeps going)"),
    ("/ls", "list chats"), ("/c [msg]", "continue"), ("/x <n>", "stop chat"),
    ("/rm <n>", "delete chat"), ("/ps", "agent processes"), ("/k <pid>", "kill process"),
    ("/s", "chat status"), ("/u", "agent usage"),
    ("/p [n]", "projects / switch"), ("/m", "memory"), ("/st", "settings"),
    ("/swf", "off when all done"), ("/qqq", "cancel that"), ("/help", "all commands"), ("/q", "quit"),
]


def quick_help(ui: UI, columns: int = 3) -> None:
    cells = [c(f"{cmd:<9}", "bold", "cyan") + f"{desc:<20}" for cmd, desc in QUICK]
    for i in range(0, len(cells), columns):
        ui.line("  " + " ".join(cells[i:i + columns]).rstrip())


class Tailer(threading.Thread):
    """Prints what a background chat writes to its live log, as it comes."""

    QUIET_NOTE_S = 300  # say "still working" after this much silence

    def __init__(self, path: Path, state: ProjectState | None = None):
        super().__init__(daemon=True)
        self.path = path
        self.state = state
        self.last_data = time.time()
        self.halt = threading.Event()
        try:
            self.pos = path.stat().st_size
        except OSError:
            self.pos = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def run(self) -> None:
        while not self.halt.is_set():
            try:
                size = self.path.stat().st_size
                if size < self.pos:
                    self.pos = 0
                if size > self.pos:
                    with open(self.path, "rb") as fh:
                        fh.seek(self.pos)
                        data = fh.read(size - self.pos)
                    self.pos = size
                    text = self.decoder.decode(data)
                    if text:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                    self.last_data = time.time()
                elif self.state and time.time() - self.last_data > self.QUIET_NOTE_S:
                    self.last_data = time.time()
                    if chats.is_running(self.state):
                        sys.stdout.write(c(f"  … still working, no new output for {self.QUIET_NOTE_S // 60} min "
                                           "(a long build or test run; a silent agent is stopped after "
                                           "20 min and handed over)\n", "gray"))
                    else:
                        sys.stdout.write(c("  ■ the chat's background process is no longer running "
                                           "(/continue restarts it)\n", "yellow"))
                    sys.stdout.flush()
            except OSError:
                pass
            self.halt.wait(0.25)

    def stop(self) -> None:
        self.halt.set()
        self.join(timeout=1)


class Notifier(threading.Thread):
    """Watches every chat of every project and says when one needs you."""

    ALERT = {"needs input": ("🔔", "needs your input"), "done": ("✔", "is done"),
             "stopped": ("■", "stopped"), "waiting": ("⏳", "is waiting for usage limits to reset")}

    def __init__(self, session: "Session"):
        super().__init__(daemon=True)
        self.session = session
        self.halt = threading.Event()
        self.seen: dict[tuple[str, str], str] = {}
        self.first = True

    def run(self) -> None:
        while not self.halt.is_set():
            try:
                self.scan()
            except Exception:
                pass  # a half-written file must never kill the shell
            try:
                self.check_shutdown()
            except Exception:
                pass
            self.first = False
            self.halt.wait(3)

    def scan(self) -> None:
        attached = self.session.attached
        for root in self.session.watched_roots():
            name = cli._project_name(root) or root.name
            for info in chats.list_chats(root):
                key = (str(root).lower(), info.chat_id)
                prev = self.seen.get(key)
                self.seen[key] = info.status
                if self.first or prev is None or prev == info.status or info.status not in self.ALERT:
                    continue
                if attached and attached.chat_id == info.chat_id and same_path(attached.root, root):
                    continue  # you are looking at it
                icon, what = self.ALERT[info.status]
                ref = f"#{info.number}" if same_path(root, self.session.root) else f"{name}#{info.number}"
                extra = f": {str(info.question)[:120]}" if info.status == "needs input" and info.question else ""
                sys.stdout.write("\n" + c(f"{icon} chat {ref} ({info.text[:40]}) {what}{extra}  -> /open {ref}",
                                          "bold", "yellow") + "\n")
                sys.stdout.flush()

    def check_shutdown(self) -> None:
        """/swf: the open console triggers it as soon as no chat is working (checked every 3 s)."""
        from . import power
        if power.armed():
            run = cli.load_config(self.session.root)["run"]
            power.maybe_finish(run, lambda m: (sys.stdout.write("\n" + c(m, "bold", "yellow") + "\n"),
                                               sys.stdout.flush()), force=True)

    def stop(self) -> None:
        self.halt.set()


class Session:
    def __init__(self, root: Path, ui: UI, standalone: bool = False):
        self.root = root
        self.ui = ui
        self.standalone = standalone  # `aa open`: leaving the chat ends the session
        self.attached: ProjectState | None = None
        self.tailer: Tailer | None = None
        self.parser = cli.build_parser()

    # -- helpers ------------------------------------------------------------------------------
    def project_label(self) -> str:
        name = cli._project_name(self.root)
        if not name:
            return self.root.name
        num = Registry().number(name)
        return f"{num}:{name}" if num else name

    def prompt(self) -> str:
        from . import power
        mark = "⏻ " if power.armed() else ""  # shut down when finished is on
        if self.attached:
            return c(f"\n{mark}aa {self.project_label()} #{self.attached.number()}> ", "bold", "magenta")
        return c(f"\n{mark}aa {self.project_label()}> ", "bold", "cyan")

    def watched_roots(self) -> list[Path]:
        roots = [Path(i["path"]) for i in Registry().all().values() if Path(i["path"]).is_dir()]
        if not any(same_path(r, self.root) for r in roots):
            roots.append(self.root)
        return roots

    def find_chat(self, ref: str | None) -> ProjectState | None:
        """'3', '#3', an id, or 'project#3'."""
        root = self.root
        if ref and "#" in ref and not ref.startswith("#"):
            proj, _, ref = ref.partition("#")
            info = Registry().get(proj)
            if not info:
                self.ui.error(f"No project named {proj!r}.")
                return None
            root = Path(info["path"])
        try:
            state = ProjectState(root, ref or None)
        except ValueError as exc:
            self.ui.error(str(exc))
            return None
        if not state.chat_id:
            self.ui.error("No chat yet: type a task to start one.")
            return None
        return state

    # -- opening / leaving chats ------------------------------------------------------------------
    def attach(self, state: ProjectState, history_lines: int = 40) -> None:
        self.detach(quiet=True)
        if not same_path(state.root, self.root):
            self.root = state.root
        state.set_current(state.chat_id)
        info = chats.chat_info(state)
        self.ui.line()
        self.ui.line(c(f"══ chat #{state.number()} · {self.project_label()} · ", "bold", "magenta")
                     + cli.status_cell(info.status) + c(f" ══  {info.text[:80]}", "bold", "magenta"))
        lines = chats.tail(state.live_log, history_lines)
        if lines:
            self.ui.line(c("  (earlier output)", "gray"))
            for ln in lines:
                self.ui.line(ln)
        else:
            for s in state.recent_steps(8):
                self.ui.line(c(f"  {s['kind']:<8} {s.get('agent') or '':<8} {str(s.get('summary', ''))[:140]}", "gray"))
        if info.status == "needs input" and info.question:
            self.ui.line(c("\n  The agent asks: ", "bold", "yellow") + str(info.question))
        hint = {"working": "it is working; anything you type is read at its next turn",
                "waiting": "it waits for usage limits; type to add a message",
                "needs input": "type your answer",
                "done": "it is done; type a follow-up to continue it",
                "stopped": "type a message to continue it",
                "new": "type a message to start it"}.get(info.status, "")
        self.ui.line(c(f"  ({hint} · /back leaves it running · /stop stops it)", "gray"))
        self.attached = state
        self.tailer = Tailer(state.live_log, state)
        self.tailer.start()

    def detach(self, quiet: bool = False) -> None:
        if self.tailer:
            self.tailer.stop()
            self.tailer = None
        if self.attached and not quiet:
            running = chats.is_running(self.attached)
            self.ui.line(c(f"Left chat #{self.attached.number()}" + (" (it keeps working in the background)"
                                                                    if running else ""), "gray"))
        self.attached = None

    # -- actions ------------------------------------------------------------------------------------
    def message(self, text: str) -> None:
        state = self.attached
        if chats.is_running(state):
            chats.send(state, text)
            self.ui.line(c("  ✉ sent: the agent reads it at the start of its next turn", "gray"))
        else:
            chats.send(state, text)
            cli._register(state.root, self.ui)
            chats.spawn(state, ["resume"])
            self.ui.line(c("  ✉ sent, the chat continues…", "gray"))

    def run_argv(self, argv: list[str]) -> None:
        if not any(a in ("-C", "--dir", "-P", "--project") or a.startswith(("--dir=", "--project="))
                   for a in argv):
            argv = ["-C", str(self.root)] + argv
        if self.attached and not any(a == "--chat" or a.startswith("--chat=") for a in argv):
            argv = ["--chat", self.attached.chat_id] + argv
        try:
            args = self.parser.parse_args(argv)
        except SystemExit:
            return
        if not getattr(args, "func", None):
            return
        if args.func is cli.cmd_open:
            state = self.find_chat(args.chat_ref) if args.chat_ref else (self.attached or self.find_chat(None))
            if state:
                self.attach(state)
            return
        if args.func is cli.cmd_worker:
            self.ui.error("That is an internal command.")
            return
        background = args.func in (cli.cmd_run, cli.cmd_continue)
        if background:
            args.bg = True
            cli.LAST_CHAT.clear()
        try:
            args.func(args, self.ui)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                self.ui.error(str(exc.code))
            return
        if background and cli.LAST_CHAT.get("chat"):
            self.attach(ProjectState(cli.LAST_CHAT["root"], cli.LAST_CHAT["chat"]), history_lines=0)
        if args.func is cli.cmd_project and args.action == "use":
            self.detach()
            self.root = Path(Registry().get(args.name)["path"])
            self.overview()

    def new_chat(self, text: str, plan: str | None = None) -> None:
        if not text.strip():
            self.ui.error("Give the task, e.g. /new add a login page")
            return
        self.detach(quiet=True)
        flags, text = split_options(text)
        self.run_argv(["run", *([plan] if plan else []), *flags, text])

    def overview(self) -> None:
        self.ui.line(c(f"project: {self.project_label()}  {self.root}", "gray"))
        self.run_argv(["chats"])

    def quit(self) -> int:
        self.detach(quiet=True)
        from . import power
        if not self.standalone and power.armed() and not power.state().get("triggered_at"):
            power.disarm()  # nobody watches any more
            self.ui.info("⏻ Shutdown when finished turned off with the console (/swf works while it is open).")
        working = [(r, i) for r in self.watched_roots() for i in chats.list_chats(r) if i.running]
        if working:
            self.ui.info(f"{len(working)} chat(s) keep working in the background. "
                         "Come back with `aa` (or `aa chats --all`).")
        return 0

    # -- one line of input ------------------------------------------------------------------------------
    def handle(self, line: str) -> bool:
        """False when the shell should end."""
        line = line.strip()
        if not line:
            return True
        if line in ("/quit", "/exit", "/q", "exit", "quit"):
            return False
        if line in ("/help", "/?", "help", "/h"):
            self.ui.line(HELP)
            return True
        if line == "aa" or line.startswith("aa "):
            try:
                argv = split_args(line[2:])
            except ValueError as exc:
                self.ui.error(str(exc))
                return True
            if argv:
                self.run_argv(argv)
            return True
        if not line.startswith("/"):
            if self.attached:
                self.message(line)
            else:
                self.new_chat(line)
            return not (self.standalone and not self.attached)
        word, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        if word in ("back", "b", "home"):
            self.detach()
            return not self.standalone
        if word in ("new", "n", "run", "r", "task", "t"):
            self.new_chat(rest)
        elif word == "plan":
            self.new_chat(rest, "--plan")
        elif word == "noplan":
            self.new_chat(rest, "--no-plan")
        elif word in ("open", "o"):
            state = self.find_chat(rest or None)
            if state:
                self.attach(state)
        elif word in ("continue", "c"):
            target = self.attached or self.find_chat(None)
            if target:
                if not self.attached:
                    self.attach(target, history_lines=10)
                if rest:
                    self.message(rest)
                elif not chats.is_running(target):
                    chats.spawn(target, ["resume"])
                    self.ui.line(c("  the chat continues…", "gray"))
        elif word in ("delete", "rm", "del"):
            refs = rest.split()
            if not refs and self.attached:
                refs = [self.attached.chat_id]
            if self.attached and (self.attached.chat_id in refs or str(self.attached.number()) in
                                  [r.lstrip("#") for r in refs]):
                self.detach(quiet=True)
            self.run_argv(["delete", *refs])
        elif word in ("stop", "x"):
            state = self.find_chat(rest) if rest else (self.attached or None)
            if not state:
                self.ui.error("Which chat? /stop <n> (or open it first)")
            elif chats.stop(state):
                self.ui.ok(f"Stopped chat #{state.number()}.")
            else:
                self.ui.info(f"Chat #{state.number()} is not working.")
        elif word in ("project", "projects", "p") and rest and len(rest.split()) == 1 \
                and rest.split()[0] not in ("list", "ls", "add", "use", "remove", "rm", "rename", "mv", "info"):
            self.run_argv(["project", "use", rest])  # /p 2  or  /p shop
        elif word in ("chats", "ls"):
            self.run_argv(["chats", *(["--all"] if rest.startswith("all") else [])])
        elif word == "cd":
            new = Path(rest.strip('"')).expanduser()
            new = (self.root / new).resolve() if not new.is_absolute() else new.resolve()
            if new.is_dir():
                self.detach()
                self.root = new
                self.overview()
            else:
                self.ui.error(f"Not a directory: {new}")
        elif word not in self.commands():
            import difflib
            close = difflib.get_close_matches(word, SHELL_WORDS + self.commands(), n=2, cutoff=0.5)
            self.ui.error(f"Unknown command /{word}." + (f" Did you mean {' or '.join('/' + w for w in close)}?" if close else "")
                          + " To start a chat, type the task without a slash. /help lists the commands.")
        else:
            try:
                argv = [word] + split_args(rest)
            except ValueError as exc:
                self.ui.error(str(exc))
                return True
            self.run_argv(argv)
        return True

    def commands(self) -> list[str]:
        """Every command name and alias the command line knows."""
        for action in self.parser._actions:
            if getattr(action, "choices", None) and isinstance(action.choices, dict):
                return [n for n in action.choices if n != "worker"]
        return []

    def loop(self) -> int:
        while True:
            try:
                line = input(self.prompt())
            except EOFError:
                return self.quit()
            except KeyboardInterrupt:
                if self.attached and not self.standalone:
                    self.ui.line()
                    self.detach()
                    continue
                self.ui.line()
                return self.quit()
            if not self.handle(line):
                return self.quit()


def shell(base_args, ui: UI) -> int:
    root = cli._shell_start_root(base_args)
    ui.in_shell = True
    session = Session(root, ui)
    ui.line(c(f"AutoAgents {__version__}", "bold", "cyan") + c("   chats run in the background; type a task to start one", "gray"))
    ui.line()
    try:
        cli.compact_usage(root, ui)
    except Exception as exc:  # a broken config must not keep the shell from opening
        ui.warn(f"Usage unavailable: {exc}")
    ui.line()
    quick_help(ui)
    ui.line()
    session.overview()
    from . import power
    if power.armed() and not power.state().get("triggered_at"):
        power.disarm()  # /swf belongs to a console session: a new shell always starts with it off
    notifier = Notifier(session)
    notifier.start()
    try:
        return session.loop()
    finally:
        notifier.stop()
        session.detach(quiet=True)


def attach_standalone(state: ProjectState, ui: UI) -> int:
    """`aa open`: watch one chat and type into it; Ctrl+C or /back ends it (the chat keeps working)."""
    ui.in_shell = True
    session = Session(state.root, ui, standalone=True)
    session.attach(state)
    try:
        return session.loop()
    finally:
        session.detach(quiet=True)
