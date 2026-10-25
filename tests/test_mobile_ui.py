"""Presentation behaves consistently in the current and legacy packages."""
import importlib
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class MobileUITests(unittest.TestCase):
    def modules(self):
        for package in ('consoleagents', 'autoagents'):
            yield tuple(importlib.import_module(package + '.' + name)
                        for name in ('ui', 'cli', 'shell'))

    def capture(self, fn):
        out = io.StringIO()
        with redirect_stdout(out):
            fn()
        return out.getvalue()

    def assert_fits(self, ui, output, width):
        for line in output.splitlines():
            self.assertLessEqual(ui.visible_width(line), width, repr(line))

    def test_names_colors_and_progress(self):
        for ui, _, _ in self.modules():
            with self.subTest(package=ui.__name__), patch.object(ui, '_COLOR', True):
                for name, label, color in [('claude', 'Claude', '38;5;208'),
                                           ('antigravity', 'Gemini', '34'), ('codex', 'GPT', '90')]:
                    self.assertEqual(ui.agent_tag(name), f'\033[1;{color}m{label}\033[0m')
                    out = self.capture(lambda: ui.UI().progress_printer(name)('working'))
                    self.assertIn(f'\033[{color}m0:00 ', out)
                    self.assertNotIn('[', ui.ANSI.sub('', out))
                    self.assertNotIn(label, out)
                self.assertIn('custom-provider', ui.agent_tag('custom-provider'))
            with patch.object(ui, '_COLOR', False):
                self.assertEqual(ui.agent_tag('codex'), 'GPT')

    def test_ansi_wrap_preserves_text_styles_and_unicode_cell_width(self):
        for ui, _, _ in self.modules():
            with self.subTest(package=ui.__name__):
                text = '\033[38;5;208mClaude 界e\u0301 long word\033[0m\nend'
                lines = ui.wrap_ansi(text, 9)
                self.assert_fits(ui, '\n'.join(lines), 9)
                self.assertEqual(''.join(ui.ANSI.sub('', line) for line in lines),
                                 ui.ANSI.sub('', text).replace('\n', ''))
                self.assertTrue(lines[0].endswith('\033[0m'))
                self.assertTrue(lines[1].startswith('\033[38;5;208m'))
                self.assertEqual(ui.visible_width('界e\u0301'), 3)
                self.assertEqual(ui.wrap_ansi('界', 1), ['?'])

    def test_width_resize_wrapping_and_headers(self):
        for ui, _, _ in self.modules():
            view = ui.UI()
            for columns in (20, 40, 60):
                with self.subTest(package=ui.__name__, columns=columns), \
                        patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((columns, 24))):
                    out = self.capture(lambda: (view.header('Active chats'), view.line('word ' * 30)))
                    self.assertTrue(view.narrow)
                    self.assert_fits(ui, out, columns - 1)
            with patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((120, 24))):
                self.assertFalse(view.narrow)
                self.assertEqual(self.capture(lambda: view.line('x' * 160)), 'x' * 160 + '\n')

    def test_tables_compact_cards_and_desktop_fields(self):
        for ui, _, _ in self.modules():
            headers = ['pid', 'name', 'memory', 'command']
            rows = [['123', 'python', '42 MB', 'build ' + 'long task ' * 12]]
            with self.subTest(package=ui.__name__), patch.object(ui.shutil, 'get_terminal_size',
                    return_value=os.terminal_size((40, 24))):
                out = self.capture(lambda: ui.UI().table(headers, rows, compact=[0, 1, 3]))
                self.assertNotIn('memory', out)
                self.assertIn('123', out)
                self.assertIn('long task', out)
                self.assert_fits(ui, out, 39)
                detailed = self.capture(lambda: ui.UI().table(headers, rows))
                self.assertIn('memory', detailed)
                self.assert_fits(ui, detailed, 39)
            with patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((160, 24))):
                self.assertIn('memory', self.capture(lambda: ui.UI().table(headers, rows, compact=[0, 1, 3])))

    def test_mobile_usage_keeps_unknown_estimated_and_stale_distinct(self):
        for ui, cli, _ in self.modules():
            wu = SimpleNamespace(window='weekly', utilization=0, source='estimate', tokens=0)
            with self.subTest(package=ui.__name__), patch.object(ui, '_COLOR', False):
                self.assertEqual(cli.usage_cell(wu, {}, short=True, compact=True), 'unknown')
                wu.source = 'manual'
                self.assertEqual(cli.usage_cell(wu, {}, short=True, compact=True), '0%')
                wu.source, wu.tokens, wu.utilization = 'estimate', 10, .25
                self.assertEqual(cli.usage_cell(wu, {}, short=True, compact=True), '25% est')
                wu.tokens = 0
                info = {'snapshots': {'weekly': {'utilization': .5}}}
                self.assertEqual(cli.usage_cell(wu, info, short=True, compact=True), '50% stale')
                wu.source = 'account'
                self.assertIn('cached', cli.usage_cell(wu, {'status': 'timeout'}, short=True, compact=True))

    def test_quick_help_fits_phone_and_desktop(self):
        for ui, _, shell in self.modules():
            for columns in (40, 60, 100):
                with self.subTest(package=ui.__name__, columns=columns), \
                        patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((columns, 24))):
                    out = self.capture(lambda: shell.quick_help(ui.UI()))
                    self.assert_fits(ui, out, columns - 1)
                    for command in ('/update', '/rollback', '/revert', '/help'):
                        self.assertIn(command, out)

    def test_process_command_preserves_details_on_request(self):
        for ui, cli, _ in self.modules():
            procs = importlib.import_module(ui.__package__ + '.procs')
            st = SimpleNamespace(root=Path('demo'), number=lambda: 2)
            record = {'pid': 123, 'name': 'python', 'agent': 'codex', 'cmd': 'python build.py'}
            with self.subTest(package=ui.__name__), patch.object(ui.shutil, 'get_terminal_size',
                    return_value=os.terminal_size((40, 24))), \
                    patch.object(cli, '_running_procs', return_value=[(st, record)]), \
                    patch.object(cli, '_project_name', return_value='demo'), \
                    patch.object(cli.chats, 'is_running', return_value=False), \
                    patch.object(procs, 'details', return_value={123: {'mem': 42000000}}):
                parser = cli.build_parser()
                out = self.capture(lambda: cli.cmd_ps(parser.parse_args(['ps']), ui.UI()))
                self.assertNotIn('memory:', out)
                self.assert_fits(ui, out, 39)
                out = self.capture(lambda: cli.cmd_ps(parser.parse_args(['ps', '--details']), ui.UI()))
                self.assertIn('memory:', out)
                self.assertIn('GPT', out)
                self.assert_fits(ui, out, 39)

    def test_shell_chat_flags_reach_parser(self):
        for ui, _, shell in self.modules():
            session = shell.Session(Path.cwd(), ui.UI())
            with self.subTest(package=ui.__name__), patch.object(session, 'run_argv') as run:
                self.assertTrue(session.handle('/ls --details'))
                run.assert_called_with(['chats', '--details'])
                self.assertTrue(session.handle('/chats all --details'))
                run.assert_called_with(['chats', '--all', '--details'])

    def test_chat_layout_wraps_task_and_retains_timestamp_in_details(self):
        for ui, cli, _ in self.modules():
            task = 'Review this complete task description ' * 4 + 'END'
            chat = SimpleNamespace(chat_id='one', number=1, updated=1, status='working',
                                   agent='antigravity', text=task)
            with self.subTest(package=ui.__name__), patch.object(ui.shutil, 'get_terminal_size',
                    return_value=os.terminal_size((40, 24))), \
                    patch.object(cli, '_project_root', return_value=Path('demo')), \
                    patch.object(cli, '_project_name', return_value='demo'), \
                    patch.object(cli, 'ProjectState', return_value=SimpleNamespace(chat_id='one')), \
                    patch.object(cli.chats, 'list_chats', return_value=[chat]):
                parser = cli.build_parser()
                out = self.capture(lambda: cli.cmd_chats(parser.parse_args(['chats']), ui.UI()))
                self.assertIn('END', out)
                self.assertIn('Gemini', out)
                self.assertNotIn('updated:', out)
                self.assert_fits(ui, out, 39)
                detailed = self.capture(lambda: cli.cmd_chats(parser.parse_args(['chats', '--details']), ui.UI()))
                self.assertIn('updated:', detailed)
