"""Unit tests for the updater and rollback engine."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from consoleagents import updater


class TestUpdater(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.app_dir = self.root / "app"
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.remote_dir = self.root / "remote.git"

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except OSError:
            pass

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        target = cwd or self.app_dir
        r = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git command failed: git {' '.join(args)}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def _setup_git_with_remote(self) -> None:
        # Bare remote
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote_dir)], check=True, capture_output=True)
        # App repo
        self._git("init", "-b", "main")
        self._git("remote", "add", "origin", str(self.remote_dir))
        (self.app_dir / "ca.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (self.app_dir / "version.txt").write_text("v1.0.0\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "Initial commit v1.0.0")
        self._git("push", "-u", "origin", "main")

    def test_get_app_root_detection(self):
        # Override via env
        os.environ["CONSOLEAGENTS_APP_ROOT"] = str(self.app_dir)
        try:
            self.assertEqual(updater.get_app_root(), self.app_dir)
        finally:
            del os.environ["CONSOLEAGENTS_APP_ROOT"]

        # Detected via ca.py presence in start_path
        (self.app_dir / "ca.py").write_text("", encoding="utf-8")
        nested = self.app_dir / "sub" / "dir"
        nested.mkdir(parents=True, exist_ok=True)
        self.assertEqual(updater.get_app_root(start_path=nested), self.app_dir)

    def test_not_a_git_repo(self):
        self.assertFalse(updater.is_git_repo(self.app_dir))
        status = updater.check_update(self.app_dir, fetch=False)
        self.assertFalse(status.is_repo)
        self.assertIn("Not a git repository", status.error or "")

        up_res = updater.apply_update(self.app_dir)
        self.assertFalse(up_res.ok)

        rb_res = updater.apply_rollback(self.app_dir)
        self.assertFalse(rb_res.ok)

    def test_check_update_clean_and_up_to_date(self):
        self._setup_git_with_remote()
        self.assertTrue(updater.is_git_repo(self.app_dir))
        self.assertFalse(updater.is_dirty(self.app_dir))

        status = updater.check_update(self.app_dir, fetch=True)
        self.assertTrue(status.is_repo)
        self.assertEqual(status.branch, "main")
        self.assertEqual(status.behind, 0)
        self.assertEqual(status.ahead, 0)
        self.assertFalse(status.has_update)
        self.assertFalse(status.dirty)
        self.assertIsNone(status.error)

    def test_dirty_working_directory_detection(self):
        self._setup_git_with_remote()
        self.assertFalse(updater.is_dirty(self.app_dir))

        (self.app_dir / "version.txt").write_text("v1.0.1-dirty\n", encoding="utf-8")
        self.assertTrue(updater.is_dirty(self.app_dir))

        status = updater.check_update(self.app_dir, fetch=False)
        self.assertTrue(status.dirty)

        # Update should fail if dirty unless allow_dirty=True
        res = updater.apply_update(self.app_dir)
        self.assertFalse(res.ok)
        self.assertIn("uncommitted changes", res.message)

    def test_checkpoints_save_and_load(self):
        self._setup_git_with_remote()
        checkpoints = updater.load_checkpoints(self.app_dir)
        self.assertEqual(len(checkpoints), 0)

        cp1 = updater.save_checkpoint(self.app_dir, action="pre_update", target="v1.1")
        self.assertEqual(cp1.branch, "main")
        self.assertEqual(cp1.action, "pre_update")
        self.assertEqual(cp1.target, "v1.1")
        self.assertEqual(cp1.message, "Initial commit v1.0.0")

        loaded = updater.load_checkpoints(self.app_dir)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, cp1.id)
        self.assertEqual(loaded[0].commit, cp1.commit)

    def test_apply_update_and_check_incoming_commits(self):
        self._setup_git_with_remote()
        c1 = updater.get_current_commit(self.app_dir)

        # Create a second clone to push an update to remote
        clone_dir = self.root / "other_clone"
        subprocess.run(["git", "clone", "-b", "main", str(self.remote_dir), str(clone_dir)], check=True, capture_output=True)
        (clone_dir / "version.txt").write_text("v2.0.0\n", encoding="utf-8")
        self._git("commit", "-am", "Release v2.0.0", cwd=clone_dir)
        self._git("push", "origin", "main", cwd=clone_dir)

        # Check update on app_dir
        status = updater.check_update(self.app_dir, fetch=True)
        self.assertTrue(status.has_update)
        self.assertEqual(status.behind, 1)
        self.assertEqual(len(status.incoming_commits), 1)
        self.assertEqual(status.incoming_commits[0]["subject"], "Release v2.0.0")

        # Apply update
        up_res = updater.apply_update(self.app_dir)
        self.assertTrue(up_res.ok)
        self.assertIn("Updated successfully", up_res.message)
        self.assertIsNotNone(up_res.checkpoint)
        self.assertEqual(up_res.previous_commit, c1)

        c2 = updater.get_current_commit(self.app_dir)
        self.assertNotEqual(c1, c2)
        self.assertEqual((self.app_dir / "version.txt").read_text(encoding="utf-8").strip(), "v2.0.0")

        # Now check rollback
        rb_res = updater.apply_rollback(self.app_dir)
        self.assertTrue(rb_res.ok)
        self.assertIn("Rolled back", rb_res.message)
        self.assertEqual(updater.get_current_commit(self.app_dir), c1)
        self.assertEqual((self.app_dir / "version.txt").read_text(encoding="utf-8").strip(), "v1.0.0")

    def test_rollback_by_index_and_commit(self):
        self._setup_git_with_remote()
        c1 = updater.get_current_commit(self.app_dir)
        cp1 = updater.save_checkpoint(self.app_dir, action="pre_update")

        (self.app_dir / "version.txt").write_text("v1.1.0\n", encoding="utf-8")
        self._git("commit", "-am", "Commit 2")
        c2 = updater.get_current_commit(self.app_dir)
        cp2 = updater.save_checkpoint(self.app_dir, action="pre_update")

        (self.app_dir / "version.txt").write_text("v1.2.0\n", encoding="utf-8")
        self._git("commit", "-am", "Commit 3")
        c3 = updater.get_current_commit(self.app_dir)

        # Rollback using checkpoint 2 (which is c1, because cp2 was recorded at c2)
        # Most recent is cp2 (c2), next is cp1 (c1).
        # Target "2" should choose cp1 (c1)
        res = updater.apply_rollback(self.app_dir, target="2")
        self.assertTrue(res.ok)
        self.assertEqual(updater.get_current_commit(self.app_dir), c1)

        # Rollback by explicit hash to c2
        res2 = updater.apply_rollback(self.app_dir, target=c2)
        self.assertTrue(res2.ok)
        self.assertEqual(updater.get_current_commit(self.app_dir), c2)


class TestCLIUpdater(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.app_dir = self.root / "app"
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.remote_dir = self.root / "remote.git"

        # Bare remote
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote_dir)], check=True, capture_output=True)
        # App repo
        self._git("init", "-b", "main")
        self._git("remote", "add", "origin", str(self.remote_dir))
        (self.app_dir / "ca.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (self.app_dir / "version.txt").write_text("v1.0.0\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "Initial commit v1.0.0")
        self._git("push", "-u", "origin", "main")

        # Set env so CLI operates on self.app_dir
        os.environ["CONSOLEAGENTS_APP_ROOT"] = str(self.app_dir)

    def tearDown(self):
        if "CONSOLEAGENTS_APP_ROOT" in os.environ:
            del os.environ["CONSOLEAGENTS_APP_ROOT"]
        try:
            self.temp_dir.cleanup()
        except OSError:
            pass

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        target = cwd or self.app_dir
        r = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git command failed: git {' '.join(args)}\nstdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def test_cli_update_check(self):
        from consoleagents.cli import main
        code = main(["update", "--check"])
        self.assertEqual(code, 0)

    def test_cli_update_apply(self):
        from consoleagents.cli import main
        c1 = updater.get_current_commit(self.app_dir)

        # Push update to remote via clone
        clone_dir = self.root / "other_clone"
        subprocess.run(["git", "clone", "-b", "main", str(self.remote_dir), str(clone_dir)], check=True, capture_output=True)
        (clone_dir / "version.txt").write_text("v1.0.1\n", encoding="utf-8")
        self._git("commit", "-am", "Release v1.0.1", cwd=clone_dir)
        self._git("push", "origin", "main", cwd=clone_dir)

        # Run ca update with -y
        code = main(["update", "-y"])
        self.assertEqual(code, 0)
        c2 = updater.get_current_commit(self.app_dir)
        self.assertNotEqual(c1, c2)
        self.assertEqual((self.app_dir / "version.txt").read_text(encoding="utf-8").strip(), "v1.0.1")

    def test_cli_rollback_list_and_apply(self):
        from unittest.mock import patch
        from consoleagents.cli import main
        c1 = updater.get_current_commit(self.app_dir)
        cp1 = updater.save_checkpoint(self.app_dir, action="pre_update")

        (self.app_dir / "version.txt").write_text("v1.1.0\n", encoding="utf-8")
        self._git("commit", "-am", "Update 2")
        c2 = updater.get_current_commit(self.app_dir)
        cp2 = updater.save_checkpoint(self.app_dir, action="pre_update")

        # Test listing checkpoints
        code = main(["rollback", "--list"])
        self.assertEqual(code, 0)

        # Test rollback by index with -y
        # rev_cp: [cp2, cp1]. Index 2 is cp1 (c1)
        code = main(["rollback", "2", "-y"])
        self.assertEqual(code, 0)
        self.assertEqual(updater.get_current_commit(self.app_dir), c1)

    def test_cli_rollback_interactive(self):
        from unittest.mock import patch
        from consoleagents.cli import main
        c1 = updater.get_current_commit(self.app_dir)
        cp1 = updater.save_checkpoint(self.app_dir, action="pre_update")

        (self.app_dir / "version.txt").write_text("v1.1.0\n", encoding="utf-8")
        self._git("commit", "-am", "Update 2")

        # Mock interactive selection: choose checkpoint 1, then confirm with 'y'
        with patch("builtins.input", side_effect=["1", "y"]):
            code = main(["rollback"])
            self.assertEqual(code, 0)
            self.assertEqual(updater.get_current_commit(self.app_dir), c1)

    def test_cli_rollback_cancel(self):
        from unittest.mock import patch
        from consoleagents.cli import main
        c1 = updater.get_current_commit(self.app_dir)
        cp1 = updater.save_checkpoint(self.app_dir, action="pre_update")

        (self.app_dir / "version.txt").write_text("v1.1.0\n", encoding="utf-8")
        self._git("commit", "-am", "Update 2")
        c2 = updater.get_current_commit(self.app_dir)

        # User chooses 'q' to cancel
        with patch("builtins.input", side_effect=["q"]):
            code = main(["rollback"])
            self.assertEqual(code, 0)
            # Working tree commit should remain at c2
            self.assertEqual(updater.get_current_commit(self.app_dir), c2)


if __name__ == "__main__":
    unittest.main()
