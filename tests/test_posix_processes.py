"""Process lifecycle regressions, including Android's restricted procfs."""
import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoagents import chats, procs


class PosixProcesses(unittest.TestCase):
    @unittest.skipUnless(os.name != "nt" and Path("/proc/self/stat").is_file(), "requires POSIX procfs")
    def test_stop_reaches_child_in_separate_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = ("import subprocess,sys,time,pathlib; "
                      "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
                      "start_new_session=True); "
                      "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)")
            worker = subprocess.Popen([sys.executable, "-c", script, str(root / "child")],
                                      start_new_session=True)
            child_pid = None
            try:
                deadline = time.monotonic() + 5
                while not (root / "child").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                child_pid = int((root / "child").read_text())
                (root / "runner.json").write_text(json.dumps({"pid": worker.pid}))
                state = SimpleNamespace(dir=root, load_task=lambda: None, live_log=root / "live.log")
                with patch.dict(os.environ, {"AUTOAGENTS_HOME": str(root / "global")}):
                    self.assertTrue(chats.stop(state))
                worker.wait(timeout=5)
                deadline = time.monotonic() + 5
                while chats.pid_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(chats.pid_alive(child_pid))
            finally:
                if worker.poll() is None:
                    worker.kill()
                worker.wait(timeout=5)
                if child_pid and chats.pid_alive(child_pid):
                    os.kill(child_pid, 9)

    def test_orphan_retains_cli_group_after_parent_exit(self):
        tab = {43: procs.Proc(43, 1, "python", 110, pgid=42),
               44: procs.Proc(44, 1, "python", 90, pgid=42),
               45: procs.Proc(45, 1, "unrelated", 110, pgid=99),
               46: procs.Proc(46, 43, "helper", 111, pgid=46)}
        self.assertEqual([p.pid for p in procs.orphans_of({42}, 100, tab)], [43, 46])

    def test_stat_name_can_contain_spaces_and_parentheses(self):
        fields = ["S", "123"] + ["0"] * 17 + ["420"]
        with patch.object(Path, "read_text", return_value="456 (worker ) name) " + " ".join(fields)):
            name, parsed = procs._read_stat(456)
        self.assertEqual(name, "worker ) name")
        self.assertEqual(parsed[0], "S")
        self.assertEqual(parsed[1], "123")
        self.assertEqual(parsed[19], "420")

    def test_ps_fallback_normalizes_paths_and_skips_invalid_rows(self):
        text = "42 1 2 /data/data/com.termux/files/usr/bin/python\n43 1 - bash\n"
        with patch.object(Path, "is_file", return_value=False), patch.object(
                procs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, text)):
            tab = procs._table_posix()
        self.assertEqual(set(tab), {42})
        self.assertEqual(tab[42].name, "python")

    @unittest.skipUnless(os.name != "nt" and Path("/proc/self/stat").is_file()
                         and hasattr(os, "WNOWAIT"), "requires Linux/Android procfs and waitid")
    def test_live_child_identity_and_unreaped_zombie(self):
        started = time.time()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)", "aa-procfs-test"])
        try:
            tab = procs.table()
            self.assertEqual(tab[child.pid].ppid, os.getpid())
            self.assertGreaterEqual(tab[child.pid].created, started - 1)
            self.assertLessEqual(tab[child.pid].created, time.time() + 1)
            self.assertIn("aa-procfs-test", procs.cmdline(child.pid))
            self.assertTrue(chats.pid_alive(child.pid))
            # Observe exit without reaping: signal 0 still succeeds on this zombie.
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
            os.kill(child.pid, 0)
            self.assertFalse(chats.pid_alive(child.pid))
            self.assertNotIn(child.pid, procs.table())
        finally:
            child.wait(timeout=5)
