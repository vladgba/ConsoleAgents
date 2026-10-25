"""Exercise real echo/canonical restoration and editor redraw through a POSIX PTY."""
import os
import signal
import subprocess
import sys
import time
import unittest


@unittest.skipIf(os.name == 'nt', 'requires a POSIX pseudoterminal')
class TerminalPTYTests(unittest.TestCase):
    def exercise(self, package, interrupt=False):
        import fcntl
        import pty
        import select
        import struct
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 12, 40, 0, 0))
        original = termios.tcgetattr(slave)
        code = f'''
import threading, time
from types import SimpleNamespace
from {package}.terminal import PinnedStatus
import {package}.terminal as terminal
terminal.local_status = lambda _: 'P:2 Claude 42% · Gemini ? · GPT 8%'
with PinnedStatus(SimpleNamespace()) as display:
    def background():
        time.sleep(.2)
        print('ASYNC OUTPUT', flush=True)
    worker = threading.Thread(target=background)
    worker.start()
    try:
        value = display.input('ca> ')
    except KeyboardInterrupt:
        value = 'INTERRUPTED'
    worker.join()
print('RESULT=' + value, flush=True)
'''
        process = subprocess.Popen([sys.executable, '-c', code], stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, 'TERM': 'xterm-256color', 'NO_COLOR': '1',
                                        'CONSOLEAGENTS_HEADER': '1'})
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        output = bytearray()

        def until(marker):
            deadline = time.monotonic() + 8
            while marker not in output:
                self.assertLess(time.monotonic(), deadline, output.decode(errors='replace'))
                if select.select([master], [], [], .1)[0]:
                    output.extend(os.read(master, 65536))

        until(b'\x1b[?2004h')
        os.write(master, b'ab')
        until(b'ASYNC OUTPUT')
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 18, 60, 0, 0))
        until(b'\x1b[2;18r')
        if interrupt:
            process.send_signal(signal.SIGINT)
            until(b'RESULT=INTERRUPTED')
        else:
            # Insert in the middle after background output and a terminal resize.
            os.write(master, b'\x1b[DX\r')
            until(b'RESULT=aXb')
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(termios.tcgetattr(slave), original)
        self.assertIn(b'\x1b[r', output)
        self.assertIn(b'\x1b[?2004l', output)

    def test_typing_survives_background_output_and_resize(self):
        for package in ('consoleagents', 'autoagents'):
            with self.subTest(package=package):
                self.exercise(package)

    def test_ctrl_c_restores_terminal_modes(self):
        for package in ('consoleagents', 'autoagents'):
            with self.subTest(package=package):
                self.exercise(package, interrupt=True)
