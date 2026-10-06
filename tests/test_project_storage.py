from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from translator.storage.project_storage import atomic_json, load_json


class AtomicJsonTests(unittest.TestCase):
    def windows_error(self, code):
        error = PermissionError("file in use")
        error.winerror = code
        return error

    def test_transient_windows_file_locks_are_retried(self):
        replace = os.replace
        for code in (5, 32, 33):
            with self.subTest(code=code), TemporaryDirectory() as folder:
                path = Path(folder) / "network.json"
                atomic_json(path, {"acquired": 1})
                attempts = []
                def locked(source, destination):
                    attempts.append(source)
                    if len(attempts) < 3:
                        self.assertEqual(load_json(path, {}), {"acquired": 1})
                        raise self.windows_error(code)
                    replace(source, destination)
                with patch("translator.storage.project_storage.os.replace", side_effect=locked), patch("translator.storage.project_storage.time.sleep"):
                    atomic_json(path, {"acquired": 2})
                self.assertEqual(len(attempts), 3)
                self.assertEqual(load_json(path, {}), {"acquired": 2})
                self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_persistent_lock_preserves_existing_file_and_cleans_temporary_file(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "network.json"
            atomic_json(path, {"acquired": 1})
            with patch("translator.storage.project_storage.os.replace", side_effect=self.windows_error(5)) as replace, patch("translator.storage.project_storage.time.sleep"):
                with self.assertRaises(PermissionError):
                    atomic_json(path, {"acquired": 2})
            self.assertEqual(replace.call_count, 11)
            self.assertEqual(load_json(path, {}), {"acquired": 1})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_other_errors_are_not_retried(self):
        with TemporaryDirectory() as folder:
            with patch("translator.storage.project_storage.os.replace", side_effect=OSError("disk failure")) as replace, patch("translator.storage.project_storage.time.sleep") as sleep:
                with self.assertRaisesRegex(OSError, "disk failure"):
                    atomic_json(Path(folder) / "network.json", {})
            replace.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_concurrent_polling_and_progress_updates(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "network.json"
            atomic_json(path, {"acquired": 0})
            def write():
                for index in range(1, 101):
                    atomic_json(path, {"acquired": index})
            def read():
                for _ in range(500):
                    self.assertIn("acquired", load_json(path, {}))
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(write)] + [executor.submit(read) for _ in range(3)]
                for future in futures:
                    future.result(timeout=30)
            self.assertEqual(load_json(path, {}), {"acquired": 100})
