import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from translator.storage import group_backups as groups
from translator.storage.project_storage import atomic_json, load_json, load_translation_state, save_translation_state


class GroupBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'book'
        self.manifest = self.folder / 'translation-project.json'
        atomic_json(self.folder / 'glossary.json', [{'source': 'name', 'target': 'old'}])
        self.state = {'metadata': {'title': 'fixture'}, 'language': 'en', 'chapters': [
            {'title': 'one', 'paragraphs': ['old translation'], 'source_paragraphs': ['original']}]}
        save_translation_state(self.manifest, self.state)
        self.saved = groups.create_group(self.folder)

    def change(self):
        self.state['chapters'][0]['paragraphs'] = ['new translation']
        self.state['chapters'].append({'title': 'two', 'paragraphs': ['new chapter']})
        atomic_json(self.folder / 'glossary.json', [{'source': 'name', 'target': 'new'}])
        save_translation_state(self.manifest, self.state, [0, 1])

    def test_restore_matches_manifest_chapters_and_glossary_and_invalidates_old_export(self):
        self.change()
        groups.restore_group(self.folder, self.saved)
        restored = load_translation_state(self.manifest)
        self.assertEqual(len(restored['chapters']), 1)
        self.assertEqual(restored['chapters'][0]['paragraphs'], ['old translation'])
        self.assertEqual(load_json(self.folder / 'glossary.json', []), [{'source': 'name', 'target': 'old'}])
        self.assertTrue(restored['epub_dirty'])
        self.assertTrue(restored['restored_backup'])
        self.assertFalse((groups._root(self.folder) / 'restore.json').exists())

    def test_checksum_failure_does_not_change_any_current_file(self):
        self.change()
        metadata = json.loads((groups._root(self.folder) / 'snapshots' / (self.saved + '.json')).read_bytes())
        digest = metadata['files']['chapters/000001.json']
        (groups._root(self.folder) / 'objects' / digest).write_bytes(b'{corrupted')
        before = self.manifest.read_bytes(), (self.folder / 'glossary.json').read_bytes()
        with self.assertRaisesRegex(ValueError, '校验失败'):
            groups.restore_group(self.folder, self.saved)
        self.assertEqual(before, (self.manifest.read_bytes(), (self.folder / 'glossary.json').read_bytes()))

    def test_mid_restore_failure_rolls_back_all_records(self):
        self.change()
        paths = [self.manifest, self.folder / 'glossary.json', self.folder / 'chapters' / '000001.json']
        before = [path.read_bytes() for path in paths]
        write = groups._write
        failed = []
        def fail_once(path, content):
            if path == self.folder / 'glossary.json' and not failed:
                failed.append(True)
                raise OSError('disk unavailable')
            return write(path, content)
        with patch.object(groups, '_write', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'disk unavailable'):
                groups.restore_group(self.folder, self.saved)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertFalse((groups._root(self.folder) / 'restore.json').exists())

    def test_interrupted_restore_is_finished_before_loading_the_project(self):
        self.change()
        write = groups._write
        def power_loss(path, content):
            if path == self.manifest:
                raise SystemExit('power loss')
            return write(path, content)
        with patch.object(groups, '_write', side_effect=power_loss):
            with self.assertRaises(SystemExit):
                groups.restore_group(self.folder, self.saved)
        self.assertTrue((groups._root(self.folder) / 'restore.json').is_file())
        restored = load_translation_state(self.manifest)
        self.assertEqual(restored['chapters'][0]['paragraphs'], ['old translation'])
        self.assertFalse((groups._root(self.folder) / 'restore.json').exists())

    def test_retains_three_groups_and_uses_shared_objects(self):
        for _ in range(5):
            groups.create_group(self.folder)
        self.assertEqual(len(groups.list_groups(self.folder)), 3)
        objects = list((groups._root(self.folder) / 'objects').iterdir())
        self.assertEqual(len(objects), 3)  # manifest, one chapter, glossary; shared across snapshots


if __name__ == '__main__':
    unittest.main()
