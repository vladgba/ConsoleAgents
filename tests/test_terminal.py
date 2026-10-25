"""Pinned status lifecycle, local data, and editing under asynchronous output."""
import importlib
import io
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class TTY(io.StringIO):
    def isatty(self):
        return True


class TerminalTests(unittest.TestCase):
    def modules(self):
        for package in ('consoleagents', 'autoagents'):
            yield importlib.import_module(package + '.terminal')

    @contextmanager
    def display(self, module):
        output, error = TTY(), TTY()
        session = SimpleNamespace(root=Path('project'))
        with patch('sys.stdout', output), patch('sys.stderr', error), \
                patch.object(module, 'supported', return_value=True), \
                patch.object(module, 'local_status', return_value='P:2 Claude 42% · Gemini ? · GPT 8%'), \
                patch.object(module.shutil, 'get_terminal_size', return_value=os.terminal_size((40, 12))) as size:
            with module.PinnedStatus(session) as display:
                try:
                    yield display, output, error, size
                finally:
                    display.close()
                    self.assertIs(module.sys.stdout, output)
                    self.assertIs(module.sys.stderr, error)
                    self.assertFalse(display.thread.is_alive())

    def test_header_scroll_region_resize_and_cleanup(self):
        for module in self.modules():
            with self.subTest(package=module.__name__):
                with self.display(module) as (display, output, _, size):
                    self.assertIn('\033[2;12r', output.getvalue())
                    before = output.getvalue()
                    display.draw()
                    self.assertEqual(output.getvalue(), before)  # no idle redraw traffic
                    size.return_value = os.terminal_size((60, 20))
                    display.draw()
                    self.assertIn('\033[2;20r', output.getvalue())
                    size.return_value = os.terminal_size((10, 2))
                    display.draw()
                    self.assertTrue(output.getvalue().endswith('\0338'))
                    size.return_value = os.terminal_size((40, 12))
                    display.draw()
                    self.assertTrue(output.getvalue().endswith('\0338'))
                self.assertIn('\033[r', output.getvalue())
                self.assertTrue(output.getvalue().endswith('\033[?2004l\033[0m'))

    def test_editing_preserves_text_cursor_and_history_across_background_output(self):
        for module in self.modules():
            with self.subTest(package=module.__name__), self.display(module) as (display, output, _, size):
                keys = iter(['a', 'b', 'LEFT', 'X', 'END', '\r', 'UP', '\r'])
                count = 0

                def read():
                    nonlocal count
                    count += 1
                    if count == 4:
                        module.sys.stdout.write('background output\n')
                        module.sys.stderr.write('background warning\n')
                        size.return_value = os.terminal_size((25, 8))
                        display.draw()
                        self.assertEqual((display.text, display.cursor), ('ab', 1))
                    return next(keys)

                @contextmanager
                def reader():
                    yield read

                with patch.object(module, 'key_reader', reader):
                    self.assertEqual(display.input('ca project> '), 'aXb')
                    self.assertEqual(display.input('ca project> '), 'aXb')
                self.assertIn('background output', output.getvalue())
                self.assertFalse(display.editing)
                self.assertEqual(display.history, ['aXb'])

    def test_interrupt_eof_and_paste_do_not_leave_editor_enabled(self):
        for module in self.modules():
            with self.subTest(package=module.__name__), self.display(module) as (display, output, _, _):
                for key, exception in [('\x03', KeyboardInterrupt), ('\x04', EOFError)]:
                    @contextmanager
                    def reader():
                        yield lambda: key

                    with patch.object(module, 'key_reader', reader), self.assertRaises(exception):
                        display.input('ca> ')
                    self.assertFalse(display.editing)
                    self.assertTrue(output.getvalue().endswith('\033[?2004l\033[0m\n'))
                keys = iter(['PASTE', 'a', '\n', 'b', 'PASTE_END', '\r'])

                @contextmanager
                def paste_reader():
                    yield lambda: next(keys)

                with patch.object(module, 'key_reader', paste_reader):
                    self.assertEqual(display.input('ca> '), 'a b')

    def test_exception_restores_streams_and_region(self):
        for module in self.modules():
            with self.subTest(package=module.__name__), self.assertRaisesRegex(RuntimeError, 'boom'):
                with self.display(module):
                    raise RuntimeError('boom')

    def test_redirected_and_disabled_terminals_retain_plain_input(self):
        for module in self.modules():
            output = io.StringIO()
            with self.subTest(package=module.__name__), patch('sys.stdout', output), \
                    patch('builtins.input', return_value='answer') as read:
                with module.PinnedStatus(SimpleNamespace()) as display:
                    self.assertFalse(display.active)
                    self.assertEqual(display.input('prompt> '), 'answer')
                read.assert_called_once_with('prompt> ')
                self.assertEqual(output.getvalue(), '')
            with patch.dict(os.environ, {'CONSOLEAGENTS_HEADER': '0'}):
                self.assertFalse(module.supported())
            with patch.dict(os.environ, {'TERM': 'dumb'}):
                self.assertFalse(module.supported())

    def test_local_status_uses_cached_data_and_deduplicates_live_processes(self):
        for module in self.modules():
            cli = importlib.import_module(module.__package__ + '.cli')
            procs = importlib.import_module(module.__package__ + '.procs')
            project = importlib.import_module(module.__package__ + '.project')
            usage = importlib.import_module(module.__package__ + '.usage')
            config = {'agents': {'claude': {}, 'antigravity': {}, 'codex': {}, 'off': {'enabled': False}}}
            values = {
                'claude': usage.AgentUsage('claude', {'weekly': usage.WindowUsage('weekly', .42, 'manual', None, 0)}),
                'antigravity': usage.AgentUsage('antigravity', {'weekly': usage.WindowUsage('weekly', 0, 'estimate', None, 0)}),
                'codex': usage.AgentUsage('codex', {'weekly': usage.WindowUsage('weekly', .08, 'estimate', None, 10)})}
            store = Mock()
            store.agent_usage.side_effect = lambda name, _: values[name]
            store.refresh_info.return_value = {}
            base = Mock()
            base.chat_ids.return_value = ['a', 'b']
            session = SimpleNamespace(root=Path('project'), watched_roots=lambda: [Path('project')])
            with self.subTest(package=module.__name__), patch.object(cli, 'load_config', return_value=config), \
                    patch.object(cli, 'Orchestrator', side_effect=AssertionError('must not launch providers')), \
                    patch.object(usage, 'UsageStore', return_value=store), \
                    patch.object(project, 'ProjectState', return_value=base), \
                    patch.object(procs, 'load', return_value=[{'pid': 1}, {'pid': 2}]), \
                    patch.object(procs, 'table', return_value={1: 'alive'}) as table, \
                    patch.object(procs, 'alive', side_effect=lambda record, _: record['pid'] == 1):
                text = module.ANSI.sub('', module.local_status(session))
                self.assertEqual(text, 'P:1 Claude 42% · Gemini ? · GPT 8%~')
                table.assert_called_once()

    def test_clip_never_wraps_header_or_splits_sgr(self):
        for module in self.modules():
            text = '\033[38;5;208mClaude 42%\033[0m · GPT 8%'
            with self.subTest(package=module.__name__):
                for width in (1, 10, 20, 40):
                    self.assertLessEqual(module.visible_width(module.clip(text, width)), width)

    def test_narrow_header_keeps_all_primary_usage_percentages(self):
        for module in self.modules():
            with self.subTest(package=module.__name__), self.display(module) as (display, output, _, _):
                output.seek(0)
                output.truncate()
                display.status = 'P:12 Claude 100%*? · Gemini 100%~ · GPT 100%*? · helper 40%'
                display.draw()
                self.assertIn('GPT 100%*?', output.getvalue())
                self.assertEqual(display.header_rows, 2)
                self.assertIn('\033[3;12r', output.getvalue())
