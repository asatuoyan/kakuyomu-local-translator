"""Atomic journal of Web tasks that may resume after a service restart."""
from copy import deepcopy
from pathlib import Path
import secrets
import threading

from translator.storage.project_storage import atomic_json, load_json


class TaskJournal:
    def __init__(self, output_dir):
        self.path = Path(output_dir) / "web-active-tasks.json"
        self.lock = threading.RLock()
        self.error = ""
        try:
            saved = load_json(self.path, {"version": 1, "tasks": {}})
            if not isinstance(saved, dict) or saved.get("version") != 1 or not isinstance(saved.get("tasks"), dict):
                raise ValueError("Invalid task recovery journal")
            self.tasks = saved["tasks"]
            for slot, task in self.tasks.items():
                if slot not in {"translation", "acquisition"} or not isinstance(task, dict) or not isinstance(task.get("body"), dict) or not isinstance(task.get("id"), str):
                    raise ValueError("Invalid saved task")
        except (OSError, ValueError) as exc:
            self.tasks = {}
            self.error = str(exc)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.tasks)

    def _save(self, tasks):
        atomic_json(self.path, {"version": 1, "tasks": tasks})
        self.tasks = tasks

    def begin(self, slot, body):
        with self.lock:
            task_id = secrets.token_hex(12)
            tasks = deepcopy(self.tasks)
            tasks[slot] = {"id": task_id, "body": deepcopy(body)}
            self._save(tasks)
            return task_id

    def matches(self, slot, task_id):
        with self.lock:
            return self.tasks.get(slot, {}).get("id") == task_id

    def clear(self, slot, task_id=None):
        with self.lock:
            if slot not in self.tasks or task_id is not None and not self.matches(slot, task_id):
                return
            tasks = deepcopy(self.tasks)
            tasks.pop(slot)
            self._save(tasks)
