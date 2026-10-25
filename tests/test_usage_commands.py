import copy
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autoagents import cli, shell, usage_refresh
from autoagents.config import DEFAULT_CONFIG
from autoagents.ui import UI
from autoagents.usage import UsageStore, WindowUsage
from autoagents.usage_refresh import QuotaWindow, RefreshResult


class UsageCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"AUTOAGENTS_HOME": str(self.root / "global"), "AA_COLOR": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # ui._COLOR is decided at import time (e.g. AA_COLOR=1 inside an aa chat), so the env alone is not enough.
        self.color = patch("autoagents.ui._COLOR", False)
        self.color.start()
        self.addCleanup(self.color.stop)
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.cfg["agents"] = {n: {**self.cfg["agents"][n], "command": sys.executable} for n in ("claude", "codex")}
        self.config_patch = patch.object(cli, "load_config", return_value=self.cfg)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.store = UsageStore()

    def command(self, *argv):
        args = cli.build_parser().parse_args(["-C", str(self.root), "usage", *argv])
        out = io.StringIO()
        with redirect_stdout(out):
            code = args.func(args, UI())
        return code, out.getvalue()

    def fake_refresh(self, name, cfg, store, root):
        result = RefreshResult(name, name, "ok" if name == "claude" else "timeout",
                               "" if name == "claude" else "Account usage refresh timed out.", time.time(),
                               [QuotaWindow("five_hour", 0, time.time() + 100, "account")] if name == "claude" else [])
        store.record_refresh(name, result)
        return result

    def test_refresh_all_continues_on_failure_and_preserves_old_data(self):
        self.store.set_snapshot("codex", "weekly", .7, time.time() + 100, "codex")
        before = self.store.refresh_info("codex")["snapshots"]
        with patch.object(usage_refresh, "refresh_agent", side_effect=self.fake_refresh) as run:
            code, out = self.command("refresh")
        self.assertEqual(code, 1)
        self.assertEqual([c.args[0] for c in run.call_args_list], ["claude", "codex"])
        self.assertIn("claude: ok", out)
        self.assertIn("codex: timeout", out)
        self.assertIn("cached", out)
        self.assertEqual(self.store.refresh_info("codex")["snapshots"], before)

    def test_refresh_single_agent_and_unknown_agent_validation(self):
        with patch.object(usage_refresh, "refresh_agent", side_effect=self.fake_refresh) as run:
            code, _ = self.command("refresh", "claude")
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 1)
            with self.assertRaises(SystemExit):
                self.command("refresh", "not-an-agent")
            self.assertEqual(run.call_count, 1)
        with self.assertRaises(SystemExit):
            self.command("refresh", "claude", "50")

    def test_shell_refresh_aliases_route_to_cli_without_chat(self):
        session = shell.Session(self.root, UI())
        with patch.object(usage_refresh, "refresh_agent", side_effect=self.fake_refresh) as run, redirect_stdout(io.StringIO()):
            self.assertTrue(session.handle("/usage refresh claude"))
            self.assertTrue(session.handle("/u refresh claude"))
        self.assertEqual(run.call_count, 2)
        self.assertIn("/usage refresh [agent]", shell.HELP)

    def test_unknown_and_measured_zero_have_different_display(self):
        _, unknown = self.command()
        self.assertIn("unknown", unknown)
        self.assertNotIn("0%", unknown.split("soft limit")[0])
        self.store.set_snapshot("claude", "five_hour", 0, time.time() + 100, "claude")
        _, measured = self.command("show", "claude")
        self.assertIn("0%", measured)
        self.assertIn("just now", measured)
        compact = io.StringIO()
        with redirect_stdout(compact):
            cli.compact_usage(self.root, UI())
        self.assertIn("unknown", compact.getvalue())
        self.assertIn("0%", compact.getvalue())

    def test_estimate_stale_disabled_and_failure_labels(self):
        wu = WindowUsage("five_hour", .2, "estimate", None, 20)
        self.assertIn("est", cli.usage_cell(wu, {}))
        self.store.set_snapshot("claude", "five_hour", .52, None, "claude", now=time.time() - 20000)
        wu = self.store.agent_usage("claude", self.cfg["agents"]["claude"]).windows["five_hour"]
        self.assertIn("52% stale", cli.usage_cell(wu, self.store.refresh_info("claude")))
        info = {"disabled_windows": ["five_hour"], "quota_windows": [{"applicable": True, "resets_at": time.time() + 100}]}
        self.assertEqual(cli.usage_cell(wu, info), "disabled")
        info["quota_windows"][0]["resets_at"] = time.time() - 100
        self.assertIn("stale", cli.usage_cell(wu, info))

    def test_clear_and_reset_remove_refresh_metadata(self):
        for action in ("clear", "reset"):
            result = RefreshResult("claude", "claude", "ok", observed_at=time.time(),
                                   windows=[QuotaWindow("weekly", .2, time.time() + 100, "weekly")])
            self.store.record_refresh("claude", result)
            self.command(action, "claude")
            info = self.store.refresh_info("claude")
            self.assertNotIn("status", info)
            self.assertEqual(info["snapshots"], {})
            self.assertEqual(info["quota_windows"], [])

    def test_manual_snapshot_overrides_disabled_window(self):
        result = RefreshResult("claude", "claude", "ok", observed_at=time.time(),
                               windows=[QuotaWindow("weekly", 1, time.time() + 100, "weekly"),
                                        QuotaWindow("five_hour", None, None, "short", disabled=True)])
        self.store.record_refresh("claude", result)
        self.command("set", "claude", "20")
        self.assertNotIn("five_hour", self.store.refresh_info("claude")["disabled_windows"])

    def test_partial_response_reports_warning_and_success_exit(self):
        def partial(name, cfg, store, root):
            result = self.fake_refresh("claude", cfg, store, root)
            result.status, result.message = "partial", "Additional quota periods retained."
            store.record_refresh(name, result)
            return result
        with patch.object(usage_refresh, "refresh_agent", side_effect=partial):
            code, out = self.command("refresh", "claude")
        self.assertEqual(code, 0)
        self.assertIn("partial", out)
        self.assertIn("Additional quota periods", out)
