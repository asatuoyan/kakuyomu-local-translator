"""Read-only Web review reports and remembered glossary target changes."""
import hashlib

from translator.storage.project_storage import atomic_json, load_json, load_translation_state


def remember_term_changes(folder, previous, current):
    path = folder / "glossary-changes.json"
    changes = load_json(path, {})
    updated = False
    before = {entry.source: entry.target for entry in previous}
    for entry in current:
        old = before.get(entry.source)
        if old and old != entry.target:
            values = changes.setdefault(entry.source, [])
            if old not in values:
                values.append(old)
                updated = True
    if updated:
        atomic_json(path, changes)


def review_project(folder, cfg, mode="all"):
    from translator.engine import audit_project, project_glossary
    if mode not in ("all", "changes"):
        raise ValueError("请选择译文检查或术语修改检查")
    manifest = folder / "translation-project.json"
    project = (load_translation_state(manifest) if manifest.exists()
               else load_json(folder / "project.json", {}))
    entries = project_glossary(cfg, folder, project)
    changes = load_json(folder / "glossary-changes.json", {})
    rows, unavailable = [], []
    for index, chapter in enumerate(project.get("chapters", []), 1):
        originals = chapter.get("source_paragraphs") or chapter.get("japanese", [])
        translations = chapter.get("translation") or chapter.get("paragraphs", [])
        title = chapter.get("title", str(index))
        if not originals and not translations and any(b.get("type") == "image" for b in chapter.get("blocks", [])):
            continue
        if not originals or len(originals) != len(translations):
            unavailable.append({"chapter": title, "reason": "缺少完整原文与译文对应关系，请在桌面版补齐原文后重新检查"})
            continue
        problems = {}
        if mode == "all":
            for _, warning in audit_project({**project, "chapters": [chapter]}, entries):
                problems.setdefault(warning.paragraph_index, []).append(
                    f"术语未落实：{warning.source} → {warning.expected_target}" if warning.source else warning.category)
        else:
            for pos, (original, translated) in enumerate(zip(originals, translations), 1):
                for entry in entries:
                    if entry.source in original:
                        old_targets = [old for old in changes.get(entry.source, [])
                                       if old != entry.target and old in translated]
                        if old_targets:
                            problems.setdefault(pos, []).append(
                                f"旧译名：{'、'.join(old_targets)} → {entry.target}（{entry.source}）")
        for pos, reasons in problems.items():
            original, translated = originals[pos - 1], translations[pos - 1]
            identifier = hashlib.sha256(f"{index}\0{pos}\0{original}\0{translated}".encode()).hexdigest()
            rows.append({"id": identifier, "chapter_index": index, "chapter": title,
                         "paragraph": pos, "original": original, "translation": translated,
                         "reasons": reasons})
    return {"items": rows, "unavailable": unavailable, "chapters": len(project.get("chapters", [])),
            "mode": mode}
