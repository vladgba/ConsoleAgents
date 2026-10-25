import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from autoagents import usage_refresh as refresh
from autoagents.usage import UsageStore

FIXTURES = Path(__file__).with_name("fixtures")


def fixture(name):
    return json.loads((FIXTURES / f"quota_{name}.json").read_text())


class QuotaParsing(unittest.TestCase):
    def test_claude_account_and_model_limits(self):
        result = refresh.RefreshResult("claude", "claude", "ok", observed_at=123,
                                       windows=refresh.parse_claude(fixture("claude")))
        self.assertEqual(result.snapshots["weekly"]["utilization"], .4)
        self.assertEqual(result.snapshots["five_hour"]["utilization"], .52)
        self.assertEqual(len(result.windows), 3)
        result.windows = refresh.parse_claude(fixture("claude"), "claude-sonnet-5-5")
        self.assertEqual(result.snapshots["weekly"]["utilization"], .75)
        self.assertEqual(result.snapshots["weekly"]["resets_at"], 1893931200)
        self.assertEqual(result.snapshots["weekly"]["observed_at"], 123)

    def test_invalid_numbers_are_not_zero(self):
        for value in (None, True, "0", float("nan"), float("inf"), -1, 101):
            self.assertEqual(refresh.parse_claude({"five_hour": {"utilization": value}}), [])
        self.assertIsNone(refresh._timestamp("2030-01-01T00:00:00"))

    def test_codex_exact_periods_and_unrelated_pools(self):
        windows = refresh.parse_codex(fixture("codex"))
        self.assertEqual([(w.window, w.utilization) for w in windows if w.applicable], [("five_hour", .1), ("weekly", .2)])
        self.assertEqual(windows[-1].scope, "unrelated")
        self.assertFalse(windows[-1].applicable)
        data = fixture("codex")
        data["rateLimits"]["primary"]["windowDurationMins"] = 15
        self.assertEqual(refresh.parse_codex(data)[0].window, "15m")

    def test_agy_remaining_is_converted_and_shared_pool_selected(self):
        windows = refresh.parse_antigravity(fixture("agy"), "claude-sonnet-5-5-medium")
        selected = [w for w in windows if w.applicable]
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0].utilization, 1)
        self.assertTrue(selected[1].disabled)
        self.assertIsNone(selected[1].utilization)
        gemini = [w for w in refresh.parse_antigravity(fixture("agy"), "Gemini 3.8 Flash (Medium)") if w.applicable]
        self.assertAlmostEqual(gemini[0].utilization, .05)
        argon = [w for w in refresh.parse_antigravity(fixture("agy"), "gemini-4-argon-medium") if w.applicable]
        self.assertEqual(len(argon), 1)
        self.assertEqual(argon[0].utilization, 0)

    def test_unknown_default_does_not_guess_pool(self):
        self.assertFalse(any(w.applicable for w in refresh.parse_antigravity(fixture("agy"), None)))
        with self.assertRaises(refresh.RefreshError):
            refresh.parse_antigravity({"num_turns": 1}, "gemini-flash")


class QuotaRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.store = UsageStore(self.cwd / "usage.json")

    def run_claude(self, **kwargs):
        return refresh.refresh_agent("claude", {"type": "claude"}, self.store, self.cwd, **kwargs)

    def test_success_records_real_zero_and_freshness(self):
        data = {"five_hour": {"utilization": 0, "resets_at": "2030-01-01T00:00:00Z"}}
        with patch.object(refresh.shutil, "which", return_value="claude"), patch.object(refresh, "_claude_fetch", return_value=data):
            result = self.run_claude()
        self.assertEqual(result.status, "ok")
        saved = self.store._load()["agents"]["claude"]
        self.assertEqual(saved["snapshots"]["five_hour"]["utilization"], 0)
        self.assertEqual(saved["snapshots"]["five_hour"]["source"], "claude")
        self.assertGreater(self.store.refresh_info("claude")["checked_at"], 0)

    def test_failed_fetch_preserves_snapshot_and_redacts_errors(self):
        errors = [(TimeoutError("SECRET"), "timeout"),
                  (urllib.error.HTTPError("url", 401, "SECRET", {}, None), "auth"),
                  (ValueError("SECRET"), "error"),
                  (urllib.error.URLError(TimeoutError("SECRET")), "timeout")]
        for error, status in errors:
            self.store.set_snapshot("claude", "weekly", .7, None, "manual", now=100)
            before = self.store._load()["agents"]["claude"]["snapshots"]
            with patch.object(refresh.shutil, "which", return_value="claude"), patch.object(refresh, "_claude_fetch", side_effect=error):
                result = self.run_claude()
            self.assertEqual(result.status, status)
            saved = self.store._load()
            self.assertEqual(saved["agents"]["claude"]["snapshots"], before)
            self.assertNotIn("SECRET", json.dumps(saved))

    def test_unsupported_windows_retained_without_false_mapping(self):
        data = {"rateLimits": {"primary": {"usedPercent": 30, "windowDurationMins": 15}}}
        with patch.object(refresh.shutil, "which", return_value="codex"), patch.object(refresh, "_codex_rpc", return_value=data):
            result = refresh.refresh_agent("codex", {"type": "codex"}, self.store, self.cwd)
        self.assertEqual(result.status, "unsupported")
        info = self.store.refresh_info("codex")
        self.assertEqual(info["quota_windows"][0]["window"], "15m")
        self.assertEqual(self.store._load()["agents"]["codex"]["snapshots"], {})

    def test_disabled_window_clears_stale_short_limit(self):
        self.store.set_snapshot("agy-claude", "five_hour", .8, None, "antigravity")
        with patch.object(refresh.shutil, "which", return_value="agy"), patch.object(refresh, "_antigravity_fetch", return_value=fixture("agy")):
            result = refresh.refresh_agent("agy-claude", {"type": "antigravity", "model": "claude-sonnet-5-5-medium"}, self.store, self.cwd)
        self.assertEqual(result.status, "ok")
        saved = self.store._load()["agents"]["agy-claude"]
        self.assertEqual(saved["disabled_windows"], ["five_hour"])
        self.assertNotIn("five_hour", saved["snapshots"])
        self.assertEqual(saved["snapshots"]["weekly"]["utilization"], 1)

    def test_proot_wrapper_reads_correct_home_without_running_model(self):
        wrapper = self.cwd / "claude"
        wrapper.write_text('exec proot-distro login --user agent --work-dir "$PWD" debian -- /home/agent/.local/bin/claude "$@"\n')
        with patch.dict(os.environ, {}, clear=True), patch.object(refresh, "_run_json", return_value={"claudeAiOauth": {}}) as run:
            refresh._claude_credentials({}, str(wrapper), self.cwd, 5)
        self.assertEqual(run.call_args.args[0], ["proot-distro", "login", "--user", "agent", "debian", "--", "cat", "/home/agent/.claude/.credentials.json"])

    def test_old_agy_never_receives_usage_as_prompt(self):
        with patch.object(refresh, "_run_text", return_value="1.1.0"), patch.object(refresh, "_run_json") as run:
            with self.assertRaises(refresh.RefreshError):
                refresh._antigravity_fetch("agy", self.cwd, 5)
        run.assert_not_called()

    def process(self, script):
        return subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                start_new_session=os.name != "nt")

    def test_codex_rpc_only_initializes_and_reads_quota(self):
        log = self.cwd / "methods.json"
        script = ("import sys,json,pathlib; methods=[]\n"
                  "for line in sys.stdin:\n"
                  " d=json.loads(line); methods.append(d['method'])\n"
                  " if d.get('id')==1: print(json.dumps({'id':1,'result':{}}),flush=True)\n"
                  f" if d.get('id')==2: pathlib.Path({str(log)!r}).write_text(json.dumps(methods)); print(json.dumps({{'id':2,'result':{fixture('codex')!r}}}),flush=True)\n")
        with patch.object(refresh, "_popen", side_effect=lambda *a: self.process(script)):
            result = refresh._codex_rpc("codex", self.cwd, 5)
        self.assertEqual(result, fixture("codex"))
        self.assertEqual(json.loads(log.read_text()), ["initialize", "initialized", "account/rateLimits/read"])

    def test_codex_timeout_and_auth_errors_are_sanitized(self):
        with patch.object(refresh, "_popen", side_effect=lambda *a: self.process("import time; time.sleep(30)")):
            with self.assertRaises(TimeoutError):
                refresh._codex_rpc("codex", self.cwd, .1)
        script = "import json; input(); print(json.dumps({'id':1,'error':{'message':'unauthorized SECRET'}}),flush=True); input()"
        with patch.object(refresh, "_popen", side_effect=lambda *a: self.process(script)):
            with self.assertRaises(refresh.RefreshError) as caught:
                refresh._codex_rpc("codex", self.cwd, 5)
        self.assertEqual(caught.exception.status, "auth")
        self.assertNotIn("SECRET", str(caught.exception))
