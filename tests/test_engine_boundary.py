import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import translator.engine as engine
import translator.ui.cli as cli


class EngineBoundaryTests(unittest.TestCase):
    def test_importing_engine_does_not_load_ui_or_tkinter(self):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c',
            'import sys,json; import translator.engine; print(json.dumps([name for name in sys.modules if name == "tkinter" or name.startswith("tkinter.") or name.startswith("translator.ui")]))'],
            capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout), [])

    def test_missing_model_never_requests_input_even_with_legacy_flag(self):
        with patch.object(engine, 'installed_models', return_value=set()), \
             patch('builtins.input', side_effect=AssertionError('engine prompted')), \
             patch.object(engine, 'pull_model') as pull:
            with self.assertRaises(RuntimeError):
                engine.ensure_model({'model': 'missing'}, interactive=True)
        pull.assert_not_called()

    def test_cli_translation_delegates_to_core_without_a_file_dialog_for_explicit_source(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'original.epub'
            source.write_bytes(b'original')
            with patch.object(cli, 'select_epub_file', side_effect=AssertionError('unexpected dialog')), \
                 patch('builtins.input', return_value='1'), \
                 patch.object(cli, 'translate_epub_language', return_value=Path(directory) / 'translated.epub') as translate:
                cli.translate_source_epub({'model': 'test'}, False, source)
            translate.assert_called_once_with(source, {'model': 'test'}, list(cli.LANGUAGES)[0])

    def test_engine_source_does_not_depend_on_ui_modules_or_input(self):
        tree = ast.parse(Path(engine.__file__).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or '').startswith(('translator.ui', 'tkinter')))
            if isinstance(node, ast.Import):
                self.assertTrue(all(not name.name.startswith(('translator.ui', 'tkinter')) for name in node.names))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, 'input')
