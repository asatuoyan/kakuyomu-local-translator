import importlib
from pathlib import Path
import pkgutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import translator
from translator.config import APP_DIR, CONFIG_PATH
from translator.paths import WEB_DIR
from translator.version import VERSION


class PackageLayoutTests(unittest.TestCase):
    def test_entry_points_share_the_actual_implementation_modules(self):
        for entry, implementation in (
            ("main", "translator.engine"),
            ("gui", "translator.ui.gui"),
            ("web_app", "translator.ui.web_app"),
        ):
            with self.subTest(entry=entry):
                self.assertIs(importlib.import_module(entry), importlib.import_module(implementation))
        for module in pkgutil.walk_packages(translator.__path__, translator.__name__ + "."):
            with self.subTest(module=module.name):
                importlib.import_module(module.name)

    def test_config_and_assets_keep_the_original_project_root(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(APP_DIR, root)
        self.assertEqual(CONFIG_PATH, root / "config.json")
        self.assertEqual(WEB_DIR, root / "web")
        for asset in ("web_app.html", "web_app.js", "reader.html"):
            self.assertTrue((WEB_DIR / asset).is_file())

    def test_web_version_entry_point_works_outside_the_project_directory(self):
        with TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(APP_DIR / "web_app.py"), "--version"],
                cwd=folder, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(VERSION, result.stdout)
