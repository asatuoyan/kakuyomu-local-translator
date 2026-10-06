"""Consistent, bounded project snapshots with recoverable multi-file restoration."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import time

LIMIT = 3
INTERVAL = 300
_stamps = {}


def _root(folder):
    folder = Path(folder).resolve()
    root = folder / ".backups" / "groups"
    if not root.resolve().is_relative_to(folder):
        raise ValueError("备份目录位于作品目录之外")
    return root


def _write(path, content):
    from translator.storage.project_storage import replace_file
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path, data):
    _write(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _allowed(name):
    parts = Path(name).parts
    return len(parts) == 1 and name in {"translation-project.json", "project.json", "glossary.json"} or (
        len(parts) == 2 and parts[0] == "chapters" and len(parts[1]) == 11
        and parts[1][:6].isdigit() and parts[1].endswith(".json"))


def list_groups(folder):
    result = []
    for path in sorted((_root(folder) / "snapshots").glob("*.json"), reverse=True):
        try:
            saved = json.loads(path.read_bytes())
            result.append({"id": path.stem, "saved_at": saved["saved_at"],
                           "files": len(saved["files"]), "chapters": saved["chapters"],
                           "reason": saved.get("reason", "")})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def create_group(folder, *, reason="manual", force=True):
    from translator.storage.project_storage import _json_file_lock
    folder = Path(folder).resolve()
    with _json_file_lock:
        root = _root(folder)
        if (root / "restore.json").exists():
            raise ValueError("有未完成的整组恢复，请先完成恢复")
        groups = list_groups(folder)
        if not force and groups and time.time() - groups[0]["saved_at"] < INTERVAL:
            return groups[0]["id"]
        manifests = [p for p in (folder / "translation-project.json", folder / "project.json") if p.is_file()]
        if not manifests:
            raise ValueError("该目录没有可备份的翻译项目")
        files = [*manifests]
        chapter_count = 0
        for path in manifests:
            state = json.loads(path.read_bytes())
            if path.name == "translation-project.json" and state.get("schema_version") == 2:
                count = state.get("chapter_count")
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError("项目章节数量无效")
                chapter_count = count
                files.extend(folder / "chapters" / f"{i:06d}.json" for i in range(1, count + 1))
        glossary = folder / "glossary.json"
        if glossary.is_file():
            files.append(glossary)
        records = {}
        for path in files:
            if not path.resolve().is_relative_to(folder):
                raise ValueError("项目记录位于作品目录之外")
            stat = path.stat()
            stamp = stat.st_mtime_ns, stat.st_size
            previous = _stamps.get(path)
            old_object = root / "objects" / previous[1] if previous else None
            old_stamp = (old_object.stat().st_mtime_ns, old_object.stat().st_size) if old_object and old_object.is_file() else None
            if previous and previous[0] == stamp and old_stamp == previous[2]:
                digest = previous[1]
            else:
                content = path.read_bytes()
                json.loads(content)
                digest = hashlib.sha256(content).hexdigest()
                target = root / "objects" / digest
                if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    _write(target, content)
                stat = target.stat()
                _stamps[path] = stamp, digest, (stat.st_mtime_ns, stat.st_size)
            records[path.relative_to(folder).as_posix()] = digest
        identity = f"{time.time_ns()}-{secrets.token_hex(4)}"
        _json(root / "snapshots" / (identity + ".json"),
              {"version": 1, "saved_at": time.time(), "files": records,
               "chapters": chapter_count, "reason": reason})
        snapshots = sorted((root / "snapshots").glob("*.json"), reverse=True)
        for old in snapshots[LIMIT:]:
            old.unlink()
        used = set()
        for path in snapshots[:LIMIT]:
            used.update(json.loads(path.read_bytes())["files"].values())
        for path in (root / "objects").iterdir():
            if path.name not in used:
                path.unlink()
        return identity


def _snapshot_records(folder, identity):
    if not isinstance(identity, str) or Path(identity).name != identity:
        raise ValueError("无效的整组备份编号")
    root = _root(folder)
    saved = json.loads((root / "snapshots" / (identity + ".json")).read_bytes())
    records = {}
    for name, digest in saved["files"].items():
        if not _allowed(name) or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("整组备份包含无效文件")
        content = (root / "objects" / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("整组备份校验失败，未修改当前项目")
        json.loads(content)
        records[name] = content
    manifest = records.get("translation-project.json")
    if manifest:
        state = json.loads(manifest)
        if state.get("schema_version") == 2:
            expected = {f"chapters/{i:06d}.json" for i in range(1, state["chapter_count"] + 1)}
            if not expected.issubset(records):
                raise ValueError("整组备份缺少项目清单对应的章节")
        # Exported EPUBs are outside this snapshot; rebuild them before download.
        state.update(epub_dirty=True, restored_backup=True, output_path="")
        records["translation-project.json"] = json.dumps(state, ensure_ascii=False).encode("utf-8")
    if "glossary.json" not in records:
        records["glossary.json"] = b"[]"
    return records


def _apply(folder, journal):
    root = _root(folder)
    direction = journal["phase"]
    staging = root / "transactions" / journal["transaction"] / direction
    # Chapters and glossary first; manifests become visible last.
    names = sorted(journal["files"], key=lambda name: name in {"translation-project.json", "project.json"})
    contents = {}
    for name in names:
        if not _allowed(name):
            raise ValueError("整组恢复记录包含无效路径")
        target = folder / name
        if not target.resolve().is_relative_to(folder):
            raise ValueError("整组恢复目标位于项目目录之外")
        source = staging / name
        if source.is_file():
            content = source.read_bytes()
            expected = journal.get("hashes", {}).get(direction, {}).get(name)
            if expected and hashlib.sha256(content).hexdigest() != expected:
                raise ValueError("整组恢复暂存文件校验失败")
            if direction == "after":
                json.loads(content)
            contents[name] = content
        elif direction == "before":
            contents[name] = None
        else:
            raise ValueError("整组恢复暂存文件缺失")
    for name, content in contents.items():
        if content is None:
            (folder / name).unlink(missing_ok=True)
        else:
            _write(folder / name, content)


def recover_group_restore(folder):
    from translator.storage.project_storage import _json_file_lock
    folder = Path(folder).resolve()
    with _json_file_lock:
        journal_path = _root(folder) / "restore.json"
        if not journal_path.is_file():
            return
        journal = json.loads(journal_path.read_bytes())
        transaction = journal.get("transaction")
        if not isinstance(transaction, str) or Path(transaction).name != transaction or journal.get("phase") not in {"before", "after"}:
            raise ValueError("整组恢复记录无效")
        _apply(folder, journal)
        journal_path.unlink()


def restore_group(folder, identity):
    from translator.storage.project_storage import _json_file_lock
    folder = Path(folder).resolve()
    with _json_file_lock:
        recover_group_restore(folder)
        records = _snapshot_records(folder, identity)  # Verify everything before changing any project file.
        root = _root(folder)
        transaction = f"{time.time_ns()}-{secrets.token_hex(4)}"
        staging = root / "transactions" / transaction
        hashes = {"before": {}, "after": {}}
        for name, content in records.items():
            _write(staging / "after" / name, content)
            hashes["after"][name] = hashlib.sha256(content).hexdigest()
            current = folder / name
            if not current.resolve().is_relative_to(folder):
                raise ValueError("整组恢复目标位于项目目录之外")
            if current.is_file():
                original = current.read_bytes()
                _write(staging / "before" / name, original)
                hashes["before"][name] = hashlib.sha256(original).hexdigest()
        journal = {"phase": "after", "transaction": transaction, "files": list(records), "hashes": hashes}
        _json(root / "restore.json", journal)
        try:
            _apply(folder, journal)
        except Exception:
            journal["phase"] = "before"
            _json(root / "restore.json", journal)
            recover_group_restore(folder)
            raise
        (root / "restore.json").unlink()
        # Keep raw rollback records too, including a corrupt pre-restore manifest.
        for old in sorted((root / "transactions").iterdir(), reverse=True)[LIMIT:]:
            if old.is_dir() and old.resolve().is_relative_to((root / "transactions").resolve()) and not old.is_symlink():
                shutil.rmtree(old)


def checkpoint(folder, *, force=False, reason="automatic"):
    """Periodic coherent snapshots; invalid existing records never stop translation."""
    try:
        create_group(folder, reason=reason, force=force)
    except (OSError, ValueError, TypeError, KeyError):
        return
