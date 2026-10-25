"""Compact tool labels affect progress without changing transcript identifiers."""
import importlib
import json
import unittest


class ToolLabelsTests(unittest.TestCase):
    def test_stream_labels_and_transcripts(self):
        for package in ('consoleagents', 'autoagents'):
            providers = importlib.import_module(package + '.providers')
            for tool, arguments, expected in (
                    ('read_file', {'path': 'main.py'}, '📄👁 main.py'),
                    ('run_command', {'CommandLine': 'python -m unittest'}, '>_ python -m unittest')):
                for adapter in ('ClaudeProvider', 'AntigravityProvider'):
                    with self.subTest(package=package, adapter=adapter, tool=tool):
                        provider = getattr(providers, adapter)('agent', {}, {})
                        result, shown = providers.RunResult('agent'), []
                        if adapter == 'ClaudeProvider':
                            event = {'type': 'assistant', 'message': {'content': [
                                {'type': 'tool_use', 'name': tool, 'input': arguments}]}}
                        else:
                            event = {'event': 'step_update', 'step_update': {
                                'state': 'ACTIVE', 'step_type': 'tool', 'tool_info': {
                                    'name': tool, 'parameters': arguments}}}
                        provider.handle_line(json.dumps(event), result, shown.append)
                        self.assertEqual(shown, [expected])
                        self.assertTrue(result.log[0].startswith('[tool] ' + tool + ': '))

    def test_missing_arguments_and_other_tool_names(self):
        for package in ('consoleagents', 'autoagents'):
            providers = importlib.import_module(package + '.providers')
            with self.subTest(package=package):
                self.assertEqual(providers._tool_display('read_file'), '📄👁')
                self.assertEqual(providers._tool_display('run_command'), '>_')
                self.assertEqual(providers._tool_display('write_file: read_file.txt'),
                                 '🔧 write_file: read_file.txt')
