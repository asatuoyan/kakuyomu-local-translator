"""Bounded snapshots of valid JSON records and explicit restoration."""
import json
import os
from pathlib import Path
import secrets
import time

LIMIT = 3


def directory(path):
    return path.parent / ".backups" / path.name


def preserve(path):
    if not path.is_file():
        return
    try:
        content = path.read_bytes()
        json.loads(content)
    except (ValueError, UnicodeError):
        return  # Never rotate a corrupt record into the good history.
    folder = directory(path)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{time.time_ns()}-{secrets.token_hex(3)}.json"
    temporary = target.with_suffix(".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        from translator.storage.project_storage import replace_file
        replace_file(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    for old in sorted(folder.glob("*.json"), reverse=True)[LIMIT:]:
        old.unlink()


def list_backups(path):
    return [{"id": entry.name, "saved_at": entry.stat().st_mtime}
            for entry in sorted(directory(path).glob("*.json"), reverse=True)]


def restore(path, identity):
    from translator.storage.project_storage import atomic_json
    if not isinstance(identity, str) or Path(identity).name != identity:
        raise ValueError("无效的备份编号")
    backup = directory(path) / identity
    if not backup.is_file():
        raise ValueError("备份不存在")
    value = json.loads(backup.read_text(encoding="utf-8"))
    atomic_json(path, value)
