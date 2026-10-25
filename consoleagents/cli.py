"""Command line. Every command has a short alias (in brackets).

  aa                              interactive shell: chats run in the background, open / leave them
  aa run "task" [--bg]       (r)  start a new chat for a task (plans a roadmap first if it is complex)
  aa continue [-m "msg"]     (c)  continue a chat (after a limit, a question, Ctrl+C) or give a follow-up
  aa chats [--all]          (ls)  the chats of this project (or of every project) and what they are doing
  aa open [chat]             (o)  watch a chat live and type into it; Ctrl+C leaves it running
  aa stop [chat]             (x)  stop a chat that runs in the background
  aa delete <chat...>       (rm)  delete chats (aa rm 3 4, aa rm --done); memory and settings stay
  aa ps [--all]                   processes agents started that still run (Gradle, adb, servers...)
  aa kill <pid...|all>       (k)  kill them (only processes aa saw an agent start)
  aa push                         git push the current branch now (aa st set run.push on_done: after each chat)
  aa swf | aa qqq                 shut down when finished: on (lock phone screens + power off) | off
  aa status | roadmap | log  (s | rd | l)   look at a chat
  aa usage                   (u)  usage of every agent; `aa u refresh [agent]` fetches account quotas
  aa agents                  (a)  which CLIs are installed and enabled
  aa project ...             (p)  list / add / use / remove your projects (-P <name> runs a command in one)
  aa memory ...              (m)  project and global memory the agents read and add to
  aa settings ...           (st)  show / change settings, for this project or globally
  aa config [--global]      (cf)  create / show the config file
A chat is chosen with --chat <n|id> (default: the project's current chat).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, chats
from .config import (PROJECT_DIRNAME, WINDOWS, check_value, flatten, get_key, global_config_path, has_key,
                     home_dir, load_config, parse_value, project_config_path, read_config_file, set_key,
                     unset_key, write_config_file, write_default_config)
from .memory import global_memory, project_memory
from .orchestrator import Orchestrator
from .project import ProjectState
from .registry import NAME_RE, Registry, same_path
from .ui import UI, agent_tag, bar, c, fmt_in, fmt_time
from .usage import UsageStore

STATUS_STYLE = {"working": ("cyan",), "waiting": ("yellow",), "needs input": ("bold", "yellow"),
                "done": ("green",), "stopped": ("gray",), "new": ("gray",)}

# The chat the last run/continue command worked on (the shell opens it right after).
LAST_CHAT: dict = {}


def status_cell(status: str) -> str:
    return c(status, *STATUS_STYLE.get(status, ()))


# -- where: project, chat ---------------------------------------------------------------------------
def _project_root(args) -> Path:
    """-C <dir>, else -P <name>, else the registered project containing the current folder, else that folder."""
    if getattr(args, "dir", None):
        return Path(args.dir).expanduser().resolve()
    reg = Registry()
    name = getattr(args, "project", None)
    if name:
        info = reg.get(name)
        if not info:
            raise SystemExit(f"No project named {name!r}. See `aa project list`.")
        return Path(info["path"])
    cwd = Path.cwd().resolve()
    owner = reg.by_path(cwd)
    return Path(reg.get(owner)["path"]) if owner else cwd


def _shell_start_root(args) -> Path:
    """Where the shell opens: an explicit or current-folder project, else the project used last."""
    if getattr(args, "dir", None) or getattr(args, "project", None):
        return _project_root(args)
    cwd = Path.cwd().resolve()
    reg = Registry()
    if reg.by_path(cwd) or ((cwd / PROJECT_DIRNAME).is_dir() and (cwd / PROJECT_DIRNAME) != home_dir()):
        return _project_root(args)
    cur = reg.current()
    if cur and Path(reg.get(cur)["path"]).is_dir():
        return Path(reg.get(cur)["path"])
    return cwd


def _project_name(root: Path) -> str | None:
    return Registry().by_path(root)


def _register(root: Path, ui: UI) -> str | None:
    """Every folder a task runs in becomes a named project (named after the folder)."""
    reg = Registry()
    try:
        known = reg.by_path(root)
        name = known if known and same_path(reg.get(known)["path"], root) else reg.ensure(root)
    except ValueError:
        return None
    if name and not known:
        ui.info(f"New project {name!r} registered (rename it with: aa project rename {name} <new-name>)")
    reg.touch(name)
    return name


def _chat_ref(args) -> str | None:
    return getattr(args, "chat_ref", None) or getattr(args, "chat", None)


def _state(args) -> ProjectState:
    ref = _chat_ref(args)
    root = None
    if ref and "#" in ref and not ref.startswith("#"):  # "shop#3": chat 3 of project shop
        proj, _, ref = ref.partition("#")
        info = Registry().get(proj)
        if not info:
            raise SystemExit(f"No project named {proj!r}. See `aa project list`.")
        root = Path(info["path"])
    try:
        return ProjectState(root or _project_root(args), ref)
    except ValueError as exc:
        raise SystemExit(str(exc))


def _orchestrator(args, ui: UI, chat: str | None = None, root: Path | None = None) -> Orchestrator:
    root = root or _project_root(args)
    if not root.is_dir():
        raise SystemExit(f"Project directory not found: {root}")
    cfg = load_config(root)
    if getattr(args, "permission", None):
        cfg["run"]["permission"] = args.permission
    if getattr(args, "no_wait", False):
        cfg["run"]["wait_on_limits"] = False
    try:
        return Orchestrator(root, cfg, ui, chat=chat)
    except ValueError as exc:
        raise SystemExit(str(exc))


def _check_agent(orch: Orchestrator, name: str | None) -> None:
    if name and name not in orch.cfg["agents"]:
        raise SystemExit(f"Unknown agent '{name}'. Known: {', '.join(orch.cfg['agents'])}")


def _pass_through(args) -> list[str]:
    """run/continue options that a background worker must get too."""
    out = []
    if getattr(args, "agent", None):
        out += ["--agent", args.agent]
    if getattr(args, "max_turns", None):
        out += ["--max-turns", str(args.max_turns)]
    if getattr(args, "permission", None):
        out += ["--permission", args.permission]
    if getattr(args, "no_wait", False):
        out.append("--no-wait")
    return out


def _run_here(state: ProjectState, fn, ui: UI | None = None) -> int:
    """Run in this console, marked as the chat's worker so other consoles see it working."""
    chats.mark_worker(state)
    try:
        return fn()
    finally:
        chats.unmark_worker(state)
        _finished(state.root, ui)


def _finished(root: Path, ui: UI | None) -> None:
    """A chat's process ends. Shutdown-when-finished is handled by the open console (shell.Notifier),
    never by a chat's own process: with no console open, nothing may power the PC off."""


# -- task commands ------------------------------------------------------------------------------------
def cmd_run(args, ui: UI) -> int:
    orch = _orchestrator(args, ui)
    _check_agent(orch, args.agent)
    if not orch.installed:
        ui.error("None of the agent CLIs (claude, codex, agy) was found on PATH.")
        return 3
    text = " ".join(args.task).strip()
    if text == "-":
        text = sys.stdin.read().strip()
    if not text:
        ui.error("Empty task.")
        return 1
    name = _register(orch.root, ui)
    busy = [i for i in chats.list_chats(orch.root) if i.running]
    if busy:
        ui.warn(f"Chat #{busy[0].number} is also working in this folder: both may edit the same files.")
    orch.create(text)
    LAST_CHAT.update(root=orch.root, chat=orch.state.chat_id)
    planning = "always" if args.plan else "never" if args.no_plan else None
    if args.bg:
        flags = (["--plan"] if args.plan else ["--no-plan"] if args.no_plan else []) + _pass_through(args)
        chats.spawn(orch.state, ["begin", *flags])
        ui.ok(f"Chat #{orch.state.number()} started in the background"
              + (f" ({name})" if name else "") + f". Watch it: aa open {orch.state.number()}")
        return 0
    if name:
        ui.info(f"Project: {name} ({orch.root})")
    return _run_here(orch.state, lambda: orch.begin(agent=args.agent, planning=planning, max_turns=args.max_turns),
                     ui)


def cmd_continue(args, ui: UI) -> int:
    target = _state(args)  # accepts "3", an id or "project#3"
    orch = _orchestrator(args, ui, chat=target.chat_id, root=target.root)
    _check_agent(orch, args.agent)
    state = orch.state
    if not state.chat_id:
        raise SystemExit('No chat yet. Start one with: aa run "task"')
    LAST_CHAT.update(root=orch.root, chat=state.chat_id)
    message = " ".join(args.message).strip() if args.message else None
    if chats.is_running(state):
        if message:
            chats.send(state, message)
            ui.ok(f"Sent to chat #{state.number()}: the agent reads it at the start of its next turn.")
        else:
            ui.info(f"Chat #{state.number()} is already working. Watch it: aa open {state.number()}")
        return 0
    _register(orch.root, ui)
    if args.bg:
        if message:
            chats.send(state, message)
        chats.spawn(state, ["resume", *(["--fresh"] if args.fresh else []), *_pass_through(args)])
        ui.ok(f"Chat #{state.number()} continues in the background. Watch it: aa open {state.number()}")
        return 0
    return _run_here(state, lambda: orch.resume(message, agent=args.agent, max_turns=args.max_turns,
                                                 fresh=args.fresh), ui)


def cmd_worker(args, ui: UI) -> int:
    """The background process of one chat (started by run/continue --bg and by the shell)."""
    ui.in_shell = True  # its questions are answered by typing into the chat
    orch = _orchestrator(args, ui, chat=args.chat)
    chats.mark_worker(orch.state)
    try:
        if args.mode == "begin":
            planning = "always" if args.plan else "never" if args.no_plan else None
            return orch.begin(agent=args.agent, planning=planning, max_turns=args.max_turns)
        return orch.resume(None, agent=args.agent, max_turns=args.max_turns, fresh=args.fresh)
    except Exception as exc:  # never die silently: say it in the chat and mark it stopped
        import traceback
        traceback.print_exc()
        ui.error(f"This chat's background process crashed: {exc}. Continue it with a message or /continue.")
        task = orch.state.load_task()
        if task:
            task["status"] = "stopped"
            orch.state.save_task(task)
            orch.state.log_step("failure", None, f"background process crashed: {exc}")
        return 5
    finally:
        chats.unmark_worker(orch.state)
        _finished(orch.root, ui)


def cmd_chats(args, ui: UI) -> int:
    if args.all:
        roots = [Path(i["path"]) for i in Registry().all().values() if Path(i["path"]).is_dir()]
        here = _project_root(args)
        if not any(same_path(r, here) for r in roots) and (here / PROJECT_DIRNAME / "chats").is_dir():
            roots.append(here)
    else:
        roots = [_project_root(args)]
    rows = []
    for root in roots:
        name = _project_name(root) or root.name
        current = ProjectState(root).chat_id
        items = chats.list_chats(root)
        if not args.all_chats:
            items = items[-args.n:]
        for info in items:
            mark = "*" if info.chat_id == current else " "
            ref = f"{name}#{info.number}" if args.all else f"#{info.number}"
            rows.append((info.updated, [f"{mark}{ref}", status_cell(info.status), agent_tag(info.agent) if info.agent else "-",
                                        (info.text if ui.narrow else info.text[:60]).replace("\n", " "), fmt_time(info.updated)]))
    if not rows:
        ui.info('No chats yet. Start one: aa run "task" (or type a task in the shell).')
        return 0
    if args.all:
        rows.sort(key=lambda r: -r[0])
    ui.table(["chat", "status", "agent", "task", "updated"], [r[1] for r in rows],
             compact=None if getattr(args, "details", False) else [0, 1, 2, 3])
    if ui.narrow:
        ui.info("/o <n> open · /x <n> stop · /ls --details")
        return 0
    ui.line(c("* = current chat. Open one: aa open <n> (shell: /open <n>); stop: aa stop <n>", "gray"))
    return 0


def cmd_open(args, ui: UI) -> int:
    from .shell import attach_standalone
    state = _state(args)
    if not state.chat_id:
        raise SystemExit("No chat to open. See `aa chats`.")
    return attach_standalone(state, ui)


def cmd_delete(args, ui: UI) -> int:
    """Delete chats (their folder: task, roadmap, logs, transcript). Memory and settings are kept."""
    root = _project_root(args)
    base = ProjectState(root)
    if args.done:
        targets = [base.for_chat(i.chat_id) for i in chats.list_chats(root) if i.status == "done"]
    else:
        refs = args.chats or ([_chat_ref(args)] if _chat_ref(args) else [])
        if not refs:
            raise SystemExit("Which chat? aa rm <n> [<n> ...] (numbers from aa ls), or aa rm --done")
        targets = []
        for ref in refs:
            cid = base.resolve_chat(ref)
            if not cid:
                raise SystemExit(f"No chat {ref!r}. See `aa ls`.")
            targets.append(base.for_chat(cid))
    if not targets:
        ui.info("Nothing to delete.")
        return 0
    for st in targets:
        info = chats.chat_info(st)
        ui.line(f"  #{st.number()}  {status_cell(info.status)}  {info.text[:70]}")
    working = [st for st in targets if chats.is_running(st)]
    if working and not args.force:
        raise SystemExit(f"Chat #{working[0].number()} is working. Stop it first (aa x {working[0].number()}) "
                         "or add --force.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Add -y to delete without asking.")
        try:
            answer = input(f"Delete {len(targets)} chat(s) for good? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            ui.info("Kept.")
            return 1
    for st in targets:
        if chats.is_running(st):
            chats.stop(st)
        delete_chat(st)
    ui.ok(f"Deleted {len(targets)} chat(s). The remaining chats are renumbered (aa ls).")
    return 0


def delete_chat(state: ProjectState) -> None:
    import shutil
    for _ in range(10):  # Windows: a file may still be held for a moment by a process that just ended
        try:
            shutil.rmtree(state.dir)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.3)
    shutil.rmtree(state.dir, ignore_errors=True)


def _proc_scope(args, use_chat: bool = True) -> list[ProjectState]:
    """The chats whose processes a ps/kill command is about."""
    if getattr(args, "all", False):
        roots = [Path(i["path"]) for i in Registry().all().values() if Path(i["path"]).is_dir()]
        here = _project_root(args)
        if not any(same_path(r, here) for r in roots):
            roots.append(here)
    else:
        roots = [_project_root(args)]
    states = []
    for root in roots:
        base = ProjectState(root)
        if use_chat and _chat_ref(args) and not getattr(args, "all", False):
            states.append(_state(args))
        else:
            states += [base.for_chat(cid) for cid in base.chat_ids()]
    return states


def _running_procs(args, use_chat: bool = True) -> list[tuple[ProjectState, dict]]:
    from . import procs
    out = []
    for st in _proc_scope(args, use_chat):
        for rec in procs.running(st.dir):
            out.append((st, rec))
    return out


def cmd_ps(args, ui: UI) -> int:
    """Processes started by agents that are still running."""
    from . import procs
    found = _running_procs(args, use_chat=False)  # always the whole project: the chat column says which
    if not found:
        ui.info("No processes started by agents are running" + ("" if args.all else " in this project")
                + ("." if args.all else " (--all: every project)."))
        return 0
    info = procs.details([r["pid"] for _, r in found])
    rows = []
    for st, r in found:
        d = info.get(r["pid"], {})
        name = _project_name(st.root) or st.root.name
        kind = c("agent CLI", "cyan") if r.get("agent_cli") else ("running now" if chats.is_running(st) else "left over")
        age = int(time.time() - (r.get("created") or r.get("seen") or time.time()))
        rows.append([str(r["pid"]), r["name"], f"{name}#{st.number()}", agent_tag(r.get("agent") or "?"),
                     kind, f"{age // 3600}h{age % 3600 // 60:02d}m" if age >= 3600 else f"{age // 60}m",
                     f"{d.get('mem', 0) / 1e6:.0f} MB" if d.get("mem") else "-",
                     procs.describe(str(d.get("cmd") or r.get("cmd") or ""), 70)])
    ui.table(["pid", "name", "chat", "agent", "", "age", "memory", "command"], rows,
             compact=None if getattr(args, "details", False) else [0, 1, 2, 7])
    if ui.narrow:
        ui.info("/k <pid> kill · /ps --details")
        return 0
    ui.line(c("Kill: aa kill <pid ...> | aa kill all [--all] | aa kill --chat <n> (shell: /kill ...)", "gray"))
    return 0


def cmd_kill(args, ui: UI) -> int:
    """Kill processes agents started (with their children). Only processes aa recorded can be killed."""
    from . import procs
    targets = args.targets or []
    found = _running_procs(args, use_chat=not any(t.isdigit() for t in targets))  # pids: search the project
    if not targets and _chat_ref(args):
        targets = ["all"]
    if not targets:
        raise SystemExit("What to kill? aa kill <pid ...> | aa kill all [--all] | aa kill --chat <n>   (see ca ps)")
    if "all" in targets:
        chosen = [(st, r) for st, r in found if not (r.get("agent_cli") and chats.is_running(st))]
    else:
        wanted = {int(t) for t in targets if t.isdigit()}
        chosen = [(st, r) for st, r in found if r["pid"] in wanted]
        missing = wanted - {r["pid"] for _, r in chosen}
        if missing:
            ui.warn(f"Not a running process started by an agent: {', '.join(map(str, sorted(missing)))} "
                    "(aa only kills processes it saw an agent start).")
    if not chosen:
        ui.info("Nothing to kill.")
        return 0
    for st, r in chosen:
        ok = procs.kill(r)
        line = f"{r['name']} {r['pid']} ({_project_name(st.root) or st.root.name}#{st.number()}, {r.get('agent')})"
        (ui.ok if ok else ui.warn)(("Killed " if ok else "Could not kill ") + line)
    return 0


def cmd_swf(args, ui: UI) -> int:
    """Shut down when finished: on."""
    from . import power
    run = load_config(_project_root(args))["run"]
    working = [i for info in Registry().all().values() if Path(info["path"]).is_dir()
               for i in chats.list_chats(Path(info["path"])) if i.running]
    power.arm(work_now=bool(working))
    what = " and ".join(x for x in ("lock Android screens" if run.get("finish_lock_android", True) else "",
                                    f"power off this PC ({run.get('shutdown_grace_s', 60)} s to cancel)"
                                    if run.get("finish_shutdown", True) else "") if x) or "do nothing (both actions are off)"
    ui.ok(f"⏻ Shutdown when finished is ON: when the last working chat ends, aa will {what}.")
    if working:
        ui.info(f"{len(working)} chat(s) working now: " + ", ".join(f"{_project_name(i.root) or i.root.name}#{i.number}"
                                                                 for i in working))
    else:
        if ui.in_shell:
            ui.warn(f"No chat is working: this console starts it in a few seconds "
                    f"({run.get('shutdown_grace_s', 60)} s grace). /qqq cancels.")
        else:
            ui.info("No chat is working now.")
    ui.info("Turn it off with /qqq. It is watched by the open aa shell and ends when you close it"
            + ("." if ui.in_shell else ": run it inside `aa` (as /swf)."))
    return 0


def cmd_qqq(args, ui: UI) -> int:
    """Shut down when finished: off (and cancel a shutdown already scheduled)."""
    from . import power
    was = power.armed() or power.state().get("triggered_at")
    cancelled = power.disarm()
    if cancelled:
        ui.ok("⏻ Scheduled shutdown cancelled. Shutdown when finished is OFF.")
    else:
        ui.ok("⏻ Shutdown when finished is OFF." if was else "Shutdown when finished was already off.")
    return 0


def cmd_push(args, ui: UI) -> int:
    """Push the project's current branch now (what aa does by itself with run.push on_done)."""
    from .gitops import push
    root = _project_root(args)
    branches = list(load_config(root)["run"].get("push_branches") or [])
    res = push(root, branches)
    (ui.ok if res.ok else ui.warn)(res.message)
    return 0 if res.ok else 1


def cmd_update(args, ui: UI) -> int:
    """Update ConsoleAgents from GitHub or check for updates."""
    from . import updater

    root = updater.get_app_root()
    if not updater.is_git_repo(root):
        ui.error(f"Cannot update: '{root}' is not a git repository.")
        return 1

    if getattr(args, "rollback", False):
        return cmd_rollback(args, ui)

    fetch = not getattr(args, "no_fetch", False)
    status = updater.check_update(root, fetch=fetch)

    if status.error:
        ui.warn(status.error)
        return 1

    ui.info(f"ConsoleAgents repository: {root}")
    ui.info(f"Current version: {status.short_commit} on branch '{status.branch}'")
    if status.remote_url:
        ui.info(f"Remote: {status.remote_url}")

    if getattr(args, "check", False):
        if status.dirty:
            ui.warn("Working directory has uncommitted local changes.")
        if status.has_update:
            ui.warn(f"Update available: {status.behind} new commit(s) on remote '{status.remote}/{status.branch}'.")
            if status.incoming_commits:
                rows = [[c["short"], c["date"], c["author"][:15], c["subject"][:55]] for c in status.incoming_commits]
                ui.table(["commit", "date", "author", "message"], rows)
        else:
            ui.ok(f"ConsoleAgents is up to date with {status.remote}/{status.branch}.")
        return 0

    if not status.has_update and not getattr(args, "target", None):
        ui.ok(f"ConsoleAgents is already up to date ({status.short_commit}).")
        return 0

    if status.has_update:
        ui.info(f"Found {status.behind} new commit(s) from GitHub:")
        if status.incoming_commits:
            rows = [[c["short"], c["date"], c["author"][:15], c["subject"][:55]] for c in status.incoming_commits]
            ui.table(["commit", "date", "author", "message"], rows)

    allow_dirty = getattr(args, "allow_dirty", False)
    if status.dirty and not allow_dirty:
        ui.error("Working directory has uncommitted changes. Stash or commit them before updating (or pass --allow-dirty).")
        return 1

    if not getattr(args, "yes", False):
        target_name = getattr(args, "target", None) or f"{status.remote}/{status.branch} (+{status.behind} commits)"
        try:
            ans = input(f"Apply update to {target_name}? [y/N] ").strip().lower()
        except EOFError:
            ui.error("Add -y / --yes to apply updates without interactive confirmation.")
            return 1
        if ans != "y":
            ui.info("Update cancelled.")
            return 0

    res = updater.apply_update(root, target=getattr(args, "target", None), allow_dirty=allow_dirty)
    if not res.ok:
        ui.error(res.message)
        return 1

    ui.ok(res.message)
    if res.checkpoint:
        ui.info(f"Backup checkpoint saved: {res.checkpoint.short_commit} (use `ca rollback` if you need to revert).")
    return 0


def cmd_rollback(args, ui: UI) -> int:
    """Revert ConsoleAgents to a previous checkpoint or commit (fallback interface)."""
    from . import updater

    root = updater.get_app_root()
    if not updater.is_git_repo(root):
        ui.error(f"Cannot rollback: '{root}' is not a git repository.")
        return 1

    checkpoints = updater.load_checkpoints(root)
    cur_commit = updater.get_current_commit(root)

    if getattr(args, "list", False):
        if not checkpoints:
            ui.info("No saved update checkpoints found.")
            return 0
        ui.header("Update & Rollback Checkpoints")
        rows = []
        for i, cp in enumerate(reversed(checkpoints), 1):
            is_head = " (HEAD)" if cp.commit == cur_commit else ""
            rows.append([str(i), cp.short_commit + is_head, cp.iso_time or "-", cp.action, cp.message[:50]])
        ui.table(["#", "commit", "date", "action", "summary"], rows)
        return 0

    target = getattr(args, "target", None) or getattr(args, "checkpoint_ref", None)

    # If no target specified, show the interactive fallback interface
    if not target:
        if not checkpoints:
            ui.error("No saved checkpoints found in history to roll back to. Specify a commit hash explicitly (e.g. ca rollback <commit>).")
            return 1

        ui.header("Rollback Interface (Available Checkpoints)")
        rows = []
        rev_cp = list(reversed(checkpoints))
        for i, cp in enumerate(rev_cp, 1):
            is_head = " (HEAD)" if cp.commit == cur_commit else ""
            rows.append([str(i), cp.short_commit + is_head, cp.iso_time or "-", cp.action, cp.message[:50]])
        ui.table(["#", "commit", "date", "action", "summary"], rows)
        ui.line()

        if not getattr(args, "yes", False):
            try:
                choice = input(f"Select checkpoint to restore [1-{len(rev_cp)}], or 'q' to cancel: ").strip()
            except EOFError:
                ui.error("Interactive terminal required, or specify target (e.g. `ca rollback 1 -y`).")
                return 1
            if not choice or choice.lower() in ("q", "quit", "exit"):
                ui.info("Rollback cancelled.")
                return 0
            if not choice.isdigit() or not (1 <= int(choice) <= len(rev_cp)):
                ui.error(f"Invalid selection: '{choice}'. Expected number between 1 and {len(rev_cp)}.")
                return 1
            selected = rev_cp[int(choice) - 1]
            target = selected.commit
            prompt_name = f"#{choice} ({selected.short_commit} - {selected.message[:40]})"
        else:
            # -y without target defaults to most recent checkpoint (1)
            selected = rev_cp[0]
            target = selected.commit
            prompt_name = f"#1 ({selected.short_commit} - {selected.message[:40]})"
    else:
        prompt_name = str(target)

    allow_dirty = getattr(args, "allow_dirty", False)
    if updater.is_dirty(root) and not allow_dirty:
        ui.error("Working directory has uncommitted changes. Stash or commit them before rollback (or pass --allow-dirty).")
        return 1

    if not getattr(args, "yes", False):
        try:
            confirm = input(f"Revert ConsoleAgents working tree to {prompt_name}? [y/N] ").strip().lower()
        except EOFError:
            ui.error("Add -y to revert without interactive confirmation.")
            return 1
        if confirm != "y":
            ui.info("Rollback cancelled.")
            return 0

    res = updater.apply_rollback(root, target=str(target), allow_dirty=allow_dirty)
    if not res.ok:
        ui.error(res.message)
        return 1

    ui.ok(res.message)
    return 0


def cmd_stop(args, ui: UI) -> int:
    state = _state(args)
    if not state.chat_id:
        raise SystemExit("No chat. See `aa chats`.")
    if chats.stop(state):
        ui.ok(f"Stopped chat #{state.number()}. Continue it later with: aa continue --chat {state.number()}")
    else:
        ui.info(f"Chat #{state.number()} is not running.")
    return 0


def cmd_status(args, ui: UI) -> int:
    state = _state(args)
    task = state.load_task()
    if not task:
        ui.info(f"No chat in {state.base}.")
        return 0
    name = _project_name(state.root)
    info = chats.chat_info(state)
    ui.line(c(f"Project: {name or '(not registered)'}  {state.root}", "gray"))
    ui.header(f"Chat #{state.number()}")
    ui.line(task["text"])
    ui.line(f"Status: {status_cell(info.status)}   turns: {task.get('turns', 0)}   "
            f"last agent: {task.get('last_agent') or '-'}   agents used: {', '.join(task.get('agents_used') or []) or '-'}")
    if task.get("question") and info.status == "needs input":
        ui.block("Question for you:", str(task["question"]))
    if task.get("handover"):
        ui.line(f"Pending handover from {task['handover']['from']}: {task['handover']['reason']}")
    roadmap = state.roadmap_text(color=True)
    if roadmap:
        steps = state.load_roadmap()
        done = sum(1 for s in steps if s.get("status") == "done")
        ui.header(f"Roadmap {done}/{len(steps)}")
        ui.line(roadmap)
    recent = state.recent_steps(6)
    if recent:
        ui.header("Recent steps")
        for s in recent:
            ui.line(f"{fmt_time(s['ts'])}  {s['kind']:<8} {s.get('agent') or '':<7} {s.get('summary', '')[:150]}")
    return 0


def cmd_roadmap(args, ui: UI) -> int:
    text = _state(args).roadmap_text(color=True)
    ui.line(text if text else "No roadmap (the task is simple, or no chat yet).")
    return 0


def cmd_log(args, ui: UI) -> int:
    state = _state(args)
    if args.transcript:
        ui.line(state.recent_transcript(args.chars))
        return 0
    for s in state.recent_steps(args.n):
        ui.line(f"{fmt_time(s['ts'])}  {s['kind']:<8} {s.get('agent') or '':<7} {s.get('summary', '')}")
    return 0


# -- usage, agents ------------------------------------------------------------------------------------
def cmd_usage(args, ui: UI) -> int:
    root = _project_root(args)
    cfg = load_config(root)
    store = UsageStore()
    action = args.action
    if args.percent is not None and action != "set":
        raise SystemExit("A percentage is only valid with usage set.")
    if args.agent and args.agent not in cfg["agents"]:
        raise SystemExit(f"Unknown agent {args.agent!r}. Choose: {', '.join(cfg['agents'])}")
    refresh_failed = False
    if action == "refresh":
        from .usage_refresh import refresh_agent
        targets = [args.agent] if args.agent else [n for n, a in cfg["agents"].items() if a.get("enabled", True)]
        if not targets:
            ui.info("No enabled agents to refresh.")
        for name in targets:
            ui.info(f"Refreshing {name} account usage…")
            result = refresh_agent(name, cfg["agents"][name], store, root)
            if result.status in {"ok", "partial"}:
                ui.ok(f"{name}: {result.status}" + (f" — {result.message}" if result.message else ""))
            else:
                refresh_failed = True
                ui.error(f"{name}: {result.status} — {result.message} Previous snapshots retained.")
    if action in ("set", "clear", "reset"):
        if not args.agent or args.agent not in cfg["agents"]:
            raise SystemExit(f"Give an agent: {', '.join(cfg['agents'])}")
        if action == "set":
            if args.percent is None:
                raise SystemExit("Usage: aa usage set <agent> <percent> [--window five_hour|weekly] [--resets 2h30m]")
            resets = None
            if args.resets:
                from .limits import parse_reset
                resets = parse_reset("in " + args.resets, time.time()) or parse_reset("at " + args.resets, time.time())
            store.set_snapshot(args.agent, args.window, args.percent / 100.0, resets, source="manual")
            ui.ok(f"{args.agent}: {args.window} usage set to {args.percent:.0f}%"
                  + (f", resets {fmt_time(resets)}" if resets else ""))
        else:
            store.clear(args.agent, everything=(action == "reset"))
            ui.ok(f"{args.agent}: cooldown and reported usage cleared" + (" (and local history)" if action == "reset" else ""))
        return 0

    orch = Orchestrator(root, cfg, ui, store)
    rows = []
    for name, acfg in cfg["agents"].items():
        if args.agent and name != args.agent:
            continue
        u = store.agent_usage(name, acfg)
        info = store.refresh_info(name)
        state = agent_state(cfg, acfg, name, u, orch.installed)
        cells = [agent_tag(name)]
        for w in WINDOWS:
            wu = u.windows[w]
            cells.append(usage_cell(wu, info, short=ui.narrow and not args.agent, compact=ui.narrow))
            cells.append(fmt_in(wu.resets_at) if wu.resets_at and w not in info["disabled_windows"] else "-")
        cells.append(usage_updated(info))
        cells.append(state)
        cells.append(pace_cell(cfg, u, usable=acfg.get("enabled", True) and name in orch.installed))
        rows.append(cells)
    ui.table(["agent", "5-hour", "resets", "weekly", "resets", "usage check", "status", "pace"], rows,
             compact=None if args.agent else [0, 1, 3, 6])
    if ui.narrow and not args.agent:
        ui.info("Details: /u show <agent> · refresh: /u refresh")
    if ui.narrow:
        ui.line("est = estimate · stale/cached = old · unknown = no data")
        return 1 if refresh_failed else 0
    ui.line(c(f"\nsoft limit {cfg['balancer']['soft_limit']:.0%} (avoid), hard limit "
              f"{cfg['balancer']['hard_limit']:.0%} (never start). Source names identify account reports; "
              "est = local token estimate, manual = set by you. Reports include estimated local spend since observation.\n"
              "unknown = no account data; stale/cached = old report; disabled = provider says this window does not apply. "
              "Force a new account check: aa usage refresh [agent] (in the shell: /usage refresh [agent]).\n"
              "pace: the balancer looks at both windows. Above x1 = quota would be lost at the next reset, so the "
              "agent is preferred; below x1 = it is using its week (or 5 hours) too fast, so it is spared.", "gray"))
    return 1 if refresh_failed else 0


def _usage_age(stamp: float | None) -> str:
    if stamp is None:
        return "never"
    seconds = max(0, int(time.time() - stamp))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def usage_updated(info: dict) -> str:
    if info.get("checked_at") is not None:
        return f"{info['status']} · {_usage_age(info['checked_at'])}"
    stamps = [s["observed_at"] for s in info.get("snapshots", {}).values() if "observed_at" in s]
    return _usage_age(max(stamps)) if stamps else "never"


def usage_cell(wu, info: dict, short: bool = False, compact: bool = False) -> str:
    """Presentation only: retain the ledger's numeric values for balancing."""
    snap = info.get("snapshots", {}).get(wu.window)
    if wu.window in info.get("disabled_windows", []):
        resets = [w.get("resets_at") for w in info.get("quota_windows", [])
                  if w.get("applicable") and w.get("resets_at")]
        if ((resets and max(resets) <= time.time()) or
                (not resets and time.time() - (info.get("quota_observed_at") or 0) >= WINDOWS[wu.window] * 60)):
            return c("unknown (disabled report stale)", "yellow")
        return c("disabled", "gray")
    if wu.source != "estimate":
        cached = wu.source != "manual" and info.get("status") not in (None, "ok", "partial")
        label = " cached" if cached else ""
        if not short:
            label += f" {wu.source} · {_usage_age(snap.get('observed_at') if snap else None)}"
        return (f"{wu.utilization:.0%}" if compact else bar(wu.utilization, 8 if short else 12)) + c(label, "yellow" if cached else "gray")
    if wu.tokens:
        label = " est" + (" (account stale)" if snap and not short else "")
        return (f"{wu.utilization:.0%}" if compact else bar(wu.utilization, 8 if short else 12)) + c(label, "gray")
    if snap:
        label = f"{snap['utilization']:.0%} stale"
        if not short:
            label += f" · {_usage_age(snap.get('observed_at'))}; current unknown"
        return c(label, "yellow")
    return c("not reported" if info.get("status") in ("ok", "partial") else "unknown", "gray")


def pace_cell(cfg: dict, u, short: bool = False, usable: bool = True) -> str:
    """The pacing factor the balancer applies (both windows together), coloured. '-' for an agent
    that cannot be picked right now (paused, full, off): a bonus there would mean nothing."""
    from .balancer import pacing
    if not cfg["balancer"].get("pacing", True):
        return c("off", "gray")
    if not usable or u.in_cooldown() or u.utilization >= cfg["balancer"]["hard_limit"]:
        return c("-", "gray")
    factor, why = pacing(u, cfg["balancer"], time.time())
    color = "green" if factor >= 1.05 else "yellow" if factor <= 0.95 else "gray"
    text = f"x{factor:.2f}" + ("" if short or not why else f" {why}")
    return c(text, color)


def agent_state(cfg: dict, acfg: dict, name: str, u, installed: set[str], short: bool = False) -> str:
    """ready / low / too full / cooldown / not installed / disabled, coloured."""
    from .balancer import is_expired
    if not acfg.get("enabled", True):
        return c("disabled", "gray")
    if name not in installed:
        return c("not installed", "red")
    if is_expired(acfg, time.time()):
        return c("expired", "gray")
    if u.in_cooldown():
        why = "" if short else f" ({u.cooldown_reason})"
        return c(f"back in {fmt_in(u.cooldown_until)}{why}", "red")
    from .busy import busy_agents, where
    running = busy_agents(exclude_pid=-1).get(name)
    if running:
        return c(f"working ({where(running)})", "cyan")
    if u.utilization >= cfg["balancer"]["hard_limit"]:
        return c("too full", "red")
    if u.utilization >= cfg["balancer"]["soft_limit"]:
        return c("low", "yellow")
    return c("ready", "green")


def compact_usage(root: Path, ui: UI) -> None:
    """One line per usable agent: 5-hour and weekly bars plus its state (shell start screen)."""
    cfg = load_config(root)
    store = UsageStore()
    orch = Orchestrator(root, cfg, ui, store)
    rows, off = [], []
    for name, acfg in cfg["agents"].items():
        if not acfg.get("enabled", True) or name not in orch.installed:
            off.append(name)
            continue
        u = store.agent_usage(name, acfg)
        info = store.refresh_info(name)
        cells = [agent_tag(name)]
        for w in WINDOWS:
            wu = u.windows[w]
            cells.append(usage_cell(wu, info, short=True, compact=ui.narrow))
        cells.append(usage_updated(info))
        cells.append(agent_state(cfg, acfg, name, u, orch.installed, short=True))
        cells.append(pace_cell(cfg, u, short=True))
        rows.append(cells)
    if rows:
        ui.table(["agent", "5-hour", "week", "usage check", "status", "pace"], rows,
                 compact=[0, 1, 2, 4])
    note = ("est = local estimate · unknown = no account data · refresh: /u refresh · details: /u"
            " · pace >x1 = preferred; <x1 = spared"
            + (f" · off: {', '.join(off)}" if off else ""))
    if ui.narrow:
        note = "est = estimate · unknown = no data · /u for details"
    ui.line(c(note, "gray"))


def cmd_agents(args, ui: UI) -> int:
    root = _project_root(args)
    cfg = load_config(root)
    orch = Orchestrator(root, cfg, ui)
    rows = []
    for name, acfg in cfg["agents"].items():
        exe = orch.providers[name].executable()
        rows.append([agent_tag(name), acfg.get("type", name), "yes" if acfg.get("enabled", True) else "no",
                     exe or c("not found", "red"), str(acfg.get("model") or "default"), str(acfg.get("strength", 1.0)),
                     "/".join(acfg.get("levels") or ["any"]), str(acfg.get("active_until") or "-")])
    ui.table(["agent", "type", "enabled", "executable", "model", "strength", "levels", "active until"], rows)
    return 0


def cmd_config(args, ui: UI) -> int:
    path = global_config_path() if args.glob else project_config_path(_project_root(args))
    if write_default_config(path):
        ui.ok(f"Created {path}")
    else:
        ui.info(f"Config file: {path}")
    if args.show:
        ui.line(json.dumps(load_config(_project_root(args)), indent=2))
    ui.info("Edit it to enable/disable agents, set models, strengths, budgets and limits (or use aa settings).")
    return 0


# -- projects, memory, settings -----------------------------------------------------------------------
_GIT_URL_RE = re.compile(r"^(?:(?:https?|ssh|git|file)://|[\w.-]+@[\w.-]+:)")


def _is_git_url(s) -> bool:
    """A git address (https://, ssh://, git://, file://, git@host:owner/repo or a path to a bare *.git repo)."""
    if not s:
        return False
    if _GIT_URL_RE.match(s):
        return True
    if s.rstrip("/\\").endswith(".git"):
        p = Path(s).expanduser()
        return not p.exists() or ((p / "HEAD").is_file() and (p / "objects").is_dir())
    return False


def _repo_name(url: str) -> str:
    """The repository's name from its address: https://github.com/u/shop.git -> shop."""
    base = re.split(r"[/\\:]", url.rstrip("/\\"))[-1]
    return base[:-4] if base.endswith(".git") else base


def _origin_of(folder: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(folder), "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True)
    except OSError:
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def _same_remote(a: str, b: str) -> bool:
    def norm(u):
        u = u.strip().rstrip("/\\")
        u = u[:-4] if u.endswith(".git") else u
        if not _GIT_URL_RE.match(u):  # a local path
            return os.path.normcase(str(Path(u).expanduser().resolve()))
        return u.lower()
    return norm(a) == norm(b)


def _clone_target(url: str, where) -> Path:
    """Where `aa project add <url> [dir]` clones to: a new or empty `dir` itself, else `dir/<repo>` (default: ./<repo>)."""
    repo = _repo_name(url)
    if not where:
        return Path.cwd().resolve() / repo
    p = Path(where).expanduser().resolve()
    if not p.exists() or (p.is_dir() and not any(p.iterdir())):
        return p
    if not p.is_dir():
        raise SystemExit(f"Not a directory: {p}")
    origin = _origin_of(p) if (p / ".git").exists() else None
    return p if origin and _same_remote(origin, url) else p / repo


def _clone_project(url: str, target: Path, ui: UI) -> None:
    """Clone `url` into `target`, or keep `target` if it already is a checkout of `url`."""
    if target.exists() and any(target.iterdir()):
        origin = _origin_of(target) if (target / ".git").exists() else None
        if origin and _same_remote(origin, url):
            ui.info(f"{target} is already a clone of {url}; adding it as is.")
            return
        raise SystemExit(f"{target} already exists and is not a clone of {url}. Give another folder.")
    ui.info(f"Cloning {url} into {target} ...")
    try:
        done = subprocess.run(["git", "clone", *(["-q"] if ui.quiet else []), "--", url, str(target)])
    except OSError as exc:
        raise SystemExit(f"Cannot run git: {exc}. Install git to add projects from a repository.")
    if done.returncode != 0:
        raise SystemExit(f"git clone failed (exit code {done.returncode}); the project was not added.")


def cmd_project(args, ui: UI) -> int:
    reg = Registry()
    action = args.action or "list"
    try:
        if action in ("list", "ls"):
            projects = reg.all()
            if not projects:
                ui.info("No projects yet. Add one with: aa project add <name> [path] (or a git URL to clone)  "
                        "(a folder also becomes a project the first time you run a task in it)")
                return 0
            cur = reg.current()
            rows = []
            for num, name in enumerate(reg.ordered(), 1):
                info = projects[name]
                root = Path(info["path"])
                if root.is_dir():
                    items = chats.list_chats(root)
                    active = sum(1 for i in items if i.running)
                    waiting = sum(1 for i in items if i.status == "needs input")
                    chat_cell = f"{len(items)}" + (c(f", {active} working", "cyan") if active else "") \
                                + (c(f", {waiting} need input", "yellow") if waiting else "")
                    mem = str(len(project_memory(root).entries()))
                else:
                    chat_cell, mem = c("folder missing", "red"), "-"
                rows.append([("*" if name == cur else " ") + f"{num}", name, str(root), chat_cell, mem,
                             fmt_time(info["last_used"]) if info.get("last_used") else "-"])
            ui.table(["#", "project", "path", "chats", "memory", "last used"], rows)
            ui.line(c("* = the shell opens this one. Use a number or a name: /p 2 (switch), aa -P 2 <command>, "
                      "/open 2#3 (chat 3 of project 2)", "gray"))
        elif action == "add":
            if not args.name:
                raise SystemExit("Usage: aa project add <name> [path] [-d description]\n"
                                 "       aa project add [name] <git-url> [folder]   (clones the repository)")
            if _is_git_url(args.name) or _is_git_url(args.path):
                if _is_git_url(args.name):  # add <url> [folder]
                    url, where = args.name, args.path
                    name = _repo_name(url)
                    if args.dir:
                        raise SystemExit("Usage: aa project add [name] <git-url> [folder]")
                else:  # add <name> <url> [folder]
                    url, name, where = args.path, args.name, args.dir
                if not NAME_RE.match(name or ""):
                    raise SystemExit(f"Cannot use {name!r} as a project name; give one: "
                                     f"aa project add <name> {url} [folder]")
                if name in reg.all():
                    raise SystemExit(f"A project named {name!r} already exists ({reg.all()[name]['path']}). "
                                     f"Give another name: aa project add <name> {url} [folder]")
                root = _clone_target(url, where)
                _clone_project(url, root, ui)
                args.name = name
            else:
                if args.dir:
                    raise SystemExit("Usage: aa project add <name> [path] [-d description]")
                root = Path(args.path).expanduser().resolve() if args.path else Path.cwd().resolve()
            reg.add(args.name, root, args.description or "")
            ui.ok(f"Project {args.name!r} -> {root}")
        elif action == "use":
            if not args.name:
                raise SystemExit("Usage: aa project use <name>")
            info = reg.use(args.name)
            name = reg.resolve(args.name)
            ui.ok(f"Current project: #{reg.number(name)} {name} ({info['path']})")
        elif action in ("remove", "rm"):
            if not args.name:
                raise SystemExit("Usage: aa project remove <name>")
            name = reg.resolve(args.name) or args.name
            info = reg.remove(args.name)
            ui.ok(f"Removed {name!r} from the list. Its folder and {PROJECT_DIRNAME}/ were not touched: {info['path']}")
        elif action in ("rename", "mv"):
            if not args.name or not args.path:
                raise SystemExit("Usage: aa project rename <old> <new>")
            old = reg.resolve(args.name) or args.name
            reg.rename(args.name, args.path)
            ui.ok(f"Renamed {old!r} to {args.path!r}")
        elif action == "info":
            name = reg.resolve(args.name) if args.name else _project_name(_project_root(args))
            info = reg.get(name) if name else None
            if not info:
                raise SystemExit("Not a registered project. See `aa project list`.")
            root = Path(info["path"])
            ui.header(f"Project {name}")
            ui.line(f"Path: {root}")
            if info.get("description"):
                ui.line(f"About: {info['description']}")
            items = chats.list_chats(root)
            ui.line(f"Chats: {len(items)}" + "".join(f"\n  #{i.number} {status_cell(i.status)}  {i.text[:60]}"
                                                    for i in items[-5:]))
            ui.line(f"Memory: {len(project_memory(root).entries())} entries (aa -P {name} memory)")
            overrides = flatten(read_config_file(project_config_path(root)))
            ui.line("Project settings: " + (", ".join(f"{k}={json.dumps(v)}" for k, v in overrides.items())
                                            or "none (uses the global ones)"))
    except ValueError as exc:
        raise SystemExit(str(exc))
    return 0


def cmd_memory(args, ui: UI) -> int:
    root = _project_root(args)
    stores = {"project": project_memory(root), "global": global_memory()}
    action = args.action or "list"
    scope = "global" if args.glob else "project"
    if action in ("list", "ls"):
        shown = ["global"] if args.glob else ["project"] if args.proj else ["global", "project"]
        for sc in shown:
            store = stores[sc]
            ui.header("Global memory (all projects)" if sc == "global"
                      else f"Project memory: {_project_name(root) or root}")
            entries = store.entries()
            if not entries:
                ui.line(c("  (empty)", "gray"))
            for e in entries:
                ui.line(f"  {c(store.label(e), 'bold')}  {e['text']}"
                        + c(f"  ({e.get('by') or 'user'}, {fmt_time(e.get('ts'))})", "gray"))
        ui.line(c('\nAdd: aa m add "text" [-g]   remove: aa m rm p3 | g1', "gray"))
    elif action == "add":
        text = " ".join(args.text or []).strip()
        if not text:
            raise SystemExit('Usage: aa memory add "text" [--global]')
        entry = stores[scope].add(text, by="user")
        ui.ok(f"Saved to {scope} memory as {stores[scope].label(entry)}" if entry else "Already in memory.")
    elif action in ("rm", "remove", "del"):
        ident = " ".join(args.text or []).strip().lower()
        if not ident:
            raise SystemExit("Usage: aa memory rm <id>   (ids like p3 or g1, see aa memory)")
        sc = "global" if ident.startswith("g") else "project" if ident.startswith("p") else scope
        entry = stores[sc].remove(ident)
        if not entry:
            raise SystemExit(f"No {sc} memory entry {ident!r}.")
        ui.ok(f"Removed from {sc} memory: {entry['text']}")
    elif action == "clear":
        if not args.yes and (not sys.stdin.isatty()
                             or input(f"Delete ALL {scope} memory? [y/N] ").strip().lower() != "y"):
            ui.info("Kept.")
            return 1
        ui.ok(f"Deleted {stores[scope].clear()} {scope} memory entries.")
    return 0


SETTING_HINTS = "Examples: aa st set run.max_turns 20 | aa st set agents.codex.enabled false --global"


def cmd_settings(args, ui: UI) -> int:
    root = _project_root(args)
    gpath, ppath = global_config_path(), project_config_path(root)
    effective = load_config(root)
    action = args.action or "show"
    if action == "show":
        gfile, pfile = flatten(read_config_file(gpath)), flatten(read_config_file(ppath))
        rows = []
        for key, value in flatten(effective).items():
            if key.startswith("agents.") and not args.all:
                continue
            source = (c("project", "cyan") if key in pfile else c("global", "magenta") if key in gfile
                      else c("default", "gray"))
            rows.append([key, json.dumps(value), source])
        ui.table(["setting", "value", "from"], rows)
        ui.line(c(f"project file: {ppath}\nglobal file:  {gpath}\n"
                  "Project settings override global ones, which override the defaults. "
                  + ("" if args.all else "Add --all to list the agents' settings too.\n") + SETTING_HINTS, "gray"))
        return 0
    if not args.key:
        raise SystemExit(f"Give a setting name. {SETTING_HINTS}")
    if action == "get":
        try:
            ui.line(json.dumps(get_key(effective, args.key), indent=2))
        except KeyError:
            raise SystemExit(f"Unknown setting {args.key!r}. See `aa settings --all`.")
        return 0
    path = gpath if args.glob else ppath
    where = "global" if args.glob else f"project {_project_name(root) or root}"
    data = read_config_file(path)
    if action == "set":
        if args.value is None:
            raise SystemExit(f"Give a value. {SETTING_HINTS}")
        value = parse_value(args.value)
        problem = check_value(args.key, value, effective)
        if problem:
            raise SystemExit(f"{args.key}: {problem}")
        set_key(data, args.key, value)
        write_config_file(path, data)
        ui.ok(f"{args.key} = {json.dumps(value)}  ({where})")
    elif action == "unset":
        if not unset_key(data, args.key):
            raise SystemExit(f"{args.key} is not set in the {where} settings.")
        write_config_file(path, data)
        after = load_config(root)
        now = get_key(after, args.key) if has_key(after, args.key) else None
        ui.ok(f"{args.key} removed from the {where} settings; it is now {json.dumps(now)}")
    return 0


# -- parser ---------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ca", description="Console Agents: Claude / Codex / Antigravity relay for your "
                                "projects. Short aliases: r c ls o x rm k s rd l u a p m st cf up rb.")
    p.add_argument("--version", action="version", version=f"consoleagents {__version__}")
    p.add_argument("-C", "--dir", help="project directory (default: current directory)")
    p.add_argument("-P", "--project", help="a registered project, by number or name (see ca project list)")
    p.add_argument("--chat", help="which chat: its number (see ca chats) or id; default the current one")
    p.add_argument("-q", "--quiet", action="store_true", help="hide live agent activity")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("--agent", help="prefer this agent (claude, codex, antigravity, ...)")
        sp.add_argument("--max-turns", type=int, help="stop after N turns in this run")
        sp.add_argument("--permission", choices=["full", "edits", "read"], help="override run.permission")
        sp.add_argument("--no-wait", action="store_true", help="stop instead of waiting when every agent is limited")

    def chat_arg(sp):
        sp.add_argument("chat_ref", nargs="?", metavar="chat", help="chat number or id (default: current)")

    sp = sub.add_parser("run", aliases=["r", "task", "t"], help="start a new chat for a task")
    sp.add_argument("task", nargs="+", help='the task text, or "-" to read it from stdin')
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true", help="always make a roadmap")
    g.add_argument("--no-plan", action="store_true", help="never make a roadmap")
    sp.add_argument("--bg", action="store_true", help="run in the background (watch it with aa open)")
    sp.add_argument("-y", "--yes", action="store_true", help=argparse.SUPPRESS)  # kept for old scripts
    common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("continue", aliases=["c"], help="continue a chat, or answer it")
    sp.add_argument("-m", "--message", nargs="+", help="a message / answer for the agents")
    sp.add_argument("--fresh", action="store_true", help="start new agent chats instead of continuing the old ones")
    sp.add_argument("--bg", action="store_true", help="continue in the background")
    common(sp)
    sp.set_defaults(func=cmd_continue)

    sp = sub.add_parser("worker")  # internal: the background process of one chat
    sp.add_argument("mode", choices=["begin", "resume"])
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true")
    g.add_argument("--no-plan", action="store_true")
    sp.add_argument("--fresh", action="store_true")
    common(sp)
    sp.set_defaults(func=cmd_worker)

    sp = sub.add_parser("chats", aliases=["ls"], help="list chats and what they are doing")
    sp.add_argument("--all", action="store_true", help="chats of every project")
    sp.add_argument("-n", type=int, default=15, help="show the last N chats per project")
    sp.add_argument("--all-chats", action="store_true", help="no limit on the number of chats")
    sp.add_argument("--details", action="store_true", help="include all fields on narrow screens")
    sp.set_defaults(func=cmd_chats)

    sp = sub.add_parser("open", aliases=["o"], help="watch a chat live and type into it")
    chat_arg(sp)
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("delete", aliases=["rm", "del"], help="delete chats (memory and settings are kept)")
    sp.add_argument("chats", nargs="*", metavar="chat", help="chat numbers or ids")
    sp.add_argument("--done", action="store_true", help="delete every chat that is done")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask")
    sp.add_argument("-f", "--force", action="store_true", help="also delete a chat that is working (stops it)")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("ps", aliases=["procs"], help="processes agents started that are still running")
    sp.add_argument("--all", action="store_true", help="every project")
    sp.add_argument("--details", action="store_true", help="include all fields on narrow screens")
    sp.set_defaults(func=cmd_ps)

    sp = sub.add_parser("kill", aliases=["k"], help="kill processes agents started (see ca ps)")
    sp.add_argument("targets", nargs="*", metavar="pid", help='pids, or "all"')
    sp.add_argument("--all", action="store_true", help="with 'all': in every project")
    sp.set_defaults(func=cmd_kill)

    sub.add_parser("push", help="git push the project's current branch (no agent)").set_defaults(func=cmd_push)
    sub.add_parser("swf", help="shut down when finished: lock Android screens and power off the PC "
                   "when the last chat ends").set_defaults(func=cmd_swf)
    sub.add_parser("qqq", help="turn shut-down-when-finished off (cancels a scheduled shutdown)").set_defaults(func=cmd_qqq)

    sp = sub.add_parser("stop", aliases=["x"], help="stop a chat running in the background")
    chat_arg(sp)
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("status", aliases=["s"], help="show a chat")
    chat_arg(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("roadmap", aliases=["rd"], help="show a chat's roadmap")
    chat_arg(sp)
    sp.set_defaults(func=cmd_roadmap)

    sp = sub.add_parser("log", aliases=["l"], help="show a chat's steps log")
    chat_arg(sp)
    sp.add_argument("-n", type=int, default=30)
    sp.add_argument("--transcript", action="store_true", help="show the conversation instead")
    sp.add_argument("--chars", type=int, default=8000)
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("usage", aliases=["u"], help="show, refresh account quotas, or adjust usage")
    sp.add_argument("action", nargs="?", choices=["show", "refresh", "set", "clear", "reset"], default="show",
                    help="refresh fetches current account quotas without model prompts")
    sp.add_argument("agent", nargs="?")
    sp.add_argument("percent", nargs="?", type=float)
    sp.add_argument("--window", choices=list(WINDOWS), default="five_hour")
    sp.add_argument("--resets", help='when it resets: "2h30m" or "15:00"')
    sp.set_defaults(func=cmd_usage)

    sub.add_parser("agents", aliases=["a"], help="list agent CLIs").set_defaults(func=cmd_agents)

    sp = sub.add_parser("project", aliases=["projects", "p"], help="list / add / use / remove projects")
    sp.add_argument("action", nargs="?", choices=["list", "ls", "add", "use", "remove", "rm", "rename", "mv", "info"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("path", nargs="?", help="folder for add (default: current) or a git URL to clone, "
                                            "new name for rename")
    sp.add_argument("dir", nargs="?", help="add <name> <git-url> <folder>: where to clone")
    sp.add_argument("-d", "--description")
    sp.set_defaults(func=cmd_project)

    sp = sub.add_parser("memory", aliases=["mem", "m"], help="project and global memory")
    sp.add_argument("action", nargs="?", choices=["list", "ls", "add", "rm", "remove", "del", "clear"])
    sp.add_argument("text", nargs="*")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("-g", "--global", dest="glob", action="store_true", help="the global memory (all projects)")
    g.add_argument("-p", "--project-only", dest="proj", action="store_true", help="only this project's memory")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(func=cmd_memory)

    sp = sub.add_parser("settings", aliases=["st"], help="show / change settings")
    sp.add_argument("action", nargs="?", choices=["show", "get", "set", "unset"])
    sp.add_argument("key", nargs="?", help="e.g. run.max_turns, balancer.soft_limit, agents.codex.enabled")
    sp.add_argument("value", nargs="?", help='JSON value: 20, true, null, "text", ["light","mid"]')
    sp.add_argument("-g", "--global", dest="glob", action="store_true", help="change the global settings")
    sp.add_argument("--all", action="store_true", help="also list every agent setting")
    sp.set_defaults(func=cmd_settings)

    sp = sub.add_parser("config", aliases=["cf"], help="create/show the config file")
    sp.add_argument("--global", dest="glob", action="store_true", help="the global file instead of the project one")
    sp.add_argument("--show", action="store_true", help="print the effective config")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("update", aliases=["up", "upgrade"], help="update ConsoleAgents from GitHub or check for updates")
    sp.add_argument("--check", "-c", action="store_true", help="check if updates are available without applying")
    sp.add_argument("--target", help="target commit, branch, or tag to update to")
    sp.add_argument("--rollback", "-r", action="store_true", help="switch to rollback mode")
    sp.add_argument("--allow-dirty", action="store_true", help="allow updating even if local changes exist")
    sp.add_argument("-y", "--yes", action="store_true", help="apply update without asking for confirmation")
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("rollback", aliases=["revert", "rb"], help="revert ConsoleAgents to a previous checkpoint")
    sp.add_argument("checkpoint_ref", nargs="?", help="checkpoint index (1 = latest), id, or commit hash")
    sp.add_argument("--target", help="target commit, branch, or tag")
    sp.add_argument("-l", "--list", action="store_true", help="list saved checkpoints and exit")
    sp.add_argument("--allow-dirty", action="store_true", help="allow rollback even if local changes exist")
    sp.add_argument("-y", "--yes", action="store_true", help="revert without confirmation prompt")
    sp.set_defaults(func=cmd_rollback)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui = UI(quiet=args.quiet)
    if not args.command:
        from .shell import shell
        return shell(args, ui)
    try:
        return int(args.func(args, ui) or 0)
    except KeyboardInterrupt:
        ui.warn("Interrupted.")
        return 130
