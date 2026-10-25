"""Updater commands remain usable from home and an attached chat."""
import importlib
import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class ShellUpdaterTests(unittest.TestCase):
    def packages(self):
        for name in ("consoleagents", "autoagents"):
            yield (importlib.import_module(name + ".shell"),
                   importlib.import_module(name + ".updater"))

    def test_update_flags_reach_cli_from_home_and_chat(self):
        for shell, updater in self.packages():
            for attached in (None, SimpleNamespace(chat_id="open-chat")):
                with self.subTest(package=shell.__name__, attached=bool(attached)):
                    session = shell.Session(Path.cwd(), shell.UI())
                    session.attached = attached
                    status = updater.UpdateStatus(True, "main", "abc", "abc", "version",
                                                  "origin", "remote", behind=1)
                    result = updater.UpdateResult(True, "Updated successfully.")
                    out = io.StringIO()
                    with patch.object(updater, "get_app_root", return_value=Path("installation")), \
                            patch.object(updater, "is_git_repo", return_value=True), \
                            patch.object(updater, "check_update", return_value=status), \
                            patch.object(updater, "apply_update", return_value=result) as apply, \
                            patch("builtins.input", side_effect=AssertionError("unexpected prompt")), \
                            redirect_stdout(out):
                        self.assertTrue(session.handle("/update --check"))
                        apply.assert_not_called()
                        self.assertTrue(session.handle('/update --target "release/test" -y'))
                        apply.assert_called_once_with(Path("installation"), target="release/test",
                                                      allow_dirty=False)
                    self.assertIn("Checking ConsoleAgents updates", out.getvalue())
                    self.assertIn("Updated successfully", out.getvalue())
                    self.assertIs(session.attached, attached)

    def test_rollback_aliases_offer_interactive_selection_and_cancel(self):
        for shell, updater in self.packages():
            for command in ("/rollback", "/revert", "/update --rollback"):
                with self.subTest(package=shell.__name__, command=command):
                    session = shell.Session(Path.cwd(), shell.UI())
                    cp = updater.Checkpoint("cp", 1, "today", "abc", "abc", "main",
                                            "previous version", "pre_update")
                    out = io.StringIO()
                    with patch.object(updater, "get_app_root", return_value=Path("installation")), \
                            patch.object(updater, "is_git_repo", return_value=True), \
                            patch.object(updater, "get_current_commit", return_value="def"), \
                            patch.object(updater, "load_checkpoints", return_value=[cp]), \
                            patch.object(updater, "is_dirty", return_value=False), \
                            patch.object(updater, "apply_rollback",
                                         return_value=updater.RollbackResult(True, "Restored.")) as apply, \
                            patch("builtins.input", side_effect=["q", "1", "y"]), \
                            redirect_stdout(out):
                        self.assertTrue(session.handle(command))
                        apply.assert_not_called()
                        self.assertTrue(session.handle(command))
                        apply.assert_called_once_with(Path("installation"), target="abc", allow_dirty=False)
                    self.assertIn("Rollback cancelled", out.getvalue())
                    self.assertIn("Restored.", out.getvalue())

    def test_invalid_options_and_quotes_keep_shell_open(self):
        for shell, updater in self.packages():
            with self.subTest(package=shell.__name__):
                session = shell.Session(Path.cwd(), shell.UI())
                with patch.object(updater, "apply_update") as apply, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertTrue(session.handle("/update --invalid-option"))
                    self.assertTrue(session.handle('/rollback "unclosed'))
                apply.assert_not_called()

    def test_help_and_command_discovery_include_updater(self):
        for shell, _ in self.packages():
            with self.subTest(package=shell.__name__):
                session = shell.Session(Path.cwd(), shell.UI())
                for name in ("update", "rollback", "revert"):
                    self.assertIn(name, session.commands())
                    self.assertIn("/" + name, shell.HELP)
                out = io.StringIO()
                with redirect_stdout(out):
                    shell.quick_help(session.ui)
                self.assertIn("/update", out.getvalue())
                self.assertIn("/rollback", out.getvalue())
                self.assertIn("/revert", out.getvalue())
