"""Capture names from aligned translations without inventing new translations."""
import json

from glossary_manager import GlossaryEntry, load_project_glossary, merge_glossaries, save_project_glossary


def capture_terms(originals, translations, cfg, work_dir):
    from main import ollama_chat_content, check_translation_cancelled
    pairs = list(zip(originals, translations))
    existing = load_project_glossary(work_dir)
    known = set(cfg.get("glossary", {})) | {e.source for e in existing}
    incoming = []
    # Keep extraction requests bounded and in original reading order.
    batches, batch, size = [], [], 0
    for source, target in pairs:
        if batch and size + len(source) + len(target) > 5000:
            batches.append(batch)
            batch, size = [], 0
        batch.append({"source": source, "translation": target})
        size += len(source) + len(target)
    if batch:
        batches.append(batch)
    for batch in batches:
        payload = {"model": cfg.get("glossary_model") or cfg["model"], "stream": False,
                   "format": "json", "options": {"temperature": 0}, "messages": [
            {"role": "system", "content": '从原文与对应译文提取人物、地点、组织、技能等专有名词。只抄录译文中已经使用的译名，不重新翻译，不提取普通名词。同一原词取首次出现的译名。返回 JSON：{"entries":[{"source":"原词","target":"译文中的译名","category":"人物名"}]}。没有则返回空 entries。'},
            {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}]}
        raw = json.loads(ollama_chat_content(payload, cfg))
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
            raise ValueError("术语提取未返回有效 entries")
        for item in raw["entries"]:
            if not isinstance(item, dict):
                continue
            source, target = item.get("source"), item.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            source, target = source.strip(), target.strip()
            if not source or not target or source in known:
                continue
            first = next((translation for original, translation in pairs if source in original), "")
            if target not in first:
                continue
            incoming.append(GlossaryEntry(source, target, str(item.get("category", "特殊設定")), "沿用首次译文"))
            known.add(source)
        check_translation_cancelled(cfg)
    if incoming:
        save_project_glossary(work_dir, merge_glossaries(existing, incoming, overwrite=False))
    return incoming
