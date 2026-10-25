import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autoagents.balancer import rank_agents  # noqa: E402
from autoagents.config import DEFAULT_CONFIG, deep_merge  # noqa: E402
from autoagents.limits import is_auth_error, is_limit_error, parse_reset  # noqa: E402
from autoagents.orchestrator import EXIT_DONE, EXIT_NO_AGENT, Orchestrator  # noqa: E402
from autoagents.project import ProjectState  # noqa: E402
from autoagents.prompts import parse_plan, parse_report  # noqa: E402
from autoagents.providers import ClaudeProvider, CodexProvider, RunResult  # noqa: E402
from autoagents.ui import UI  # noqa: E402
from autoagents.usage import UsageStore  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_agent.py"))


def fake_cfg(behaviours: dict, extra: dict | None = None, **run) -> dict:
    agents = {
        name: {"type": "generic", "enabled": True, "command": sys.executable,
               "args": [sys.executable, FAKE, name, b], "strength": 1.0, "description": "fake",
               "budgets": {"five_hour": 1_000_000, "weekly": 10_000_000}, "default_cooldown_min": 60}
        for name, b in behaviours.items()
    }
    cfg = deep_merge(DEFAULT_CONFIG, {"run": {"wait_on_limits": False, **run}})
    for name, more in (extra or {}).items():
        agents[name].update(more)
    cfg["agents"] = agents
    return cfg


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = UsageStore(self.dir / "usage.json")
        self.log = self.dir / "prompts.jsonl"
        os.environ["FAKE_AGENT_LOG"] = str(self.log)
        # Global files (settings, projects, global memory) go to a temp home, never the real one.
        self._old_home = os.environ.get("AUTOAGENTS_HOME")
        os.environ["AUTOAGENTS_HOME"] = str(self.dir / "home")
        self.project = self.dir / "proj"
        self.project.mkdir()

    def tearDown(self):
        os.environ.pop("FAKE_REMEMBER", None)
        if self._old_home is None:
            os.environ.pop("AUTOAGENTS_HOME", None)
        else:
            os.environ["AUTOAGENTS_HOME"] = self._old_home
        self.tmp.cleanup()

    def prompts(self):
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]


class RoadmapColors(TempCase):
    STEPS = [{"id": 1, "title": "Easy", "level": "light", "status": "todo"},
             {"id": 2, "title": "Hard", "level": "heavy", "status": "todo"},
             {"id": 3, "title": "Old", "level": "heavy", "status": "done", "done_by": "codex"}]

    def test_plain_by_default_and_coloured_by_level_on_request(self):
        from autoagents import ui as ui_mod
        old, ui_mod._COLOR = ui_mod._COLOR, True
        try:
            state = ProjectState(self.project)
            plain = state.roadmap_text(self.STEPS)
            self.assertNotIn("", plain)  # files and prompts never get ANSI codes
            self.assertIn("[ ] 2. Hard [heavy]", plain)
            lines = state.roadmap_text(self.STEPS, color=True).splitlines()
            self.assertIn("[32mEasy", lines[0])     # light: green
            self.assertIn("[31;1mHard", lines[1])   # heavy: red bold
            self.assertIn("[90mOld", lines[2])      # done: grey, whatever the level
        finally:
            ui_mod._COLOR = old


class LimitParsing(unittest.TestCase):
    NOW = time.mktime((2026, 10, 24, 10, 0, 0, 0, 0, -1))

    def test_detects_limits(self):
        for msg in ["Claude AI usage limit reached|1792843200", "You've hit your usage limit.",
                    "429 Too Many Requests", "RESOURCE_EXHAUSTED: Quota exceeded", "5-hour limit reached ∙ resets 3pm"]:
            self.assertTrue(is_limit_error(msg), msg)
        self.assertFalse(is_limit_error("All tests passed"))
        self.assertTrue(is_auth_error("Error authenticating: IneligibleTierError"))
        self.assertTrue(is_auth_error('{"code":400,"message":"API key not valid. Please pass a valid API key."}'))

    def test_reset_formats(self):
        self.assertEqual(parse_reset("limit reached|1792843200", self.NOW), 1792843200)
        self.assertAlmostEqual(parse_reset("try again in 2 hours 30 minutes", self.NOW), self.NOW + 9000)
        self.assertAlmostEqual(parse_reset("Please retry in 23.5s", self.NOW), self.NOW + 23.5)
        self.assertEqual(parse_reset("resets 3pm", self.NOW), self.NOW + 5 * 3600)
        tomorrow_930 = time.mktime((2026, 10, 25, 9, 30, 0, 0, 0, -1))  # wall clock, DST-safe
        self.assertEqual(parse_reset("try again at 9:30 AM", self.NOW), tomorrow_930)
        self.assertIsNone(parse_reset("no time here", self.NOW))


class ReportParsing(unittest.TestCase):
    def test_report_last_block_wins(self):
        text = ('<autoagents-report>{"status":"continue"}</autoagents-report> later\n'
                '<autoagents-report>\n```json\n{"status":"done","completed_steps":[1,"2"],"summary":"ok"}\n```\n'
                '</autoagents-report>')
        r = parse_report(text)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["completed_steps"], [1, "2"])
        self.assertFalse(r["missing"])

    def test_missing_report(self):
        r = parse_report("I did stuff")
        self.assertTrue(r["missing"])
        self.assertEqual(r["status"], "continue")

    def test_compact_reset_durations(self):
        now = time.time()
        self.assertAlmostEqual(parse_reset("Individual quota reached. Resets in 42h18m8s.", now) - now,
                               42 * 3600 + 18 * 60 + 8, delta=1)
        self.assertAlmostEqual(parse_reset("try again in 1d2h", now) - now, 26 * 3600, delta=1)
        self.assertIsNone(parse_reset("try again in 3 months", now))

    def test_level_parsing(self):
        from autoagents.prompts import normalize_level
        self.assertEqual([normalize_level(x) for x in ("Medium", "hard", "light", "?", None)],
                         ["mid", "heavy", "light", None, None])

    def test_plan(self):
        p = parse_plan('x <autoagents-plan>{"complex": true, "roadmap": ["a", "b"]}</autoagents-plan>')
        self.assertTrue(p["complex"])
        self.assertIsNone(parse_plan("nothing"))


class ProviderParsing(unittest.TestCase):
    def test_claude_stream(self):
        p = ClaudeProvider("claude", {}, {})
        res = RunResult("claude")
        lines = [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.42, "resetsAt": 1792843200},
                "seven_day": {"utilization": 0.2, "resetsAt": 1793106000}}}},
            {"type": "result", "result": "final", "usage": {"input_tokens": 100, "output_tokens": 50,
                                                           "cache_read_input_tokens": 1000}},
        ]
        for ev in lines:
            p.handle_line(json.dumps(ev), res, lambda _m: None)
        self.assertEqual(res.final_text, "final")
        self.assertEqual(res.tokens, 250)
        self.assertEqual({s.window: s.utilization for s in res.snapshots}, {"five_hour": 0.42, "weekly": 0.2})

    def test_claude_rejected_is_limit(self):
        p = ClaudeProvider("claude", {}, {})
        res = RunResult("claude")
        p.handle_line(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 1792843200, "rateLimitType": "five_hour"}}), res, lambda _m: None)
        self.assertEqual(res.error_kind, "limit")
        self.assertEqual(res.limit_window, "five_hour")

    def test_codex_stream(self):
        p = CodexProvider("codex", {}, {})
        res = RunResult("codex")
        for ev in [{"type": "thread.started", "thread_id": "t1"},
                   {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
                   {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 10}}]:
            p.handle_line(json.dumps(ev), res, lambda _m: None)
        self.assertEqual((res.session_id, res.final_text, res.tokens), ("t1", "OK", 560))


    def test_antigravity_stream(self):
        from autoagents.providers import AntigravityProvider
        p = AntigravityProvider("antigravity", {"command": "agy"}, {"permission": "full"})
        payload = json.loads(p.stdin_payload("do it"))
        self.assertEqual(payload, {"event": "user", "message": {"role": "user", "content": "do it"}})
        self.assertEqual(p.build_args("agy", Path("."))[-1], "-p=")
        res = RunResult("antigravity")
        shown = []
        for ev in [
            {"event": "init", "conversation_id": "c1"},
            {"event": "step_update", "step_update": {"step_index": 3, "state": "ACTIVE", "step_type": "tool",
                                                     "tool_info": {"name": "run_command",
                                                                   "parameters": {"CommandLine": "npm test"}}}},
            {"event": "step_update", "step_update": {"step_index": 4, "state": "ACTIVE",
                                                     "step_type": "agent_response", "text_delta": "All "}},
            {"event": "step_update", "step_update": {"step_index": 4, "state": "ACTIVE",
                                                     "step_type": "agent_response", "text_delta": "done"}},
            {"event": "step_update", "step_update": {"step_index": 4, "state": "DONE", "step_type": "agent_response"}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "All done\n",
                                           "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 1000}}},
        ]:
            p.handle_line(json.dumps(ev), res, shown.append)
        self.assertEqual((res.session_id, res.final_text, res.tokens), ("c1", "All done\n", 220))
        self.assertIn("[tool] run_command: npm test", res.log)
        self.assertIn("All done", res.log)
        err = RunResult("antigravity")
        p.handle_line(json.dumps({"event": "result", "result": {"status": "ERROR", "error": "quota exhausted"}}),
                      err, shown.append)
        self.assertEqual(err.error, "quota exhausted")


class Balancing(TempCase):
    def test_prefers_headroom_and_skips_full(self):
        cfg = fake_cfg({"a": "work", "b": "work", "c": "work"})
        self.store.set_snapshot("a", "five_hour", 0.70, None, "manual")
        self.store.set_snapshot("b", "five_hour", 0.10, None, "manual")
        self.store.set_snapshot("c", "weekly", 0.97, None, "manual")
        ranked, skipped = rank_agents(cfg, self.store, {"a", "b", "c"})
        self.assertEqual([r.name for r in ranked], ["b", "a"])
        self.assertEqual(skipped[0].name, "c")

    def test_both_windows_pace_the_choice(self):
        # Same weekly usage (40%), but x's week resets tomorrow (its unused quota would be lost) and y's
        # week just started (40% already spent that early is too fast). x must win although y comes first.
        now = time.time()
        cfg = fake_cfg({"y": "work", "x": "work"})
        self.store.set_snapshot("x", "weekly", 0.40, now + 1 * 86400, "manual")
        self.store.set_snapshot("y", "weekly", 0.40, now + 6 * 86400, "manual")
        ranked, _ = rank_agents(cfg, self.store, {"x", "y"}, now=now)
        self.assertEqual([r.name for r in ranked], ["x", "y"])
        self.assertIn("week under-used", ranked[0].note)
        self.assertIn("week used too fast", ranked[1].note)
        # Without pacing only the fuller window counts: a tie, so config order (y first) decides.
        cfg["balancer"]["pacing"] = False
        ranked, _ = rank_agents(cfg, self.store, {"x", "y"}, now=now)
        self.assertEqual([r.name for r in ranked], ["y", "x"])

    def test_five_hour_pace_and_hard_limit_still_win(self):
        from autoagents.balancer import window_pace
        from autoagents.usage import WindowUsage
        now = time.time()
        # Half of the 5h window gone, 10% used: +0.4 behind its pace.
        pace, _ = window_pace(WindowUsage("five_hour", 0.10, "manual", now + 150 * 60, 0), now)
        self.assertAlmostEqual(pace, 0.4, places=2)
        self.assertEqual(window_pace(WindowUsage("five_hour", 0.0, "estimate", None, 0), now)[0], 0.0)
        self.assertEqual(window_pace(WindowUsage("weekly", 0.0, "estimate", None, 0), now)[0], 0.2)
        # However far behind its weekly pace, an agent at its 5-hour hard limit is never started.
        cfg = fake_cfg({"a": "work", "b": "work"})
        self.store.set_snapshot("a", "weekly", 0.05, now + 3600, "manual")
        self.store.set_snapshot("a", "five_hour", 0.97, now + 3600, "manual")
        ranked, skipped = rank_agents(cfg, self.store, {"a", "b"}, now=now)
        self.assertEqual([r.name for r in ranked], ["b"])

    def test_limit_with_unknown_window_does_not_block_the_week(self):
        acfg = {"budgets": {"five_hour": 2_000_000, "weekly": 20_000_000}}
        self.store.record_tokens("g", 500_000)
        self.store.learn_from_limit("g", None)  # agy: the window is not reported
        u = self.store.agent_usage("g", acfg)
        self.assertGreaterEqual(u.windows["five_hour"].utilization, 0.99)  # learned for the 5-hour window
        self.assertLess(u.windows["weekly"].utilization, 0.1)  # the week is not marked full
        # A real weekly quota smaller than the guessed 5-hour budget caps the 5-hour budget too.
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        data["agents"]["g"]["learned_budgets"] = {"weekly": 600_000}
        self.store.path.write_text(json.dumps(data), encoding="utf-8")
        u = self.store.agent_usage("g", acfg)
        self.assertAlmostEqual(u.windows["weekly"].utilization, 500_000 / 600_000, places=2)
        self.assertAlmostEqual(u.windows["five_hour"].utilization, 500_000 / 600_000, places=2)

    def test_limit_reset_tells_the_window(self):
        # agy: "Individual quota reached ... Resets in 42h18m8s." -> no window named, but 42 h = the week.
        cfg = fake_cfg({"g": "work", "b": "work"})
        orch = Orchestrator(self.project, cfg, UI(quiet=True), self.store)
        orch.create("Task")
        self.store.record_tokens("g", 400_000)
        res = RunResult(agent="g", error_kind="limit", error="Individual quota reached.",
                        limit_reset=time.time() + 42 * 3600)
        from unittest import mock
        with mock.patch.object(orch.providers["g"], "run", lambda *a, **k: res):
            orch._run_agent("g", "prompt", 1, "work")
        u = self.store.agent_usage("g", cfg["agents"]["g"])
        self.assertAlmostEqual(u.cooldown_until, time.time() + 42 * 3600, delta=60)  # not the 1 h default
        self.assertGreaterEqual(u.windows["weekly"].utilization, 1.0)
        self.assertEqual(self.store._load()["agents"]["g"]["learned_budgets"], {"weekly": 400_000})

    def busy_elsewhere(self, agent, seconds):
        """Pretend another chat runs `agent` for `seconds` (a real process, so the pid is alive)."""
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        path = Path(os.environ["AUTOAGENTS_HOME"]) / "busy" / f"{agent}.{proc.pid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"agent": agent, "pid": proc.pid, "project": "shop", "chat": 2}), encoding="utf-8")
        self.addCleanup(proc.kill)
        return proc

    def test_agent_busy_in_another_chat_is_skipped(self):
        from autoagents import busy
        self.busy_elsewhere("a", 30)
        cfg = fake_cfg({"a": "work", "b": "work"})
        ranked, skipped = rank_agents(cfg, self.store, {"a", "b"}, busy=busy.busy_agents())
        self.assertEqual([r.name for r in ranked], ["b"])
        self.assertIn("busy in another chat (shop#2)", skipped[0].reason)
        cfg["balancer"]["max_parallel"] = 2  # allowed twice at once: no longer skipped
        ranked, _ = rank_agents(cfg, self.store, {"a", "b"}, busy=busy.busy_agents())
        self.assertEqual([r.name for r in ranked], ["a", "b"])
        # A real relay picks b, and marks it busy only while its turn runs.
        cfg["balancer"]["max_parallel"] = 1
        self.assertEqual(Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Fix", planning="never"),
                         EXIT_DONE)
        self.assertEqual([p["agent"] for p in self.prompts()], ["b"])
        self.assertEqual(list(busy.busy_agents(exclude_pid=-1)), ["a"])

    def test_waits_while_the_only_agent_is_busy(self):
        self.busy_elsewhere("a", 1.5)
        cfg = fake_cfg({"a": "work"}, busy_poll_s=0.5)
        t0 = time.time()
        self.assertEqual(Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Fix", planning="never"),
                         EXIT_DONE)
        self.assertGreater(time.time() - t0, 1.0)  # it waited for the other chat instead of doubling up
        self.assertEqual([p["agent"] for p in self.prompts()], ["a"])

    def test_cooldown_and_soft_limit(self):
        cfg = fake_cfg({"a": "work", "b": "work"})
        self.store.set_cooldown("a", time.time() + 600, "usage limit reached")
        self.store.set_snapshot("b", "five_hour", 0.85, None, "manual")
        ranked, skipped = rank_agents(cfg, self.store, {"a", "b"})
        self.assertEqual([r.name for r in ranked], ["b"])
        self.assertIn("soft", ranked[0].note)

    def test_levels_and_expiry(self):
        helper = {"levels": ["light", "mid"], "strength": 2.0}
        cfg = fake_cfg({"main": "work", "helper": "work", "old": "work"},
                       {"helper": helper, "old": {"active_until": "2020-01-01"}})
        names = lambda level: [r.name for r in rank_agents(cfg, self.store, {"main", "helper", "old"}, level=level)[0]]
        self.assertEqual(names("mid"), ["helper", "main"])
        self.assertEqual(names("heavy"), ["main"])
        self.assertEqual(names(None), ["main"])  # unrated work is treated as possibly heavy
        forced = rank_agents(cfg, self.store, {"main", "helper"}, level="heavy", forced="helper")[0]
        self.assertIn("helper", [r.name for r in forced])  # an explicit --agent overrides levels

    def test_estimates_from_tokens(self):
        cfg = fake_cfg({"a": "work"})
        self.store.record_tokens("a", 500_000)
        u = self.store.agent_usage("a", cfg["agents"]["a"])
        self.assertAlmostEqual(u.windows["five_hour"].utilization, 0.5)
        self.assertEqual(u.windows["five_hour"].source, "estimate")


class Relay(TempCase):
    def orch(self, cfg):
        return Orchestrator(self.project, cfg, UI(quiet=True), self.store)

    def test_handover_after_limit(self):
        # "a" is preferred (first in order) but hits its limit; "b" must take over with the history.
        cfg = fake_cfg({"a": "limit", "b": "work"})
        code = self.orch(cfg).start("Fix the bug", planning="never")
        self.assertEqual(code, EXIT_DONE)
        prompts = self.prompts()
        self.assertEqual([p["agent"] for p in prompts], ["a", "b"])
        handover = prompts[1]["prompt"]
        self.assertIn("HANDOVER: YOU ARE TAKING OVER FROM A", handover)
        self.assertIn("created src/half_done.py", handover)  # a's partial output is passed on
        a = self.store.agent_usage("a", cfg["agents"]["a"])
        self.assertTrue(a.in_cooldown())
        self.assertAlmostEqual(a.cooldown_until, time.time() + 9000, delta=60)
        task = ProjectState(self.project).load_task()
        self.assertEqual(task["status"], "done")

    def test_roadmap_saved_and_followed(self):
        cfg = fake_cfg({"a": "work", "codex": "work"})
        code = self.orch(cfg).start("Build a feature", planning="auto")
        self.assertEqual(code, EXIT_DONE)
        state = ProjectState(self.project)
        steps = state.load_roadmap()
        self.assertEqual([s["status"] for s in steps], ["done", "done"])
        self.assertTrue(state.roadmap_md.exists())
        prompts = self.prompts()
        self.assertIn("PLANNER", prompts[0]["prompt"])
        self.assertEqual(prompts[1]["agent"], "codex")  # roadmap suggested codex for step 1
        self.assertIn("Work on roadmap step 1", prompts[1]["prompt"])
        self.assertIn("Work on roadmap step 2", prompts[2]["prompt"])
        kinds = [s["kind"] for s in state.recent_steps(50)]
        self.assertEqual(kinds, ["task", "roadmap", "turn", "turn", "done"])

    def test_mid_steps_go_to_helper_heavy_to_main(self):
        cfg = fake_cfg({"main": "work", "helper": "work"},
                       {"helper": {"levels": ["light", "mid"], "strength": 2.0, "history_chars": 600}})
        self.assertEqual(self.orch(cfg).start("Build a feature", planning="auto"), EXIT_DONE)
        agents = [p["agent"] for p in self.prompts()]
        self.assertEqual(agents, ["main", "helper", "main"])  # plan (heavy), step 1 (mid), step 2 (heavy)
        steps = ProjectState(self.project).load_roadmap()
        self.assertEqual([(s["level"], s["done_by"]) for s in steps], [("mid", "helper"), ("heavy", "main")])

    def test_helper_limit_falls_back_to_main(self):
        cfg = fake_cfg({"main": "work", "helper": "limit"},
                       {"helper": {"levels": ["light", "mid"], "strength": 2.0}})
        self.assertEqual(self.orch(cfg).start("Build a feature", planning="auto"), EXIT_DONE)
        self.assertEqual([p["agent"] for p in self.prompts()], ["main", "helper", "main", "main"])
        self.assertTrue(self.store.agent_usage("helper", cfg["agents"]["helper"]).in_cooldown())

    def test_all_limited_stops_and_resumes(self):
        cfg = fake_cfg({"a": "limit"})
        code = self.orch(cfg).start("Task", planning="never")
        self.assertEqual(code, EXIT_NO_AGENT)
        self.assertEqual(ProjectState(self.project).load_task()["status"], "waiting")
        # Later the agent is back and works: `continue` resumes with the saved handover.
        self.store.clear("a")
        cfg2 = fake_cfg({"a": "work"})
        self.assertEqual(self.orch(cfg2).resume("also add docs"), EXIT_DONE)
        last = self.prompts()[-1]["prompt"]
        self.assertIn("also add docs", last)
        self.assertIn("reached its usage limit", last)


class ChatReuse(TempCase):
    RESUMABLE = {"resume_args": ["--resume", "{id}"], "session_regex": r"SESSION: (\S+)"}

    def orch(self, cfg):
        return Orchestrator(self.project, cfg, UI(quiet=True), self.store)

    def test_agent_continues_its_own_chat_with_short_prompts(self):
        cfg = fake_cfg({"a": "work"}, {"a": self.RESUMABLE})
        self.assertEqual(self.orch(cfg).start("Build a feature", planning="auto"), EXIT_DONE)
        p = self.prompts()
        self.assertEqual(len(p), 3)  # plan, step 1, step 2
        self.assertIsNone(p[0]["resume"])
        sid = ProjectState(self.project).load_task()["sessions"]["a"]["id"]
        self.assertEqual([x["resume"] for x in p[1:]], [sid, sid])  # one chat for the whole task
        # Right after planning: the turn rules, but no history (it is all in its chat already).
        self.assertIn("REQUIRED FINAL REPORT", p[1]["prompt"])
        self.assertNotIn("CONVERSATION HISTORY", p[1]["prompt"])
        # Later turns: only what changed.
        self.assertIn("NEXT AUTOAGENTS TURN", p[2]["prompt"])
        self.assertIn("Work on roadmap step 2", p[2]["prompt"])
        self.assertLess(len(p[2]["prompt"]), len(p[1]["prompt"]))

    def test_other_agents_work_is_passed_on_when_returning(self):
        cfg = fake_cfg({"a": "work", "b": "work"}, {"a": self.RESUMABLE, "b": self.RESUMABLE})
        orch = self.orch(cfg)
        self.assertEqual(orch.start("Fix it", planning="never"), EXIT_DONE)
        state = ProjectState(self.project)
        state.add_transcript("agent", "b", "b refactored utils.py", 5)
        self.assertEqual(orch.resume("one more thing"), EXIT_DONE)
        last = self.prompts()[-1]
        self.assertEqual(last["agent"], "a")
        self.assertIsNotNone(last["resume"])
        self.assertIn("b refactored utils.py", last["prompt"])  # news from another agent
        self.assertIn("one more thing", last["prompt"])
        self.assertNotIn("a finished step", last["prompt"])  # its own output is not repeated

    def test_broken_resume_falls_back_to_a_new_chat(self):
        cfg = fake_cfg({"a": "badresume"}, {"a": self.RESUMABLE})
        orch = self.orch(cfg)
        self.assertEqual(orch.start("Fix it", planning="never"), EXIT_DONE)
        self.assertEqual(orch.resume("again"), EXIT_DONE)
        p = self.prompts()
        self.assertEqual([x["resume"] is not None for x in p], [False, True, False])
        self.assertIn("# TASK", p[2]["prompt"])  # full prompt in the new chat
        self.assertFalse(self.store.agent_usage("a", cfg["agents"]["a"]).in_cooldown())

    def test_fresh_and_turn_cap(self):
        cfg = fake_cfg({"a": "work"}, {"a": self.RESUMABLE}, session_max_turns=1)
        orch = self.orch(cfg)
        orch.start("Fix it", planning="never")
        orch.resume("more")
        self.assertIsNone(self.prompts()[-1]["resume"])  # cap of 1 turn per chat
        cfg["run"]["session_max_turns"] = 8
        orch.resume("more", fresh=True)
        self.assertIsNone(self.prompts()[-1]["resume"])


class ProjectsMemorySettings(TempCase):
    def orch(self, cfg):
        return Orchestrator(self.project, cfg, UI(quiet=True), self.store)

    def cli(self, *argv):
        from autoagents import cli
        return cli.main(["-q", *argv])

    def test_registry(self):
        from autoagents.registry import Registry
        reg = Registry()
        (self.project / "src").mkdir()
        self.assertEqual(reg.ensure(self.project), "proj")
        self.assertEqual(reg.ensure(self.project), "proj")  # no duplicate
        self.assertEqual(reg.by_path(self.project / "src"), "proj")  # sub-folders belong to it
        other = self.dir / "other" / "proj"
        other.mkdir(parents=True)
        self.assertEqual(reg.ensure(other), "proj-2")  # same folder name, unique project name
        with self.assertRaises(ValueError):
            reg.add("bad name!", self.project)
        reg.use("proj-2")
        self.assertEqual(reg.current(), "proj-2")
        reg.rename("proj-2", "web")
        self.assertEqual(reg.current(), "web")
        reg.remove("web")
        self.assertIsNone(reg.current())
        self.assertTrue(other.is_dir())  # removing only unregisters

    def test_projects_by_number(self):
        import io
        from contextlib import redirect_stdout
        from autoagents.registry import Registry
        from autoagents.shell import Session
        reg = Registry()
        shop, web = self.dir / "shop", self.dir / "web"
        shop.mkdir(), web.mkdir()
        reg.add("Shop", shop)
        time.sleep(0.01)
        reg.add("WebSite", web)
        self.assertEqual(reg.ordered(), ["Shop", "WebSite"])
        self.assertEqual([reg.resolve(r) for r in ("1", "#2", "web", "SHOP", "9", "x")],
                         ["Shop", "WebSite", "WebSite", "Shop", None, None])
        reg.use("2")
        self.assertEqual(reg.current(), "WebSite")
        self.assertEqual(reg.ordered(), ["Shop", "WebSite"])  # using one does not renumber
        # CLI: -P takes a number; chat refs take "2#1".
        Orchestrator(web, fake_cfg({"a": "work"}), UI(quiet=True), self.store).start("Web task", planning="never")
        from autoagents import cli
        for argv in (["-P", "2", "status"], ["s", "2#1"]):
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["-q", *argv])
            self.assertIn("Web task", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["-q", "p"])
        self.assertIn("*2", out.getvalue())  # the list shows numbers, current marked
        # Shell: "/p 1" switches project, "/open 2#1" opens a chat of another project.
        session = Session(web, UI(quiet=True))
        with redirect_stdout(io.StringIO()):
            session.handle("/p 1")
            self.assertEqual(session.root, shop.resolve())
            self.assertEqual(session.project_label(), "1:Shop")
            session.handle("/open 2#1")
            self.assertEqual(session.attached.load_task()["text"], "Web task")
            session.detach(quiet=True)

    def test_project_flag_selects_the_folder(self):
        self.assertEqual(self.cli("project", "add", "shop", str(self.project)), 0)
        self.assertEqual(self.cli("-P", "shop", "memory", "add", "Run tests with pytest -q"), 0)
        from autoagents.memory import project_memory
        self.assertEqual([e["text"] for e in project_memory(self.project).entries()], ["Run tests with pytest -q"])
        with self.assertRaises(SystemExit):
            self.cli("-P", "nope", "status")

    def test_agents_read_and_write_memory(self):
        from autoagents.memory import global_memory, project_memory
        global_memory().add("The user works on Windows 11", by="user")
        project_memory(self.project).add("Build with make", by="user")
        os.environ["FAKE_REMEMBER"] = json.dumps([{"scope": "project", "text": "Tests live in tests/"},
                                                  {"scope": "global", "text": "User prefers short commits"},
                                                  {"text": "Build with make"}])  # duplicate: ignored
        cfg = fake_cfg({"a": "work"})
        self.assertEqual(self.orch(cfg).start("Fix it", planning="never"), EXIT_DONE)
        prompt = self.prompts()[0]["prompt"]
        self.assertIn("The user works on Windows 11", prompt)
        self.assertIn("Build with make", prompt)
        self.assertIn('"remember"', prompt)  # the agent is told how to save memory
        self.assertEqual([e["text"] for e in project_memory(self.project).entries()],
                         ["Build with make", "Tests live in tests/"])
        self.assertIn("User prefers short commits", [e["text"] for e in global_memory().entries()])
        self.assertEqual(project_memory(self.project).entries()[1]["by"], "a")
        # A new task keeps the memory (only the task files are archived).
        os.environ.pop("FAKE_REMEMBER")
        self.orch(cfg).start("Next task", planning="never")
        self.assertIn("Tests live in tests/", self.prompts()[-1]["prompt"])

    def test_memory_in_resumed_chat_only_when_new(self):
        from autoagents.memory import project_memory
        cfg = fake_cfg({"a": "work"}, {"a": ChatReuse.RESUMABLE})
        orch = self.orch(cfg)
        project_memory(self.project).add("Old fact", by="user")
        orch.start("Fix it", planning="never")
        project_memory(self.project).add("New fact", by="user")
        orch.resume("more")
        last = self.prompts()[-1]
        self.assertIsNotNone(last["resume"])
        self.assertIn("New fact", last["prompt"])
        self.assertNotIn("Old fact", last["prompt"])  # it already has that in its chat

    def test_settings_set_get_unset(self):
        from autoagents.config import load_config
        self.assertEqual(self.cli("-C", str(self.project), "settings", "set", "run.max_turns", "7"), 0)
        self.assertEqual(self.cli("-C", str(self.project), "settings", "set", "run.max_turns", "12", "--global"), 0)
        self.assertEqual(load_config(self.project)["run"]["max_turns"], 7)  # project wins
        self.assertEqual(load_config(self.dir)["run"]["max_turns"], 12)  # other folders: global
        self.assertEqual(self.cli("-C", str(self.project), "settings", "unset", "run.max_turns"), 0)
        self.assertEqual(load_config(self.project)["run"]["max_turns"], 12)
        self.cli("-C", str(self.project), "settings", "set", "agents.codex.levels", '["light","mid"]')
        self.assertEqual(load_config(self.project)["agents"]["codex"]["levels"], ["light", "mid"])
        for bad in (("run.max_turns", "lots"), ("run.nope", "1"), ("agents.ghost.model", "x")):
            with self.assertRaises(SystemExit):
                self.cli("-C", str(self.project), "settings", "set", *bad)


class Robustness(TempCase):
    def provider(self, behaviour, **run):
        from autoagents.providers import make_provider
        cfg = fake_cfg({"a": behaviour})["agents"]["a"]
        return make_provider("a", cfg, {"permission": "full", **run})

    def test_leftover_child_holding_the_pipe_does_not_hang_the_turn(self):
        t0 = time.time()
        res = self.provider("daemon").run("go", self.project, timeout_s=60)
        self.assertLess(time.time() - t0, 15)  # used to wait for the 30 s child (or forever)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(parse_report(res.final_text)["summary"], "built it")

    def test_silent_agent_is_stopped(self):
        t0 = time.time()
        res = self.provider("hang", stall_timeout_min=0.03).run("go", self.project, timeout_s=60)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(res.error_kind, "stalled")

    def test_looping_agent_is_stopped(self):
        res = self.provider("loop", loop_repeat_limit=8).run("go", self.project, timeout_s=60)
        self.assertEqual(res.error_kind, "loop")
        self.assertIn("adb shell input tap", res.error)

    def test_processes_left_by_an_agent_are_listed_and_killed(self):
        import io
        from contextlib import redirect_stdout
        from autoagents import cli, procs
        cfg = fake_cfg({"a": "daemon"})
        Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Build", planning="never")
        state = ProjectState(self.project)
        left = procs.running(state.dir, include_agent_cli=False)
        self.assertEqual(len(left), 1)  # the "Gradle daemon" the fake agent started
        self.assertIn("python", left[0]["name"].lower())
        self.assertEqual(left[0]["agent"], "a")
        self.assertIn("python", left[0]["cmd"].lower())  # its command line is recorded
        steps = [s["kind"] for s in state.recent_steps(20)]
        self.assertIn("procs", steps)  # the turn said it left something running
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["-q", "-C", str(self.project), "ps"])
        self.assertIn(str(left[0]["pid"]), out.getvalue())
        self.assertIn("left over", out.getvalue())
        with redirect_stdout(io.StringIO()):
            cli.main(["-q", "-C", str(self.project), "kill", "all"])
        time.sleep(0.5)
        self.assertEqual(procs.running(state.dir, include_agent_cli=False), [])
        # A pid aa never recorded is refused.
        with redirect_stdout(out):
            cli.main(["-q", "-C", str(self.project), "kill", str(os.getpid())])
        self.assertIn("Not a running process started by an agent", out.getvalue())

    def test_process_description_shows_the_real_command(self):
        from autoagents.procs import describe
        claude_bash = ('"C:\\Program Files\\Git\\bin\\..\\usr\\bin\\bash.exe" -c "source /c/x/snapshot.sh 2>/dev/null '
                       '|| true && shopt -u extglob 2>/dev/null || true && eval \'adb -s emu shell \'\\"\'\\"\'pm list\'\\"\'\\"\' '
                       '| grep \\"app\\"\' < /dev/null && pwd -P >| /c/Temp/claude-ab12-cwd"')
        self.assertEqual(describe(claude_bash), "adb -s emu shell 'pm list' | grep \"app\"")
        self.assertEqual(describe('"C:\\sdk\\platform-tools\\adb.exe" -L tcp:5037 fork-server server'),
                         "adb -L tcp:5037 fork-server server")
        self.assertEqual(describe("gradlew  assembleDebug"), "gradlew assembleDebug")
        # Shell launchers that only run one script are cut down to the script (codex: pwsh -Command '...').
        self.assertEqual(describe('"C:\\\\Users\\\\U\\\\WindowsApps\\\\pwsh.exe" -Command \'adb -s X shell pm list\''),
                         "adb -s X shell pm list")
        self.assertEqual(describe('C:\\WINDOWS\\system32\\cmd.exe /d /s /c "gradlew.bat assembleDebug"'),
                         "gradlew.bat assembleDebug")
        self.assertEqual(describe("powershell -NoProfile -ExecutionPolicy Bypass -Command Get-Process adb"),
                         "Get-Process adb")
        self.assertEqual(describe('/bin/bash -lc "npm test"'), "npm test")
        self.assertEqual(describe('"C:\\Git\\bin\\bash.exe" build.sh'), "bash build.sh")  # not a -c script
        self.assertEqual(len(describe("x " * 200, 50)), 50)
        self.assertEqual(describe(""), "")

    def test_leftovers_can_be_killed_after_every_turn(self):
        from autoagents import procs
        cfg = fake_cfg({"a": "daemon"}, leftover_processes="kill")
        Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Build", planning="never")
        time.sleep(0.5)
        self.assertEqual(procs.running(ProjectState(self.project).dir, include_agent_cli=False), [])

    def test_agent_stuck_for_three_turns_is_replaced(self):
        cfg = fake_cfg({"spinner": "spin", "b": "work"})
        code = Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Fix", planning="never")
        self.assertEqual(code, EXIT_DONE)
        self.assertEqual([p["agent"] for p in self.prompts()], ["spinner", "spinner", "spinner", "b"])
        self.assertIn("no progress", self.prompts()[3]["prompt"])
        self.assertEqual(ProjectState(self.project).load_task()["avoid"], ["spinner"])

    def test_relay_hands_a_stalled_turn_to_another_agent(self):
        cfg = fake_cfg({"a": "hang", "b": "work"}, stall_timeout_min=0.03)
        code = Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Fix", planning="never")
        self.assertEqual(code, EXIT_DONE)
        self.assertEqual([p["agent"] for p in self.prompts()], ["a", "b"])
        self.assertIn("stalled", self.prompts()[1]["prompt"])  # b knows why it took over
        from autoagents import busy
        self.assertEqual(busy.busy_agents(exclude_pid=-1), {})  # nobody left marked busy


class ShutdownWhenFinished(TempCase):
    """/swf and /qqq. The real lock and shutdown calls are replaced: tests never power anything off."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from autoagents import power
        self.calls = []
        for name, result in (("schedule_shutdown", True), ("lock_android", ["phone1"]), ("cancel_shutdown", True)):
            patcher = mock.patch.object(power, name, lambda *a, _n=name, _r=result: self.calls.append(_n) or _r)
            patcher.start()
            self.addCleanup(patcher.stop)
        from autoagents.registry import Registry
        Registry().add("proj", self.project)

    def test_triggers_once_when_the_last_chat_ends(self):
        import io
        from contextlib import redirect_stdout
        from autoagents import cli, power
        Orchestrator(self.project, fake_cfg({"a": "work"}), UI(quiet=True), self.store).create("Task")
        with redirect_stdout(io.StringIO()):
            cli.main(["-q", "-C", str(self.project), "swf"])
        self.assertTrue(power.armed())
        state = ProjectState(self.project)
        # Another chat is still working: nothing happens yet.
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.kill)
        (state.dir / "runner.json").write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
        self.assertFalse(power.maybe_finish({}, lambda m: None))
        self.assertEqual(self.calls, [])
        # It ends: phones locked, PC shutdown scheduled, and only once.
        (state.dir / "runner.json").unlink()
        msgs = []
        self.assertTrue(power.maybe_finish({"shutdown_grace_s": 30}, msgs.append))
        self.assertEqual(self.calls, ["lock_android", "schedule_shutdown"])
        self.assertIn("shuts down in 30 s", msgs[-1])
        self.assertFalse(power.maybe_finish({}, lambda m: None))
        # /qqq cancels the scheduled shutdown and turns it off.
        with redirect_stdout(io.StringIO()):
            cli.main(["-q", "-C", str(self.project), "qqq"])
        self.assertEqual(self.calls[-1], "cancel_shutdown")
        self.assertFalse(power.armed())

    def test_armed_while_idle_waits_and_the_shell_watcher_triggers(self):
        import subprocess
        from autoagents import power
        Orchestrator(self.project, fake_cfg({"a": "work"}), UI(quiet=True), self.store).create("Task")
        state = ProjectState(self.project)
        power.arm(work_now=False)
        # Nothing has worked since it was armed: the periodic check (the shell) does nothing.
        self.assertFalse(power.maybe_finish({}, lambda m: None))
        # A chat starts working (even one whose own process would not check, like older code)...
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.kill)
        (state.dir / "runner.json").write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
        self.assertFalse(power.maybe_finish({}, lambda m: None))
        self.assertTrue(power.state()["seen_work"])
        # ...and ends without checking: the next periodic check triggers.
        proc.kill()
        proc.wait()
        self.assertTrue(power.maybe_finish({}, lambda m: None))
        self.assertEqual(self.calls, ["lock_android", "schedule_shutdown"])

    def test_console_handles_it_when_no_chat_works(self):
        import io
        from contextlib import redirect_stdout
        from autoagents import power
        from autoagents.shell import Notifier, Session
        notifier = Notifier(Session(self.project, UI(quiet=True)))
        notifier.check_shutdown()  # off: nothing happens
        self.assertEqual(self.calls, [])
        power.arm(work_now=False)  # /swf while nothing works
        out = io.StringIO()
        with redirect_stdout(out):
            notifier.check_shutdown()
            notifier.check_shutdown()  # only once
        self.assertEqual(self.calls, ["lock_android", "schedule_shutdown"])
        self.assertIn("shuts down in 60 s", out.getvalue())

    def test_do_it_now_when_everything_is_finished(self):
        from autoagents import power
        power.arm(work_now=False)
        self.assertFalse(power.maybe_finish({}, lambda m: None))
        self.assertTrue(power.maybe_finish({}, lambda m: None, force=True))  # "/swf" answered y
        self.assertEqual(self.calls, ["lock_android", "schedule_shutdown"])

    def test_only_the_console_triggers_it(self):
        import io
        from contextlib import redirect_stdout
        from autoagents import cli, power
        from autoagents.shell import Notifier, Session
        cfg_path = self.project / ".autoagents" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        from autoagents.config import DEFAULT_CONFIG
        agents = {n: {"enabled": False} for n in DEFAULT_CONFIG["agents"]}
        agents.update(fake_cfg({"a": "work"})["agents"])
        cfg_path.write_text(json.dumps({"agents": agents, "run": {"finish_lock_android": False}}), encoding="utf-8")
        power.arm()
        with redirect_stdout(io.StringIO()):
            cli.main(["-q", "-C", str(self.project), "run", "--no-plan", "Fix it"])
        self.assertEqual(self.calls, [])  # a chat's own process never powers the PC off
        with redirect_stdout(io.StringIO()):
            Notifier(Session(self.project, UI(quiet=True))).check_shutdown()
        self.assertEqual(self.calls, ["schedule_shutdown"])  # phone locking was turned off in settings

    def test_off_at_shell_start_and_at_quit(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from autoagents import power, shell
        power.arm()  # left on from an earlier session
        feed = iter(["/swf", "/q"])
        seen = []

        def fake_input(prompt=""):
            seen.append(power.armed())
            return next(feed)
        with mock.patch("builtins.input", fake_input), redirect_stdout(io.StringIO()):
            shell.shell(argparse.Namespace(dir=str(self.project), project=None), UI(quiet=True))
        self.assertEqual(seen[0], False)  # the shell started with it off
        self.assertFalse(power.armed())  # /q turned it off again


class WorkRules(TempCase):
    def tearDown(self):
        os.environ.pop("FAKE_DELEGATOR", None)
        os.environ.pop("FAKE_DELEGATE", None)
        super().tearDown()

    def test_rules_and_git_note_in_prompt(self):
        cfg = fake_cfg({"a": "work"})
        Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Fix it", planning="never")
        prompt = self.prompts()[0]["prompt"]
        self.assertIn("HOW YOU WORK", prompt)
        for rule in ("1. Plan if needed", "2. Delegate if needed", "3. Do the work", "4. Commit every finished feature"):
            self.assertIn(rule, prompt)
        self.assertIn("not a git repository", prompt)  # the temp project has no git
        cfg["run"]["commit"] = "off"
        cfg["run"]["rules"] = ["Only my rule."]
        Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Again", planning="never")
        prompt = self.prompts()[-1]["prompt"]
        self.assertIn("1. Only my rule.", prompt)
        self.assertIn("do NOT commit", prompt)

    def test_too_hard_step_goes_to_a_stronger_agent(self):
        # The helper ranks first on mid steps (strength 2) but gives up; the step is re-rated and main does it.
        cfg = fake_cfg({"main": "work", "helper": "weak"},
                       {"helper": {"levels": ["light", "mid"], "strength": 2.0}})
        self.assertEqual(Orchestrator(self.project, cfg, UI(quiet=True), self.store).start(
            "Build a feature", planning="auto"), EXIT_DONE)
        self.assertEqual([p["agent"] for p in self.prompts()], ["main", "helper", "main", "main"])
        steps = ProjectState(self.project).load_roadmap()
        self.assertEqual((steps[0]["level"], steps[0]["avoid"], steps[0]["done_by"]), ("heavy", ["helper"], "main"))
        self.assertIn("too hard", self.prompts()[2]["prompt"])  # main is told why it got the step
        self.assertIn('"too_hard"', self.prompts()[1]["prompt"])

    def test_too_hard_without_roadmap_and_forced_agent(self):
        cfg = fake_cfg({"weak": "weak", "main": "work"})
        code = Orchestrator(self.project, cfg, UI(quiet=True), self.store).start(
            "Fix it", planning="never", agent="weak")  # even when the user asked for it
        self.assertEqual(code, EXIT_DONE)
        self.assertEqual([p["agent"] for p in self.prompts()], ["weak", "main"])
        self.assertEqual(ProjectState(self.project).load_task()["level"], "heavy")

    def test_planning_mid_work_hands_steps_out(self):
        os.environ["FAKE_PLANNER"] = "weakish"
        os.environ["FAKE_STEPS"] = json.dumps([{"title": "Small part", "level": "light"},
                                               {"title": "Hard part", "level": "heavy"}])
        try:
            cfg = fake_cfg({"weakish": "work", "main": "work"},
                           {"weakish": {"levels": ["light", "mid"], "strength": 2.0}, "main": {"strength": 0.5}})
            # The whole task is rated light, so the helper starts; it finds it bigger, plans and says "done".
            orch = Orchestrator(self.project, cfg, UI(quiet=True), self.store)
            orch.create("Do it")
            task = orch.state.load_task()
            task["level"] = "light"
            orch.state.save_task(task)
            self.assertEqual(orch.begin(planning="never"), EXIT_DONE)
        finally:
            os.environ.pop("FAKE_PLANNER")
            os.environ.pop("FAKE_STEPS")
        # Its "done" did not close the new steps; the heavy one went to main.
        self.assertEqual([p["agent"] for p in self.prompts()], ["weakish", "weakish", "main"])
        steps = ProjectState(self.project).load_roadmap()
        self.assertEqual([(s["title"], s["done_by"]) for s in steps], [("Small part", "weakish"), ("Hard part", "main")])

    def git_project_with_remote(self):
        import subprocess
        def git(*a, cwd=self.project):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=str(cwd),
                           check=True, capture_output=True)
        remote = self.dir / "remote.git"
        git("init", "--bare", str(remote), cwd=self.dir)
        git("init", "-b", "dev")
        git("remote", "add", "origin", str(remote))
        (self.project / "a.txt").write_text("1", encoding="utf-8")
        git("add", "a.txt")
        git("commit", "-m", "first")
        git("push", "-u", "origin", "dev")
        (self.project / "a.txt").write_text("2", encoding="utf-8")
        git("commit", "-am", "feature by an agent")  # one commit not pushed yet
        return remote

    def remote_log(self, remote):
        import subprocess
        return subprocess.run(["git", "log", "--format=%s", "dev"], cwd=str(remote), capture_output=True,
                              text=True).stdout.split("\n")

    def test_aa_pushes_when_a_chat_is_done(self):
        remote = self.git_project_with_remote()
        cfg = fake_cfg({"a": "work"}, push="on_done")
        self.assertEqual(Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Fix", planning="never"),
                         EXIT_DONE)
        self.assertIn("feature by an agent", self.remote_log(remote))
        self.assertIn("never push yourself", self.prompts()[0]["prompt"])
        steps = [(s["kind"], s["summary"]) for s in ProjectState(self.project).recent_steps(20)]
        self.assertIn(("push", "pushed 1 commit(s) of dev to origin/dev"), steps)

    def test_push_is_off_by_default_and_respects_branches(self):
        remote = self.git_project_with_remote()
        Orchestrator(self.project, fake_cfg({"a": "work"}), UI(quiet=True), self.store).start("Fix", planning="never")
        self.assertNotIn("feature by an agent", self.remote_log(remote))
        cfg = fake_cfg({"a": "work"}, push="on_done", push_branches=["main"])
        Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Fix", planning="never")
        self.assertNotIn("feature by an agent", self.remote_log(remote))  # dev is not in the list

    def test_delegation_goes_to_the_named_agent(self):
        os.environ["FAKE_DELEGATOR"] = "a"
        os.environ["FAKE_DELEGATE"] = json.dumps([{"title": "Write the docs", "details": "README section",
                                                   "level": "light", "agent": "b"}])
        cfg = fake_cfg({"a": "work", "b": "work"})  # a ranks first (config order), so a starts
        code = Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Build it", planning="never")
        self.assertEqual(code, EXIT_DONE)
        self.assertEqual([p["agent"] for p in self.prompts()], ["a", "b"])  # a said done, but b had work
        self.assertIn("Work on roadmap step 1: Write the docs", self.prompts()[1]["prompt"])
        state = ProjectState(self.project)
        step = state.load_roadmap()[0]
        self.assertEqual((step["delegated_by"], step["agent"], step["level"], step["done_by"]), ("a", "b", "light", "b"))
        kinds = [(s["kind"], s["summary"]) for s in state.recent_steps(50)]
        self.assertIn(("commit", "abc1234 Add the parser"), kinds)
        self.assertIn(("delegate", "Write the docs -> b [light]"), kinds)


class Chats(TempCase):
    """Several chats per project, background workers, the inbox and the shell."""

    def use_fake_agents(self, behaviours):
        # Project settings: only fake agents (the real CLIs may be installed on this machine).
        from autoagents.config import DEFAULT_CONFIG
        agents = {name: {"enabled": False} for name in DEFAULT_CONFIG["agents"]}
        agents.update(fake_cfg(behaviours)["agents"])
        cfg_path = self.project / ".autoagents" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"agents": agents, "run": {"wait_on_limits": False}}), encoding="utf-8")

    def cli(self, *argv):
        from autoagents import cli
        return cli.main(["-q", "-C", str(self.project), *argv])

    def wait_status(self, state, statuses, timeout=30):
        from autoagents import chats
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = chats.chat_info(state)
            if info and info.status in statuses:
                return info
            time.sleep(0.2)
        self.fail(f"chat never reached {statuses}; log:\n" + state.live_log.read_text(encoding="utf-8", errors="replace")
                  if state.live_log.exists() else "no log")

    def test_each_task_is_its_own_chat(self):
        cfg = fake_cfg({"a": "work"})
        Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("First", planning="never")
        Orchestrator(self.project, cfg, UI(quiet=True), self.store).start("Second", planning="never")
        state = ProjectState(self.project)
        self.assertEqual(len(state.chat_ids()), 2)
        self.assertEqual(state.load_task()["text"], "Second")  # the newest is current
        first = ProjectState(self.project, "1")
        self.assertEqual(first.load_task()["text"], "First")
        # Continue the first chat: the second one is untouched.
        Orchestrator(self.project, cfg, UI(quiet=True), self.store, chat="1").resume("more")
        self.assertIn("more", self.prompts()[-1]["prompt"])
        self.assertIn("First", self.prompts()[-1]["prompt"])

    def test_old_layout_is_migrated(self):
        base = self.project / ".autoagents"
        (base / "history" / "20260101-000000-aaaa").mkdir(parents=True)
        (base / "history" / "20260101-000000-aaaa" / "task.json").write_text(
            json.dumps({"id": "20260101-000000-aaaa", "text": "old one", "status": "done"}), encoding="utf-8")
        (base / "task.json").write_text(json.dumps({"id": "20260202-000000-bbbb", "text": "current",
                                                    "status": "blocked"}), encoding="utf-8")
        (base / "ROADMAP.md").write_text("x", encoding="utf-8")
        state = ProjectState(self.project)
        self.assertEqual(state.chat_ids(), ["20260101-000000-aaaa", "20260202-000000-bbbb"])
        self.assertEqual(state.load_task()["text"], "current")
        self.assertTrue((state.dir / "ROADMAP.md").exists())
        self.assertFalse((base / "history").exists())

    def test_messages_sent_while_working_are_read(self):
        from autoagents import chats
        cfg = fake_cfg({"a": "work"})
        orch = Orchestrator(self.project, cfg, UI(quiet=True), self.store)
        orch.create("Fix it")
        chats.send(orch.state, "also update the docs")  # typed before / while it works
        self.assertEqual(orch.begin(planning="never"), EXIT_DONE)
        self.assertIn("also update the docs", self.prompts()[0]["prompt"])
        self.assertFalse(chats.has_inbox(orch.state))

    def test_background_chat_and_follow_up(self):
        from autoagents import chats
        self.use_fake_agents({"a": "work"})
        self.assertEqual(self.cli("run", "--bg", "--no-plan", "Fix it"), 0)
        state = ProjectState(self.project)
        info = self.wait_status(state, ("done",))
        self.assertFalse(info.running)
        self.assertIn("Task complete", state.live_log.read_text(encoding="utf-8"))
        # A follow-up typed later continues the same chat in the background.
        self.assertEqual(self.cli("continue", "--bg", "-m", "and add a test"), 0)
        self.wait_status(state, ("done",))
        time.sleep(0.3)
        self.wait_status(state, ("done",))
        self.assertIn("and add a test", self.prompts()[-1]["prompt"])
        self.assertEqual(len(state.chat_ids()), 1)

    def test_stop_a_background_chat(self):
        from autoagents import chats
        self.use_fake_agents({"a": "work"})
        state = ProjectState(self.project)
        Orchestrator(self.project, fake_cfg({"a": "work"}), UI(quiet=True), self.store).create("Long task")
        state = ProjectState(self.project)
        # A stand-in worker that just sleeps, so it is still running when we stop it.
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        (state.dir / "runner.json").write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
        self.assertTrue(chats.is_running(state))
        self.assertEqual(chats.chat_info(state).status, "working")
        self.assertTrue(chats.stop(state))
        proc.wait(timeout=10)
        self.assertEqual(chats.chat_info(state).status, "stopped")

    def test_delete_chats(self):
        import subprocess
        from autoagents import chats
        cfg = fake_cfg({"a": "work"})
        for text in ("One", "Two", "Three"):
            Orchestrator(self.project, cfg, UI(quiet=True), self.store).start(text, planning="never")
        Orchestrator(self.project, cfg, UI(quiet=True), self.store).create("Four (not started)")
        from autoagents.memory import project_memory
        project_memory(self.project).add("keep me", by="user")
        try:  # without -y it asks; no answer means no
            self.assertNotEqual(self.cli("rm", "1"), 0)
        except SystemExit:
            pass
        self.assertEqual(len(ProjectState(self.project).chat_ids()), 4)
        self.assertEqual(self.cli("rm", "2", "-y"), 0)
        texts = lambda: [ProjectState(self.project, c).load_task()["text"]  # noqa: E731
                         for c in ProjectState(self.project).chat_ids()]
        self.assertEqual(texts(), ["One", "Three", "Four (not started)"])  # renumbered
        # A working chat is protected unless --force.
        four = ProjectState(self.project, "3")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        (four.dir / "runner.json").write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.cli("rm", "3", "-y")
        self.assertEqual(self.cli("rm", "--done", "-y"), 0)
        self.assertEqual(texts(), ["Four (not started)"])
        self.assertEqual(self.cli("rm", "1", "-y", "--force"), 0)
        proc.wait(timeout=10)
        self.assertEqual(texts(), [])
        self.assertEqual([e["text"] for e in project_memory(self.project).entries()], ["keep me"])

    def test_shell_task_command_and_unknown_commands(self):
        from unittest import mock
        import io
        from contextlib import redirect_stdout
        from autoagents import chats
        from autoagents.shell import Session
        self.use_fake_agents({"a": "work"})
        session = Session(self.project, UI(quiet=True))
        spawned = []
        out = io.StringIO()
        with mock.patch.object(chats, "spawn", lambda st, a: spawned.append(a[0]) or 1), redirect_stdout(out):
            session.handle("/task add tile to quick settings to fast share files")
            session.handle("/back")
            session.handle("/tsak something")
        self.assertEqual(spawned, ["begin"])
        self.assertEqual(ProjectState(self.project).load_task()["text"], "add tile to quick settings to fast share files")
        self.assertIn("Unknown command /tsak. Did you mean /task", out.getvalue())
        self.assertNotIn("usage: aa", out.getvalue())

    def test_shell_quoted_windows_path(self):
        from autoagents.registry import Registry
        from autoagents.shell import Session
        target = self.dir / "GIT" / "Send Anyway"
        target.mkdir(parents=True)
        session = Session(self.project, UI(quiet=True))
        session.handle(f'/project add SendAnyway "{target}"')
        session.handle(f'aa project add Other "{self.project}"')  # the aa form too
        self.assertEqual(Path(Registry().get("SendAnyway")["path"]), target.resolve())
        self.assertEqual(Path(Registry().get("Other")["path"]), self.project.resolve())

    def test_new_chat_options(self):
        from autoagents.shell import split_options
        self.assertEqual(split_options("--agent claude --no-plan Fix --version"),
                         (["--agent", "claude", "--no-plan"], "Fix --version"))
        self.assertEqual(split_options("it's a task --plan"), ([], "it's a task --plan"))

    def test_shell_home_and_open_chat(self):
        from unittest import mock
        import argparse
        from autoagents import chats, shell
        self.use_fake_agents({"a": "work"})
        spawned = []
        feed = iter(["add a login page", "use JWT please", "/back", "/new second task", "/open 1", "/quit"])

        def fake_input(_prompt=""):
            try:
                return next(feed)
            except StopIteration:
                raise EOFError
        with mock.patch.object(chats, "spawn", lambda state, a: spawned.append((state.chat_id, a)) or 1), \
                mock.patch("builtins.input", fake_input):
            shell.shell(argparse.Namespace(dir=str(self.project), project=None), UI(quiet=True))
        state = ProjectState(self.project)
        first, second = state.chat_ids()
        self.assertEqual([(cid, a[0]) for cid, a in spawned], [(first, "begin"), (first, "resume"), (second, "begin")])
        self.assertEqual(ProjectState(self.project, "1").load_task()["text"], "add a login page")
        self.assertEqual(chats.drain(ProjectState(self.project, "1")), ["use JWT please"])  # went to chat 1
        self.assertEqual(state.current_chat_id(), first)  # /open 1 made it current


if __name__ == "__main__":
    unittest.main()
