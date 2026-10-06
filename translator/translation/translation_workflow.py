"""Persisted preview -> review -> explicitly confirmed full-book workflow."""
from dataclasses import asdict
import hashlib
from pathlib import Path

from translator.glossary.manager import extract_entity_candidates
from translator.languages import language_code
from translator.storage.project_storage import atomic_json, load_json, load_translation_state


def workflow_path(source, cfg):
    from translator.engine import safe_name
    identity = hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()[:12]
    return Path(cfg["output_dir"]) / "workflows" / (safe_name(Path(source).stem) + "_" + identity) / "workflow.json"


def ready_for_full(state, source, languages):
    return (state.get("source") == str(Path(source).resolve())
            and bool(languages) and all(language_code(code) in state.get("previews", {}) for code in languages))


def run_workflow(source, cfg, languages, preview_count=20, *, full=False, progress=None, allow_source_append=False):
    from translator.engine import (TranslationCancelled, audit_project, check_translation_cancelled,
                      project_glossary, translate_epub_language, translation_work_dir)
    source = Path(source).resolve()
    path = workflow_path(source, cfg)
    state = load_json(path, {})
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if full and allow_source_append and ready_for_full(state, source, languages) and state.get("source_hash") != source_hash:
        from translator.acquisition.source_epub import extract_epub_chapters, compute_content_hash
        # Online acquisition may append chapters, but cannot change a translated prefix.
        for language in languages:
            work_dir = translation_work_dir(source, cfg, language)
            _, originals = extract_epub_chapters(source, work_dir / "assets")
            records = load_translation_state(work_dir / "translation-project.json")["chapters"]
            if len(originals) < len(records) or any(
                record["url"] != originals[i].url or record["source_hash"] != compute_content_hash(originals[i].paragraphs)
                for i, record in enumerate(records)
            ):
                raise ValueError("已试译章节的原文或顺序发生变化，不能自动接续，请先核对。")
        state["source_hash"] = source_hash
    if full and (not ready_for_full(state, source, languages) or state.get("source_hash") != source_hash):
        raise ValueError("请先完成当前小说及所选语言的试译，再确认翻译全部。")
    if not full and state.get("source_hash") != source_hash:
        state = {}
    state.update(source=str(source), source_hash=source_hash, model=cfg["model"],
                 preview_count=preview_count, stage="full_running" if full else "preview_running")
    state.setdefault("previews", {})
    state.setdefault("full_outputs", {})
    if full:
        state["confirmed"] = True
    else:
        state["confirmed"] = False
    atomic_json(path, state)
    try:
        for language_index, language in enumerate(languages):
            check_translation_cancelled(cfg)
            code = language_code(language)
            def report(percent, message):
                if progress:
                    progress((language_index * 100 + percent) / len(languages), message)
            output = translate_epub_language(source, cfg, code, report,
                                             chapter_limit=None if full else preview_count)
            work_dir = translation_work_dir(source, cfg, code)
            project = load_translation_state(work_dir / "translation-project.json")
            count = len(project["chapters"]) if full else min(preview_count, len(project["chapters"]))
            selected = {**project, "chapters": project["chapters"][:count]}
            check_translation_cancelled(cfg)
            if progress:
                report(99, "正在检查术语与译文……")
            entries = project_glossary(cfg, work_dir, selected)
            issues = audit_project(selected, entries)
            report_path = work_dir / ("workflow-full-check.json" if full else "workflow-preview-check.json")
            atomic_json(report_path, {"chapter_count": count, "issues": [
                {"chapter": title, **asdict(issue)} for title, issue in issues]})
            # Suggestions remain separate from the authoritative glossary until reviewed.
            candidates_path = work_dir / "workflow-term-candidates.json"
            if not full:
                texts = [text for chapter in selected["chapters"] for text in chapter.get("source_paragraphs", [])]
                atomic_json(candidates_path, {"candidates": [
                    {"source": term, "count": frequency, "target": "", "status": "待确认"}
                    for term, frequency in extract_entity_candidates(texts)[:60]]})
            result = {"project": str(work_dir), "output": str(output), "chapters": count,
                      "issues": len(issues), "report": str(report_path), "candidates": str(candidates_path)}
            state["full_outputs" if full else "previews"][code] = result
            atomic_json(path, state)
        state["stage"] = "complete" if full else "awaiting_confirmation"
        atomic_json(path, state)
        return state
    except Exception as exc:
        state["stage"] = "stopped" if isinstance(exc, TranslationCancelled) else "failed"
        state["error"] = str(exc)
        atomic_json(path, state)
        raise
