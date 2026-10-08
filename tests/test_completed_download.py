import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from zipfile import ZipFile

from translator.storage.project_storage import save_translation_state
from translator.ui.web_app import Application, create_server


class CompletedDownloadTests(unittest.TestCase):
    def test_running_project_exports_saved_chapters_without_changing_progress(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = json.loads(Path('config.example.json').read_text(encoding='utf-8'))
            cfg['output_dir'] = root
            app = Application(cfg)
            app.task['running'] = True
            manifest = Path(root) / 'book' / 'translation-project.json'
            state = {'metadata': {'title': 'Partial novel', 'author': 'Author'},
                     'language': 'en', 'source_chapter_count': 3,
                     'chapters': [{'url': 'chapter-1', 'title': 'Finished chapter',
                                   'paragraphs': ['Saved translation'], 'blocks': [], 'images': []}]}
            save_translation_state(manifest, state)
            before = manifest.read_bytes()
            server = create_server(app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with urlopen(server.url + 'api/download-completed?project=book') as response:
                    self.assertEqual(response.headers['Content-Type'], 'application/epub+zip')
                    self.assertIn('attachment', response.headers['Content-Disposition'])
                    with ZipFile(io.BytesIO(response.read())) as archive:
                        content = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist()
                                            if name.endswith('.xhtml'))
                        self.assertIn('Saved translation', content)
                        self.assertIn('Finished chapter', content)
                self.assertEqual(before, manifest.read_bytes())
                self.assertTrue(app.task['running'])
                for project in ('../outside', 'empty'):
                    if project == 'empty':
                        save_translation_state(Path(root) / project / manifest.name,
                                               {**state, 'chapters': []})
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(server.url + 'api/download-completed?project=' + project)
                    self.assertEqual(raised.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
