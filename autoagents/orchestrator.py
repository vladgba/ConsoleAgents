"""The relay loop: plan once, then run turns, picking an agent each turn and handing over on limits."""
from __future__ import annotations

import time
from pathlib import Path

from . import busy as busy_mod
from . import chats
from . import procs as procs_mod
from .balancer import Candidate, Skipped, earliest_return, is_expired, rank_agents
from .config import PROJECT_DIRNAME
from .memory import global_memory, memory_section, project_memory
from .project import ProjectState
from .prompts import build_followup_prompt, build_plan_prompt, build_turn_prompt, parse_plan, parse_report
from .providers import RunResult, make_provider
from .ui import UI, agent_tag, bar, c, fmt_in, fmt_time
from .usage import UsageStore

# Exit codes of `run` / `continue`
EXIT_DONE, EXIT_MAX_TURNS, EXIT_NO_AGENT, EXIT_BLOCKED, EXIT_FAILURES, EXIT_INTERRUPTED = 0, 2, 3, 4, 5, 130

TRANSCRIPT_TURN_CAP = 120_000  # chars of one agent turn kept in the transcript
CRASH_COOLDOWN_S = 120
AUTH_COOLDOWN_S = 2 * 3600


class Orchestrator:
    def __init__(self, root: Path, cfg: dict, ui: UI | None = None, store: UsageStore | None = None,
                 chat: str | None = None):
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.run_cfg = cfg["run"]
        self.ui = ui or UI()
        self.store = store or UsageStore()
        self.state = ProjectState(self.root, chat)
        self.providers = {n: make_provider(n, a, self.run_cfg) for n, a in cfg["agents"].items()}
        self.installed = {n for n, p in self.providers.items() if p.executable()}

    # -- helpers -------------------------------------------------------------------------
    def usage_rows(self) -> list[tuple[str, str, float, str]]:
        rows = []
        soft = self.cfg["balancer"]["soft_limit"]
        for name, acfg in self.cfg["agents"].items():
            if not acfg.get("enabled", True) or name not in self.installed or is_expired(acfg, time.time()):
                continue
            u = self.store.agent_usage(name, acfg)
            notes = []
            if acfg.get("levels"):
                notes.append(f"takes ONLY {'/'.join(acfg['levels'])} steps")
            if u.in_cooldown():
                notes.append(f"UNAVAILABLE until {fmt_time(u.cooldown_until)}: {u.cooldown_reason}")
            elif u.utilization >= soft:
                notes.append("nearly exhausted, avoid giving it big steps")
            else:
                from .balancer import pacing
                factor, why = pacing(u, self.cfg["balancer"], time.time())
                if why and factor >= 1.05:
                    notes.append(f"{why}: give it work, its unused quota is lost at the reset")
                elif why and factor <= 0.95:
                    notes.append(f"{why}: spare it")
            rows.append((name, acfg.get("description", ""), u.utilization, "; ".join(notes)))
        return rows

    def _pick(self, *, hint=None, current=None, forced=None, exclude=None,
              level=None, hint_bonus: float | None = None, avoid=None,
              delegated: bool = False) -> tuple[Candidate | None, list[Skipped]]:
        busy = busy_mod.busy_agents()  # agents running a turn in other chats right now
        ranked, skipped = rank_agents(self.cfg, self.store, self.installed, hint=hint, current=current,
                                      exclude=exclude, level=level, forced=forced, hint_bonus=hint_bonus,
                                      avoid=avoid, busy=busy, delegated=delegated)
        if not ranked and avoid:
            # Everyone who could do it already said it is too hard: let the best of them try again.
            ranked, skipped = rank_agents(self.cfg, self.store, self.installed, hint=hint, current=current,
                                          exclude=exclude, level=level, forced=forced, hint_bonus=hint_bonus,
                                          busy=busy, delegated=delegated)
        if forced:
            for cand in ranked:
                if cand.name == forced:
                    return cand, skipped
            why = next((s.reason for s in skipped if s.name == forced), "unknown agent")
            self.ui.warn(f"Requested agent '{forced}' is not usable now ({why}); choosing another one.")
        return (ranked[0] if ranked else None), skipped

    def _wait_or_give_up(self, skipped: list[Skipped], task: dict) -> bool:
        """No agent available. Returns True when we waited (caller retries), False to stop."""
        busy = [s for s in skipped if s.busy]
        if busy:
            # They are only working in other chats: they will be free again soon, so wait for them.
            msg = "; ".join(f"{s.name}: {s.reason}" for s in busy)
            if msg != getattr(self, "_busy_msg", None):
                self._busy_msg = msg
                self.ui.info(f"Every agent that fits is working in another chat ({msg}). Waiting for one to be free…")
                self.state.log_step("wait", None, f"agents busy in other chats: {msg}")
            time.sleep(float(self.run_cfg.get("busy_poll_s", 15)))
            return True
        self._busy_msg = None
        for s in skipped:
            extra = f" - back at {fmt_time(s.available_at)}" if s.available_at else ""
            self.ui.warn(f"{s.name}: {s.reason}{extra}")
        back = earliest_return(skipped)
        max_wait = float(self.run_cfg.get("max_wait_hours", 12)) * 3600
        if not self.run_cfg.get("wait_on_limits", True) or not back or back - time.time() > max_wait:
            task["status"] = "waiting"
            self.state.save_task(task)
            self.ui.error("No agent can work right now." + (f" Earliest reset: {fmt_time(back)}." if back else ""))
            self.ui.info("Run `aa continue` later; the roadmap and progress are saved.")
            return False
        task["status"] = "waiting"
        self.state.save_task(task)
        self.state.log_step("wait", None, f"all agents limited, waiting until {fmt_time(back)}")
        self.ui.countdown(back + 15, "All agents are at their limits")
        return True

    # -- conversation reuse --------------------------------------------------------------
    def _session(self, task: dict, name: str) -> dict | None:
        """The agent's earlier conversation on this task, if it can be continued."""
        acfg = self.cfg["agents"][name]
        if not self.run_cfg.get("resume_sessions", True) or acfg.get("resume") is False:
            return None
        if not self.providers[name].supports_resume:
            return None
        sess = (task.get("sessions") or {}).get(name)
        if not sess or not sess.get("id"):
            return None
        if sess.get("turns", 0) >= int(self.run_cfg.get("session_max_turns", 8)):
            return None  # its context is getting long: a fresh chat with a compact prompt is cheaper
        return sess

    def _remember_session(self, task: dict, name: str, res: RunResult, kind: str, resumed: dict | None) -> None:
        sessions = task.setdefault("sessions", {})
        if not res.session_id:
            sessions.pop(name, None)
            return
        turns = (resumed.get("turns", 0) if resumed else 0) + 1
        sessions[name] = {"id": res.session_id, "turns": turns, "kind": kind,
                          "seen": self.state.transcript_count(), "ts": time.time()}

    def _escalate(self, task: dict, step: dict | None, name: str, why: str, stuck: bool = False) -> None:
        """The agent says the work is too hard for it: rate it one level up and give it to someone else."""
        order = ["light", "mid", "heavy"]
        if step:
            current = next((s for s in self.state.load_roadmap() if s["id"] == step["id"]), step)
            old = current.get("level")
            new = order[min(order.index(old) + 1, 2)] if old in order else "heavy"
            avoid = sorted(set(current.get("avoid") or []) | {name})
            self.state.update_step(step["id"], level=new, avoid=avoid)
            what = f"step {step['id']}"
        else:
            old = task.get("level")
            new = order[min(order.index(old) + 1, 2)] if old in order else "heavy"
            task["level"] = new
            task["avoid"] = sorted(set(task.get("avoid") or []) | {name})
            what = "the task"
        if stuck:
            task["handover"] = {"from": name, "reason": f"it made no progress on {what} in 3 turns in a row "
                                                       f"(last summary: {why[:300]}). Try a different approach"}
            self.state.log_step("escalate", name, f"{name} stuck on {what} for 3 turns: {old or 'unrated'} -> {new}")
            self.ui.warn(f"{name} made no progress on {what} in 3 turns: handing it to another agent.")
            return
        task["handover"] = {"from": name, "reason": f"it found {what} too hard for it ({why[:300]})"}
        self.state.log_step("escalate", name, f"{what} too hard for {name}: {old or 'unrated'} -> {new}")
        self.ui.warn(f"{name} says {what} is too hard for it: now rated {new}, handing it to a stronger agent.")

    @staticmethod
    def _no_progress(task: dict, report: dict) -> bool:
        """True on the 3rd turn in a row that finished nothing and reported the same thing (a loop)."""
        sig = " ".join((report.get("summary") or "").lower().split())[:300]
        history = task.setdefault("recent_summaries", [])
        progressed = bool(report.get("completed_steps")) or report.get("status") != "continue"
        stuck = not progressed and bool(sig) and history[-2:] == [sig, sig]
        history.append("" if progressed else sig)
        del history[:-3]
        if stuck:
            history.clear()  # the next agent starts with a clean count
        return stuck

    def profile(self, name: str) -> str:
        acfg = self.cfg["agents"][name]
        levels = "/".join(acfg.get("levels") or ["light", "mid", "heavy"])
        return (f"{name} (strength {acfg.get('strength', 1.0)} of 1.0; takes {levels} steps): "
                f"{acfg.get('description', '')}")

    def _leftovers(self, name: str, turn: int) -> None:
        """Processes the agent started in this turn that are still running after it finished."""
        try:
            left = [r for r in procs_mod.running(self.state.dir, include_agent_cli=False) if r.get("turn") == turn]
        except Exception:
            return
        if not left:
            return
        what = ", ".join(f"{r['name']} {r['pid']}" for r in left[:6]) + ("…" if len(left) > 6 else "")
        if str(self.run_cfg.get("leftover_processes", "keep")).lower() == "kill":
            killed = sum(1 for r in left if procs_mod.kill(r))
            self.ui.line(c(f"  ⚙ stopped {killed} process(es) {name} left running: {what}", "gray"))
            self.state.log_step("procs", name, f"killed leftovers: {what}")
        else:
            self.ui.line(c(f"  ⚙ {name} left {len(left)} process(es) running: {what}  (/ps to see, /kill to stop)",
                           "gray"))
            self.state.log_step("procs", name, f"left running: {what}")

    # -- rules -----------------------------------------------------------------------------
    def git_note(self) -> str:
        """What the commit rule means for this project (told to the agents every turn)."""
        mode = str(self.run_cfg.get("commit", "auto")).lower()
        if mode == "off":
            return "do NOT commit (the user commits themselves); this overrides the commit rule."
        if not self._is_git_repo():
            return "this project is not a git repository: skip committing and do not create one."
        note = ("this project is a git repository: commit as the rules say and list your commits in "
                "\"commits\". If the user had uncommitted changes before you started, leave them out of your commits.")
        if self.push_mode() != "off":
            note += " AutoAgents pushes by itself when the task is done: never push yourself."
        return note

    def push_mode(self) -> str:
        mode = str(self.run_cfg.get("push", "off")).lower()
        return mode if mode in ("off", "on_done") else "off"

    def _push_if_enabled(self) -> None:
        """git push by aa itself (no agent, no tokens) when a chat is done, if the project asks for it."""
        if self.push_mode() != "on_done" or str(self.run_cfg.get("commit", "auto")).lower() == "off":
            return
        from .gitops import push
        res = push(self.root, list(self.run_cfg.get("push_branches") or []))
        self.state.log_step("push", None, res.message)
        (self.ui.ok if res.ok else self.ui.warn)(("⬆ " if res.ok else "") + res.message)

    def _is_git_repo(self) -> bool:
        if getattr(self, "_git", None) is None:
            import subprocess
            try:
                out = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(self.root),
                                     capture_output=True, text=True, timeout=10)
                self._git = out.returncode == 0 and out.stdout.strip() == "true"
            except (OSError, subprocess.SubprocessError):
                self._git = any((d / ".git").exists() for d in (self.root, *self.root.parents))
        return self._git

    # -- memory ----------------------------------------------------------------------------
    def memory_text(self, since: float | None = None, skip_by: str | None = None) -> str:
        return memory_section(self.root, int(self.run_cfg.get("memory_chars", 6000)),
                              int(self.run_cfg.get("global_memory_chars", 3000)), since, skip_by)

    def _save_memories(self, items: list[dict], agent: str) -> None:
        for item in items[:10]:  # a runaway report must not flood the memory
            store = global_memory() if item["scope"] == "global" else project_memory(self.root)
            entry = store.add(item["text"], by=agent)
            if entry:
                self.state.log_step("memory", agent, f"[{item['scope']}] {entry['text']}")
                self.ui.line(c(f"  🧠 remembered ({item['scope']}, {store.label(entry)}): ", "gray") + entry["text"])

    def _run_agent(self, name: str, prompt: str, turn: int, purpose: str, resume: str | None = None) -> RunResult:
        provider = self.providers[name]
        acfg = self.cfg["agents"][name]
        how = f"continuing chat {resume[:8]}" if resume else "new chat"
        self.state.add_transcript("orchestrator", name,
                                  f"[{purpose} prompt sent to {name} ({how}): {len(prompt)} chars]", turn)
        timeout = float(self.run_cfg.get("turn_timeout_min", 45)) * 60
        mark = busy_mod.mark(name, self.root.name, self.state.number(), self.state.chat_id)
        progress = self.ui.progress_printer(name)

        def on_procs(found, cli_pid):
            for rec in procs_mod.record(self.state.dir, found, name, turn, cli_pid):
                if not rec["agent_cli"]:
                    what = procs_mod.describe(rec.get("cmd") or "")
                    progress(f"⚙ started {rec['name']} (pid {rec['pid']})" + (f": {what}" if what else ""))

        try:
            res = provider.run(prompt, self.root, progress, timeout, resume=resume, on_procs=on_procs)
        finally:
            busy_mod.unmark(mark)
        self._leftovers(name, turn)
        if resume and res.error_kind == "crash" and (res.duration_s < 90 or not res.log):
            # The old conversation could not be reopened (deleted, expired, ...): not the agent's fault.
            self.store.record_tokens(name, res.tokens)
            self.ui.warn(f"Could not continue {name}'s previous chat ({res.error}); starting a new one.")
            res.error_kind = "resume_failed"
            return res

        # Usage bookkeeping
        self.store.record_tokens(name, res.tokens)
        for snap in res.snapshots:
            self.store.set_snapshot(name, snap.window, snap.utilization, snap.resets_at, source=acfg.get("type", name))

        output = "\n".join(res.log).strip()
        if len(output) > TRANSCRIPT_TURN_CAP:
            half = TRANSCRIPT_TURN_CAP // 2
            output = output[:half] + "\n[...middle of a very long output cut...]\n" + output[-half:]
        if res.error_kind:
            output += f"\n\n[orchestrator: run ended with {res.error_kind}: {res.error}]"
        self.state.add_transcript("agent", name, output or "(no output)", turn)

        now = time.time()
        if res.error_kind == "limit":
            until = res.limit_reset or now + 60 * float(acfg.get("default_cooldown_min", 60))
            window = res.limit_window
            if not window and res.limit_reset:
                # The CLI did not say which window: a reset further away than 5 hours can only be the week.
                window = "weekly" if res.limit_reset - now > 5.2 * 3600 else "five_hour"
            self.store.set_cooldown(name, until, "usage limit reached")
            if not res.snapshots:
                self.store.learn_from_limit(name, window)
            if window:
                self.store.set_snapshot(name, window, 1.0, until, source="limit")
            self.ui.warn(f"{name} hit its usage limit ({res.error}). Back at {fmt_time(until)} (in {fmt_in(until)}).")
        elif res.error_kind == "auth":
            self.store.set_cooldown(name, now + AUTH_COOLDOWN_S, "login/auth problem")
            self.ui.error(f"{name}: authentication problem: {res.error}")
            self.ui.info(f"Fix it by running `{acfg.get('command', name)}` interactively and logging in, "
                         f"then `aa usage clear {name}`.")
        elif res.error_kind in ("crash", "timeout", "stalled", "loop"):
            self.store.set_cooldown(name, now + CRASH_COOLDOWN_S, res.error_kind)
            self.ui.error(f"{name} failed ({res.error_kind}): {res.error}")

        u = self.store.agent_usage(name, acfg)
        cost = f", ${res.cost_usd:.2f}" if res.cost_usd else ""
        self.ui.line(c(f"  └ {name}: {res.duration_s / 60:.1f} min, ~{res.tokens:,} tokens{cost}, usage ", "gray")
                     + bar(u.utilization) + c(f" ({u.worst_window.source if u.worst_window else '-'})", "gray"))
        return res

    # -- public entry points ---------------------------------------------------------------
    def start(self, text: str, *, agent: str | None = None, planning: str | None = None,
              max_turns: int | None = None) -> int:
        self.create(text)
        return self.begin(agent=agent, planning=planning, max_turns=max_turns)

    def create(self, text: str) -> dict:
        """A new chat for this task (not started yet)."""
        task = self.state.new_task(text)
        self.state.add_transcript("user", None, text, 0)
        self.state.log_step("task", None, text)
        return task

    def begin(self, *, agent: str | None = None, planning: str | None = None, max_turns: int | None = None) -> int:
        """Plan (when needed) and work on the chat created by create()."""
        task = self.state.load_task()
        self.ui.header(f"New task (chat #{self.state.number()})")
        self.ui.line(task["text"])
        self.ui.info(f"State folder: {self.state.dir}")
        try:
            mode = planning or self.run_cfg.get("planning", "auto")
            if mode != "never":
                self._plan(task, mode, agent)
            return self._loop(agent, max_turns)
        except KeyboardInterrupt:
            return self._interrupted()

    def resume(self, message: str | None = None, *, agent: str | None = None, max_turns: int | None = None,
               fresh: bool = False) -> int:
        task = self.state.load_task()
        if not task:
            self.ui.error(f"No task in {self.root / PROJECT_DIRNAME}. Start one with: aa run \"...\"")
            return EXIT_NO_AGENT
        if fresh:
            task["sessions"] = {}  # every agent starts a new chat with the full context
        messages = chats.drain(self.state) + ([message] if message else [])
        if messages:
            self._take_messages(task, messages)
        elif task.get("status") == "done":
            self.ui.ok("This task is already done. Give a follow-up with: aa continue -m \"...\"")
            return EXIT_DONE
        self.state.save_task(task)
        self.ui.header("Continuing task")
        self.ui.line(task["text"])
        try:
            return self._loop(agent, max_turns)
        except KeyboardInterrupt:
            return self._interrupted()

    def _take_messages(self, task: dict, messages: list[str]) -> None:
        for msg in messages:
            task.setdefault("user_messages", []).append(msg)
            self.state.add_transcript("user", None, msg, task.get("turns", 0))
            self.state.log_step("user", None, msg)
        if task.get("status") in ("done", "blocked"):
            task["question"] = None

    def _new_messages(self, task: dict) -> bool:
        """Messages typed into the chat while it was working. True when there were some."""
        msgs = chats.drain(self.state)
        if msgs:
            self._take_messages(task, msgs)
            for m in msgs:
                self.ui.line(c("  ✉ you: ", "bold", "cyan") + m)
        return bool(msgs)

    # -- planning ------------------------------------------------------------------------------
    def _plan(self, task: dict, mode: str, forced: str | None) -> None:
        tried: set[str] = set()
        while True:
            cand, skipped = self._pick(forced=forced if not tried else None, exclude=tried, level="heavy")
            if cand is None:
                if not self._wait_or_give_up(skipped, task):
                    return
                tried.clear()
                continue
            self.ui.header(f"Planning with {cand.name}")
            prompt = build_plan_prompt(task["text"], self.usage_rows(), cand.name, str(self.root), mode,
                                       memory=self.memory_text())
            res = self._run_agent(cand.name, prompt, 0, "planning")
            if res.error_kind == "interrupted":
                raise KeyboardInterrupt
            if not res.ok:
                tried.add(cand.name)
                continue
            plan = parse_plan(res.final_text)
            self._remember_session(task, cand.name, res, "plan", None)
            task["level"] = plan["level"] if plan else None
            if plan and plan["complex"]:
                steps = self.state.set_roadmap(plan["roadmap"])
                task["complex"] = True
                self.state.save_task(task)
                self.state.log_step("roadmap", cand.name, f"{len(steps)} steps. {plan['reason']}")
                self.ui.ok(f"Roadmap saved ({len(steps)} steps) to {self.state.roadmap_md}")
                self.ui.block("", self.state.roadmap_text(color=True))
            else:
                task["complex"] = False
                self.state.save_task(task)
                why = plan["reason"] if plan else "planner returned no plan block"
                self.state.log_step("plan", cand.name, f"no roadmap needed ({why})")
                self.ui.info(f"No roadmap needed: {why}")
            return

    # -- the relay loop -------------------------------------------------------------------------
    def _loop(self, forced: str | None, max_turns: int | None) -> int:
        task = self.state.load_task()
        max_turns = int(max_turns or self.run_cfg.get("max_turns", 40))
        max_fail = int(self.run_cfg.get("max_consecutive_failures", 4))
        failures: dict[str, int] = {}
        consecutive = 0
        turns = 0
        task["status"] = "running"
        self.state.save_task(task)

        while True:
            if turns >= max_turns:
                task["status"] = "stopped"
                self.state.save_task(task)
                self.state.log_step("stop", None, f"turn limit ({max_turns}) reached")
                self.ui.warn(f"Stopped after {max_turns} turns. Continue with: aa continue")
                return EXIT_MAX_TURNS

            if self._new_messages(task):
                self.state.save_task(task)
            step = self.state.current_step()
            exclude = {n for n, k in failures.items() if k >= 2}
            level = step.get("level") if step else task.get("level")
            delegated = bool(step and step.get("delegated_by") and step.get("agent"))
            avoid = set((step or {}).get("avoid") or []) if step else set(task.get("avoid") or [])
            cand, skipped = self._pick(hint=step.get("agent") if step else None,
                                       current=task.get("last_agent"), forced=forced, exclude=exclude,
                                       level=level, hint_bonus=float(self.cfg["balancer"].get("delegate_bonus", 1.6))
                                       if delegated else None, avoid=avoid, delegated=delegated)
            if cand is None:
                if not self._wait_or_give_up(skipped, task):
                    return EXIT_NO_AGENT
                task["status"] = "running"
                continue
            name = cand.name
            prev = task.get("last_agent")
            if prev and prev != name and not task.get("handover"):
                prev_u = self.store.agent_usage(prev, self.cfg["agents"][prev]) if prev in self.cfg["agents"] else None
                if forced == name:
                    reason = f"the user asked for {name} to work on it"
                elif prev_u:
                    reason = (f"the orchestrator moved the work to balance usage limits ({prev} is at "
                              f"{prev_u.utilization:.0%}, {name} has more room)")
                else:
                    reason = "agent rotation"
                task["handover"] = {"from": prev, "reason": reason}
                self.state.log_step("switch", name, f"taking over from {prev}: {reason}")
            if step and step.get("status") == "todo":
                self.state.mark_step(step["id"], "in_progress")
                step["status"] = "in_progress"

            task["turns"] = task.get("turns", 0) + 1
            turns += 1
            self.state.save_task(task)
            label = f"Turn {task['turns']} - {name}"
            if step:
                label += f" - step {step['id']}: {step['title'][:40]}"
            if level:
                label += f" [{level}]"
            self.ui.header(label)
            if cand.note:
                self.ui.line(c(f"  ({cand.note})", "gray"))
            if task.get("handover"):
                self.ui.info(f"Handover from {agent_tag(task['handover']['from'])}: {task['handover']['reason']}")

            history_chars = int(self.cfg["agents"][name].get("history_chars") or self.run_cfg.get("history_chars", 40000))
            roadmap = self.state.roadmap_text(current_id=step["id"] if step else None)
            sess = self._session(task, name)

            def full_prompt(history: bool = True) -> str:
                return build_turn_prompt(
                    agent=name, root=str(self.root), task=task, roadmap_text=roadmap,
                    step=step, recent_steps=self.state.recent_steps(12),
                    history=self.state.recent_transcript(history_chars) if history else "",
                    handover=task.get("handover"), user_messages=task.get("user_messages") or [],
                    usage_rows=self.usage_rows(), state_dir=self.state.rel(self.state.dir), memory=self.memory_text(),
                    rules=list(self.run_cfg.get("rules") or []), git_note=self.git_note(),
                    profile=self.profile(name),
                )

            if sess and sess.get("kind") == "plan":
                # It planned in this chat, so it knows the project, but not yet the turn rules.
                prompt = full_prompt(history=False)
            elif sess:
                prompt = build_followup_prompt(
                    agent=name, roadmap_text=roadmap, step=step,
                    news=self.state.recent_transcript(history_chars, since=int(sess.get("seen", 0)), skip_agent=name),
                    handover=task.get("handover"), user_messages=task.get("user_messages") or [],
                    state_dir=self.state.rel(self.state.dir), memory=self.memory_text(since=sess.get("ts", 0), skip_by=name),
                    git_note=self.git_note())
            else:
                prompt = full_prompt()
            if sess:
                self.ui.line(c(f"  (continues its own chat, turn {sess.get('turns', 0) + 1} in it: only "
                               f"{len(prompt):,} chars sent, not the full history)", "gray"))
            res = self._run_agent(name, prompt, task["turns"], "work", resume=sess["id"] if sess else None)
            if res.error_kind == "resume_failed":
                sess = None
                res = self._run_agent(name, full_prompt(), task["turns"], "work")
            task = self.state.load_task()  # _run_agent does not touch it, but stay safe
            self._remember_session(task, name, res, "work", sess)

            if res.error_kind == "interrupted":
                task["handover"] = {"from": name, "reason": "it was stopped by the user (Ctrl+C) mid-turn"}
                task["last_agent"] = name
                self.state.save_task(task)
                raise KeyboardInterrupt

            if not res.ok:
                consecutive += 1
                task["last_agent"] = name
                if res.error_kind == "limit":
                    task["handover"] = {"from": name, "reason": "it reached its usage limit"}
                    self.state.log_step("limit", name, f"usage limit reached: {res.error}")
                else:
                    failures[name] = failures.get(name, 0) + 1
                    task["handover"] = {"from": name, "reason": f"its run failed ({res.error_kind}: {res.error})"}
                    self.state.log_step("failure", name, f"{res.error_kind}: {res.error}")
                self.state.save_task(task)
                if consecutive >= max_fail:
                    task["status"] = "stopped"
                    self.state.save_task(task)
                    self.ui.error(f"{consecutive} failed turns in a row, stopping. Check `aa log`, then `aa continue`.")
                    return EXIT_FAILURES
                continue

            consecutive = 0
            failures.pop(name, None)
            report = parse_report(res.final_text)
            had_roadmap = bool(self.state.load_roadmap())
            self.state.apply_report(report["completed_steps"], report["add_steps"], name)
            if report["add_steps"] and report["status"] == "done":
                report["status"] = "continue"  # it planned new steps: they are not done yet
            self._save_memories(report["remember"], name)
            for commit in report["commits"][:20]:
                self.state.log_step("commit", name, commit)
                self.ui.line(c("  📦 commit ", "gray") + commit)
            if report["delegate"]:
                new = self.state.delegate(report["delegate"][:8], by=name)
                for st in new:
                    self.state.log_step("delegate", name, f"{st['title']} -> {st.get('agent') or 'any agent'}"
                                        + (f" [{st['level']}]" if st.get("level") else ""))
                    self.ui.line(c("  ↪ delegated ", "gray") + f"{st['title']} → {agent_tag(st['agent']) if st.get('agent') else 'any agent'}"
                                 + (c(f" [{st['level']}]", "gray") if st.get("level") else ""))
                if report["status"] == "done":
                    report["status"] = "continue"  # the delegated parts are not done yet
            task["user_messages"] = []
            task["handover"] = None
            task["last_agent"] = name
            if report["add_steps"] and not had_roadmap:
                self.state.log_step("roadmap", name, f"{len(report['add_steps'])} steps planned during the work")
                self.ui.ok(f"{name} planned {len(report['add_steps'])} steps; each goes to the agent best suited for its level.")
            if report["too_hard"] and report["status"] != "done":
                self._escalate(task, step, name, report["summary"])
                report["status"] = "continue"
            elif self._no_progress(task, report):
                self._escalate(task, step, name, report["summary"], stuck=True)
            if name not in task.setdefault("agents_used", []):
                task["agents_used"].append(name)
            self.state.log_step("turn", name, report["summary"] or "(no summary)", status=report["status"],
                                completed=report["completed_steps"])
            self.ui.line()
            self.ui.line(c("  Summary: ", "bold") + (report["summary"] or "-"))
            if report["next"]:
                self.ui.line(c("  Next:    ", "bold") + report["next"])
            if report["missing"]:
                self.ui.warn("The agent did not send its report block; assuming it should continue.")

            if report["status"] in ("done", "blocked") and chats.has_inbox(self.state):
                # You wrote to the chat while the agent was finishing: keep going with your message.
                self.state.log_step("turn", name, "new user message arrived, continuing")
                self.state.save_task(task)
                continue
            if report["status"] == "done":
                self.state.complete_all_steps(name)
                task["status"] = "done"
                self.state.save_task(task)
                self.state.log_step("done", name, report["summary"])
                self.ui.header("Task complete")
                self.ui.ok(report["summary"] or "Done.")
                self._push_if_enabled()
                return EXIT_DONE
            if report["status"] == "blocked":
                task["status"] = "blocked"
                task["question"] = report["question"] or report["summary"]
                self.state.save_task(task)
                self.state.log_step("blocked", name, str(task["question"]))
                self.ui.header("Agent needs your input")
                self.ui.line(str(task["question"]))
                self.ui.info("Type your answer in this chat (/open it from the chat list if you left it)"
                             if self.ui.in_shell else 'Answer with: aa continue -m "your answer"')
                return EXIT_BLOCKED
            self.state.save_task(task)

    def _interrupted(self) -> int:
        task = self.state.load_task()
        if task:
            task["status"] = "stopped"
            self.state.save_task(task)
            self.state.log_step("stop", None, "stopped by user (Ctrl+C)")
        self.ui.line()
        self.ui.warn("Stopped. Everything is saved; resume with: aa continue")
        return EXIT_INTERRUPTED
