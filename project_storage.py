"""Atomic project records and transactional, context-aware translation cache."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_translation_state(path: Path) -> dict:
    state = load_json(path, {})
    if state.get("schema_version") == 2:
        state["chapters"] = [
            load_json(path.parent / "chapters" / f"{index:06d}.json", None)
            for index in range(1, state["chapter_count"] + 1)
        ]
        if any(ch is None for ch in state["chapters"]):
            raise ValueError("项目章节文件缺失，请从备份恢复。")
    return state


def save_translation_state(path: Path, state: dict, changed: list[int] | None = None) -> None:
    # Legacy projects are migrated only after a successful load; retain an exact backup.
    if state.get("schema_version") != 2:
        if path.exists() and not path.with_suffix(".legacy.json").exists():
            atomic_json(path.with_suffix(".legacy.json"), load_json(path, {}))
        changed = list(range(len(state["chapters"])))
    for index in changed or []:
        atomic_json(path.parent / "chapters" / f"{index + 1:06d}.json", state["chapters"][index])
    manifest = {key: value for key, value in state.items() if key != "chapters"}
    manifest.update(schema_version=2, chapter_count=len(state["chapters"]))
    atomic_json(path, manifest)
    state.update(schema_version=2, chapter_count=len(state["chapters"]))


class TranslationCache:
    """A connection belongs to one operation/thread; only changed rows are written."""

    def __init__(self, work_dir: Path):
        work_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(work_dir / "translation-cache.sqlite3", timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS translations (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM translations WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def save(self, values: list[tuple[str, str]]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO translations VALUES (?, ?)", values)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()
