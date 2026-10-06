import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from translator.ui import file_dialogs as dialogs
from translator.ui import cli


class FileDialogTests(unittest.TestCase):
    def environment(self, selected='', error=None, existing=False):
        root = Mock()
        provider = types.SimpleNamespace(askopenfilename=Mock(return_value=selected, side_effect=error),
            askdirectory=Mock(return_value=selected), asksaveasfilename=Mock(return_value=selected))
        tk = types.ModuleType('tkinter')
        tk.Tk = Mock(return_value=root)
        tk._default_root = root if existing else None
        tk.filedialog = provider
        tk.messagebox = types.SimpleNamespace(askyesnocancel=Mock(return_value=None))
        return root, tk, provider

    def test_success_uses_parent_and_destroys_temporary_root(self):
        root, tk, provider = self.environment('book.epub')
        with patch.dict(sys.modules, {'tkinter': tk}), patch('builtins.input', side_effect=AssertionError('prompted')):
            self.assertEqual(dialogs.select_epub_file(), Path('book.epub'))
        root.withdraw.assert_called_once()
        root.destroy.assert_called_once()
        self.assertIs(provider.askopenfilename.call_args.kwargs['parent'], root)

    def test_cancellation_closes_root_without_prompting_for_another_path(self):
        root, tk, _ = self.environment('')
        with patch.dict(sys.modules, {'tkinter': tk}), patch('builtins.input', side_effect=AssertionError('prompted')):
            with self.assertRaises(dialogs.DialogCancelled):
                dialogs.select_file()
        root.destroy.assert_called_once()

    def test_unavailable_dialog_closes_root_and_falls_back_to_console(self):
        root, tk, _ = self.environment(error=RuntimeError('no display'))
        with patch.dict(sys.modules, {'tkinter': tk}), patch('builtins.input', return_value='"book.epub"') as prompt:
            self.assertEqual(dialogs.select_file(), Path('book.epub'))
        root.destroy.assert_called_once()
        prompt.assert_called_once()

    def test_dialog_does_not_destroy_an_existing_gui_window(self):
        root, tk, provider = self.environment('projects', existing=True)
        with patch.dict(sys.modules, {'tkinter': tk}):
            self.assertEqual(dialogs.select_directory(), Path('projects'))
        tk.Tk.assert_not_called()
        root.destroy.assert_not_called()
        self.assertTrue(provider.askdirectory.call_args.kwargs['mustexist'])

    def test_save_dialog_preserves_default_name_and_extension(self):
        root, tk, provider = self.environment('saved.json')
        with patch.dict(sys.modules, {'tkinter': tk}):
            self.assertEqual(dialogs.select_save_file(initial_path=Path('output/glossary.json')), Path('saved.json'))
        self.assertEqual(provider.asksaveasfilename.call_args.kwargs['initialfile'], 'glossary.json')
        self.assertEqual(provider.asksaveasfilename.call_args.kwargs['defaultextension'], '.json')
        root.destroy.assert_called_once()

    def test_cancelled_import_choice_and_cli_action_end_without_continuation(self):
        root, tk, _ = self.environment()
        with patch.dict(sys.modules, {'tkinter': tk}), patch('builtins.input', side_effect=AssertionError('prompted')):
            with self.assertRaises(dialogs.DialogCancelled):
                dialogs.select_import_source()
        root.destroy.assert_called_once()
        with patch.object(cli, '_main', side_effect=dialogs.DialogCancelled):
            self.assertEqual(cli.main(), 0)
