"""Translation core without desktop widgets or interactive workflows."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import queue
import re
import sys
import subprocess
import time
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from translator.acquisition.browser_session import launch_context as _launch_context, select_active_page
from translator.translation.adaptive_batches import AdaptiveBatcher
from translator.domain import Episode, safe_name, normalize_text
from translator.acquisition.chapter_reader import extract_episode
from translator.storage.translation_book import TranslationBook
from translator.translation.murasaki_profile import (uses_murasaki_profile, translation_payload,
                              translation_only, ThinkingBudgetExceeded)
from translator.translation.hy_mt_profile import (uses_hy_mt_30b_profile, reference_context_limit,
                           translation_payload as hy_mt_translation_payload)
from translator.config import (APP_DIR, CONFIG_PATH, DEFAULT_MODEL, TRANSLATION_MODELS,
                        load_config as read_app_config, update_config)
from translator.storage.project_storage import (TranslationCache, atomic_json, load_json,
                             load_translation_state, save_translation_state)
from translator.translation.quality_checks import check_translation_quality
from translator.languages import LANGUAGES, language_code, language_name, language_suffix, normalize_output, output_glossary
import random
from playwright.sync_api import BrowserContext, Page

from translator.formats.epub_append import build_extended_epub, create_project_from_epub, inspect_epub
from translator.glossary.manager import (
    VALID_CATEGORIES,
    CandidateTerm,
    GlossaryEntry,
    GlossaryViolation,
    check_glossary_compliance,
    dict_to_entries,
    entries_to_categorized_dict,
    entries_to_dict,
    export_glossary_to_file,
    extract_candidate_terms,
    find_affected_chapters,
    format_glossary_prompt,
    get_effective_glossary,
    import_glossary_from_file,
    invalidate_affected_cache,
    load_project_glossary,
    merge_glossaries,
    save_project_glossary,
    scan_novel_entities,
)
from translator.acquisition.source_epub import (
    SourceChapter, build_source_epub, compute_content_hash, download_binary,
    download_chapter_images, extract_epub_chapters, extract_source_chapter,
    extract_work_info, get_chapter_id, import_cookie_json, normalize_work_url,
    open_work_page, translated_source_epub,
)


def load_config() -> dict[str, Any]:
    return read_app_config(CONFIG_PATH, APP_DIR)


def installed_models(cfg: dict[str, Any]) -> set[str]:
    base = cfg["ollama_url"].rstrip("/")
    try:
        r = requests.get(f"{base}/api/tags", timeout=10)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"無法連接 Ollama（{base}）。請先啟動 Ollama 或執行 ollama serve。"
        ) from exc
    return {m.get("name", "") for m in r.json().get("models", [])}


def model_is_installed(model: str, names: set[str]) -> bool:
    if model in names:
        return True
    return ":" not in model and any(n.split(":", 1)[0] == model for n in names)


def pull_model(cfg: dict[str, Any], model: str) -> None:
    base = cfg["ollama_url"].rstrip("/")
    print(f"开始下载 {model}；首次下载可能需要较长时间。")
    with requests.post(
        f"{base}/api/pull",
        json={"model": model, "stream": True},
        stream=True,
        timeout=cfg.get("request_timeout_seconds", 600),
    ) as response:
        response.raise_for_status()
        last_percent = -1
        last_status = ""
        for raw in response.iter_lines():
            if not raw:
                continue
            event = json.loads(raw.decode("utf-8"))
            if event.get("error"):
                raise RuntimeError(event["error"])
            status = event.get("status", "")
            total = int(event.get("total") or 0)
            completed = int(event.get("completed") or 0)
            percent = int(completed * 100 / total) if total else -1
            if percent >= 0 and percent != last_percent:
                print(f"  {status}: {percent}%", end="\r", flush=True)
                last_percent = percent
            elif status and status != last_status and total == 0:
                print(f"  {status}")
            last_status = status
    print(f"\n模型 {model} 已准备完成。")


def ensure_model(cfg: dict[str, Any], model: str | None = None, interactive: bool = False) -> bool:
    """Validate an installed model without prompting; interactive is a legacy argument."""
    model = model or cfg["model"]
    names = installed_models(cfg)
    if model_is_installed(model, names):
        return True
    available = "、".join(sorted(names)) or "無"
    raise RuntimeError(
        f"Ollama 找不到模型 {model}。已安裝的模型：{available}。"
        "請在 config.json 設定與 Ollama 完全相同的模型名稱。"
    )


def glossary_text(cfg: dict[str, Any]) -> str:
    return format_glossary_prompt(cfg.get("glossary", {}))


def report_and_check_compliance(
    episode: Episode,
    translations: list[str],
    cfg: dict[str, Any],
    label: str = "初譯",
) -> list[str]:
    glossary = cfg.get("glossary", {})
    if not glossary:
        return translations
    auto_fix = bool(cfg.get("auto_fix_glossary", False))
    violations, fixed = check_glossary_compliance(
        episode.paragraphs, translations, glossary, auto_fix=auto_fix
    )
    if violations:
        print(f"  [術語檢查] {label} 發現 {len(violations)} 處未符合術語表：")
        for v in violations[:5]:
            print(f"    - 第 {v.paragraph_index} 段【{v.category}】：原文「{v.source}」-> 應為「{v.expected_target}」")
        if len(violations) > 5:
            print(f"    ... 以及其他 {len(violations) - 5} 處警示。")
        if auto_fix:
            print("    已自動依術語表標準譯名進行校正。")
            return fixed
    return translations


class OllamaOutputLimitExceeded(RuntimeError):
    """The model exhausted its output budget before completing the response."""


class OllamaContextLimitExceeded(requests.HTTPError):
    """The server rejected a prompt exceeding its context window."""


def _read_ollama_content(payload: dict[str, Any], cfg: dict[str, Any]) -> str:
    base = cfg["ollama_url"].rstrip("/")
    streamed_payload = dict(payload)
    streamed_payload["stream"] = True
    response = requests.post(
        f"{base}/api/chat",
        json=streamed_payload,
        stream=True,
        timeout=cfg.get("request_timeout_seconds", 600),
    )
    try:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            try:
                details = response.json().get("error", response.text)
            except (ValueError, AttributeError):
                details = response.text
            error_type = (OllamaContextLimitExceeded if response.status_code == 400 and
                          any(marker in str(details).lower() for marker in
                              ("exceed_context_size", "exceeds the available context size"))
                          else requests.HTTPError)
            raise error_type(
                f"Ollama 请求失败（HTTP {response.status_code}，模型 {payload.get('model', '')}）：{str(details)[:2000]}",
                response=response, request=exc.request) from exc
        content_parts: list[str] = []
        generated = 0
        thinking = 0
        for raw in response.iter_lines():
            check_translation_cancelled(cfg)
            if not raw:
                continue
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                event = json.loads(raw)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
                raise RuntimeError(f"Ollama 回傳了無效資料：{str(raw)[:300]}") from exc
            if not isinstance(event, dict):
                raise RuntimeError("Ollama 返回了无效的响应对象。")
            if event.get("error"):
                raise RuntimeError(f"Ollama 生成失敗：{event['error']}")
            message = event.get("message", {})
            if not isinstance(message, dict) or not isinstance(message.get("content", ""), str):
                raise RuntimeError("Ollama 返回了无效的消息内容。")
            content_parts.append(message.get("content", ""))
            generated += len(message.get("content", ""))
            thinking += len(message.get("thinking", "")) if isinstance(message.get("thinking", ""), str) else 0
            if cfg.get("_thinking_char_limit") and thinking > cfg["_thinking_char_limit"]:
                raise ThinkingBudgetExceeded("Murasaki 思考超出预算，切换为直接翻译。")
            if cfg.get("_translation_output_chars") and generated > cfg["_translation_output_chars"]:
                raise RuntimeError("Ollama 输出达到长度限制：生成内容远超原文长度，已停止本次请求。")
            if cfg.get("_stream_activity"):
                cfg["_stream_activity"](generated, thinking)
            if event.get("done") is True:
                if event.get("done_reason") in {"length", "max_tokens"}:
                    label = cfg.get("_output_label", "译文")
                    raise OllamaOutputLimitExceeded(f"Ollama 输出达到长度限制，{label}未完成。")
                if cfg.get("_translation_metrics"):
                    cfg["_translation_metrics"]({key: event.get(key, 0) for key in (
                        "load_duration", "prompt_eval_count", "prompt_eval_duration",
                        "eval_count", "eval_duration", "total_duration")})
                return "".join(content_parts)
        raise RuntimeError("Ollama 响应提前结束，未收到完成标记。")
    finally:
        response.close()


def check_translation_cancelled(cfg: dict[str, Any]) -> None:
    if cfg.get("_translation_cancelled", lambda: False)():
        raise TranslationCancelled()


def ollama_chat_content(payload: dict[str, Any], cfg: dict[str, Any]) -> str:
    check_translation_cancelled(cfg)
    if "_translation_cancelled" not in cfg:
        return _read_ollama_content(payload, cfg)
    # The network worker owns the response and always closes it. Cancellation of
    # the UI does not wait for a stalled socket, nor permit late data to be saved.
    results = queue.Queue(maxsize=1)
    abandoned = threading.Event()
    request_cfg = dict(cfg)
    request_cfg["_translation_cancelled"] = abandoned.is_set
    activity = [0, 0]
    request_cfg["_stream_activity"] = lambda generated, thinking: activity.__setitem__(slice(None), [generated, thinking])
    started = time.monotonic()
    next_update = started

    def receive():
        try:
            results.put((True, _read_ollama_content(payload, request_cfg)))
        except Exception as exc:
            results.put((False, exc))

    threading.Thread(target=receive, daemon=True).start()
    try:
        while True:
            check_translation_cancelled(cfg)
            now = time.monotonic()
            if cfg.get("_translation_activity") and now >= next_update:
                elapsed = int(now - started)
                details = (f"已生成 {activity[0]} 字" if activity[0] else
                           (f"模型思考中 · {activity[1]} 字" if activity[1] else "等待模型响应 / 加载"))
                cfg["_translation_activity"](f"{cfg.get('_activity_label', '')} · {details} · {elapsed} 秒")
                next_update = now + 1
            try:
                success, value = results.get(timeout=0.1)
            except queue.Empty:
                continue
            check_translation_cancelled(cfg)
            if success:
                return value
            raise value
    finally:
        abandoned.set()


def translate_chunk(
    paragraphs: list[str], cfg: dict[str, Any], previous_context: str = "",
    *, _repeat_retry: bool = False, _context_retry: bool = False,
) -> list[str]:
    check_translation_cancelled(cfg)
    if not paragraphs:
        return []
    from translator.translation.translation_prompt import build_prompt, has_prompt_echo
    user = build_prompt(paragraphs, cfg, previous_context)
    source_chars = sum(map(len, paragraphs))
    request_cfg = dict(cfg)
    # Allow expansion across languages while bounding runaway generations for
    # short titles as well as body text. Only translation requests use this guard.
    request_cfg["_translation_output_chars"] = max(512, source_chars * 6)
    payload = {
        "model": cfg["model"],
        "think": False,
        "messages": [{"role": "user", "content": user}],
        "options": {
            "temperature": max(0.5, float(cfg.get("temperature", 0.2))) if _repeat_retry else cfg.get("temperature", 0.2),
            "num_predict": max(256, source_chars * 4 + 128),
        },
    }
    murasaki = uses_murasaki_profile(cfg)
    if murasaki:
        payload = translation_payload(paragraphs, cfg, retry=_repeat_retry)
        if payload["think"]:
            request_cfg["_thinking_char_limit"] = min(4096, max(1024, source_chars * 2))
        # Some Ollama imports put inline reasoning in content rather than thinking.
        request_cfg["_translation_output_chars"] = max(16384, source_chars * 6)
    elif uses_hy_mt_30b_profile(cfg):
        payload = hy_mt_translation_payload(
            paragraphs, cfg, previous_context, retry=_repeat_retry)
    try:
        content = ollama_chat_content(payload, request_cfg)
        if murasaki:
            content = translation_only(content)
        if len(content) > max(512, source_chars * 6):
            raise RuntimeError("Ollama 输出达到长度限制：生成内容远超原文长度。")
    except OllamaContextLimitExceeded:
        if cfg.get("_batch_degraded"):
            cfg["_batch_degraded"]()
        glossary = cfg.get("glossary", {})
        source_text = "\n".join(paragraphs)
        relevant = {source: target for source, target in glossary.items() if source in source_text}
        if len(relevant) < len(glossary):
            return translate_chunk(paragraphs, {**cfg, "glossary": relevant}, previous_context,
                                   _repeat_retry=_repeat_retry)
        if len(paragraphs) > 1:
            midpoint = len(paragraphs) // 2
            return (translate_chunk(paragraphs[:midpoint], cfg, previous_context) +
                    translate_chunk(paragraphs[midpoint:], cfg, previous_context))
        if previous_context:
            return translate_chunk(paragraphs, cfg, "", _repeat_retry=_repeat_retry)
        raise
    except ThinkingBudgetExceeded:
        if cfg.get("_batch_degraded"):
            cfg["_batch_degraded"]()
        print("  Murasaki 思考超出预算，停止本次请求并直接翻译一次……", flush=True)
        direct_cfg = dict(cfg)
        direct_cfg["murasaki_thinking_mode"] = "off"
        return translate_chunk(paragraphs, direct_cfg, previous_context,
                               _repeat_retry=_repeat_retry, _context_retry=_context_retry)
    except RuntimeError as exc:
        if not any(marker in str(exc).lower() for marker in
                   ("token repeat limit reached", "输出达到长度限制")):
            raise
        if len(paragraphs) > 1:
            if cfg.get("_batch_degraded"):
                cfg["_batch_degraded"]()
            print(f"  模型输出受限，自動拆分 {len(paragraphs)} 段重試……", flush=True)
            midpoint = len(paragraphs) // 2
            first = translate_chunk(paragraphs[:midpoint], cfg, previous_context)
            continued_context = "\n".join(part for part in [previous_context, *first] if part)
            return first + translate_chunk(paragraphs[midpoint:], cfg, continued_context)
        if _repeat_retry:
            raise
        print("  模型遇到重複 token 限制，單段改用較高溫度重試一次……", flush=True)
        return translate_chunk(paragraphs, cfg, "", _repeat_retry=True)
    content = normalize_text(content)
    if not content:
        raise RuntimeError("模型没有返回译文。")
    if has_prompt_echo(content, paragraphs):
        if cfg.get("_batch_degraded"):
            cfg["_batch_degraded"]()
        if _context_retry:
            raise RuntimeError("译文混入翻译指令，未保存该批次。")
        print("  译文混入翻译指令，移除前文参考并逐段重试……", flush=True)
        return [translate_chunk([paragraph], cfg, "", _context_retry=True, _repeat_retry=murasaki)[0]
                for paragraph in paragraphs]
    translations = [content] if len(paragraphs) == 1 else re.split(r"\n\s*\n", content)
    if len(translations) != len(paragraphs):
        returned_count = len(translations)
        if len(paragraphs) > 1:
            if cfg.get("_batch_degraded"):
                cfg["_batch_degraded"]()
            midpoint = len(paragraphs) // 2
            print(
                f"  模型回傳 {returned_count}/{len(paragraphs)} 段，"
                "自動拆成較小批次重試……",
                flush=True,
            )
            first = translate_chunk(paragraphs[:midpoint], cfg, previous_context, _repeat_retry=murasaki)
            continued_context = "\n".join(
                part for part in [previous_context, *first] if part
            )
            second = translate_chunk(paragraphs[midpoint:], cfg, continued_context, _repeat_retry=murasaki)
            return first + second
        raise RuntimeError(
            f"模型回傳 {returned_count} 段，但預期 1 段。"
        )
    results = [normalize_output(normalize_text(str(x)), cfg["target_language"]) for x in translations]
    context_lines = [normalize_output(line.strip(), cfg["target_language"])
                     for line in previous_context.splitlines() if len(line.strip()) >= 10]
    source_text = "\n".join(paragraphs)
    copied_context = any(line in target and line not in source_text
                         for line in context_lines for target in results)
    repeated = any(results[i] == results[j] and paragraphs[i] != paragraphs[j]
                   and len(results[i]) >= 10
                   for i in range(len(results)) for j in range(i))
    for target in results:
        lines = [line.strip() for line in target.splitlines() if len(line.strip()) >= 10]
        repeated = repeated or len(lines) != len(set(lines))
    if copied_context or repeated:
        if cfg.get("_batch_degraded"):
            cfg["_batch_degraded"]()
        if _context_retry:
            raise RuntimeError("译文重复前文或不同原文返回相同译文，未保存该批次。")
        print("  译文疑似重复前文，移除译文参考并逐段重试……", flush=True)
        return [translate_chunk([paragraph], cfg, "", _context_retry=True, _repeat_retry=murasaki)[0]
                for paragraph in paragraphs]
    return results


def make_batches(
    paragraphs: list[str], limit: int, max_items: int = 40
) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if current and (size + len(paragraph) > limit or len(current) >= max_items):
            batches.append(current)
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph)
    if current:
        batches.append(current)
    return batches


class TranslationCancelled(Exception):
    """The user stopped translation, including while waiting for a retry."""


def run_translation_batch(operation, cfg: dict[str, Any], label: str):
    """Retry only a failed model batch; callers persist successful batches."""
    retries = max(0, int(cfg.get("translation_max_retries", 5)))
    delay = max(0.0, float(cfg.get("translation_retry_delay_seconds", 10)))
    cancelled = cfg.get("_translation_cancelled", lambda: False)

    def check_cancelled():
        if cancelled():
            raise TranslationCancelled()

    for attempt in range(retries + 1):
        check_cancelled()
        try:
            return operation()
        except (requests.RequestException, RuntimeError) as exc:
            if isinstance(exc, requests.HTTPError):
                status = exc.response.status_code if exc.response is not None else None
                if status not in (408, 429, 500, 502, 503, 504):
                    raise
            elif isinstance(exc, requests.RequestException):
                if not isinstance(exc, (requests.ConnectionError, requests.Timeout,
                                        requests.exceptions.ChunkedEncodingError)):
                    raise
            elif any(marker in str(exc).lower() for marker in (
                "not found", "does not exist", "unauthorized", "forbidden",
            )):
                raise
            if attempt == retries:
                raise
            if cfg.get("_batch_degraded"):
                cfg["_batch_degraded"]()
            wait = min(delay * (2 ** min(attempt, 10)), 60.0)
            print(f"  [{label}自動重試] {exc}；{wait:g} 秒後重試 "
                  f"({attempt + 1}/{retries})，保留已完成進度。", flush=True)
            if cfg.get("_translation_activity"):
                cfg["_translation_activity"](
                    f"{cfg.get('_activity_label', label)} · {wait:g} 秒后重试 "
                    f"{attempt + 1}/{retries} · {exc}")
            # Short sleeps keep GUI cancellation responsive during backoff.
            remaining = wait
            while remaining > 0:
                check_cancelled()
                interval = min(remaining, 0.2)
                time.sleep(interval)
                remaining -= interval


def translate_episode(episode: Episode, cfg: dict[str, Any], work_dir: Path) -> list[str]:
    with TranslationCache(work_dir) as cache:
        return _translate_episode(episode, cfg, cache)


def _translate_episode(episode: Episode, cfg: dict[str, Any], cache: TranslationCache) -> list[str]:
    from translator.translation.translation_prompt import has_prompt_echo
    check_translation_cancelled(cfg)
    translated: list[str] = []
    missing: list[str] = []
    positions: list[int] = []
    keys: list[str] = []
    chapter_hash = compute_content_hash(episode.paragraphs)
    for i, paragraph in enumerate(episode.paragraphs):
        key_source = json.dumps(
            [3, cfg["model"], cfg["target_language"], cfg.get("glossary", {}),
             cfg.get("temperature", 0.2), cfg.get("context_chars", 1200),
             episode.url, chapter_hash, i, paragraph],
            ensure_ascii=False,
            sort_keys=True,
        )
        if uses_murasaki_profile(cfg):
            key_source += "|murasaki-v0.2-profile-2|" + cfg.get("murasaki_thinking_mode", "auto")
        elif uses_hy_mt_30b_profile(cfg):
            key_source += f"|hy-mt2-30b-profile-2|{reference_context_limit(cfg)}|{cfg.get('hy_mt_num_ctx', 4096)}"
        key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
        keys.append(key)
        cached = cache.get(key)
        if cached is not None and not cfg.get("_force_retranslate") and not has_prompt_echo(cached, [paragraph]):
            translated.append(cached)
        else:
            translated.append("")
            missing.append(paragraph)
            positions.append(i)

    progress = cfg.get("_episode_progress")
    completed = len(translated) - len(missing)
    if progress:
        progress(completed, len(translated))
    live = cfg.get("_episode_live")
    if live:
        live(list(translated))
    if missing:
        adaptive = cfg.get("adaptive_translation_batches", True)
        controller = cfg.get("_adaptive_batcher") or AdaptiveBatcher(cfg)
        local = dict(cfg)
        if adaptive:
            local["_batch_degraded"] = controller.mark_degraded
        cursor = 0
        batch_no = 0
        print(f"需要翻译 {len(missing)} 段，{'自适应' if adaptive else '固定'}分批。")
        if adaptive:
            print(f"  {controller.preset.name} · 当前目标 {controller.chars} 字 / "
                  f"{controller.items} 段 · 上限 {controller.ceiling} 字 · "
                  f"运行上下文 {controller.context_length} token（还会按参考文本预算缩小）")
        while cursor < len(missing):
            batch_no += 1
            context = "\n".join(x for x in translated[: positions[cursor]] if x)
            if adaptive:
                context_limit = (reference_context_limit(cfg) if uses_hy_mt_30b_profile(cfg)
                                 else int(cfg.get("context_chars", 1200)))
                reference_chars = min(len(context), context_limit) + len(glossary_text(cfg))
                if uses_murasaki_profile(cfg):
                    from translator.translation.murasaki_profile import relevant_glossary
                    reference_cfg = {**cfg, "glossary": relevant_glossary(missing[cursor:], cfg.get("glossary", {}))}
                    reference_chars = len(glossary_text(reference_cfg))
                batch = controller.take(missing[cursor:], reference_chars)
            else:
                batch = make_batches(missing[cursor:], int(cfg.get("translation_chunk_chars", 2200)),
                                     int(cfg.get("translation_chunk_paragraphs", 40)))[0]
            local["_activity_label"] = f"批次 {batch_no} · {len(batch)} 段 · 原文 {sum(map(len, batch))} 字"
            print(f"  {local['_activity_label']}（{'自适应' if adaptive else '固定'}）……", flush=True)
            started = time.monotonic()
            result = run_translation_batch(
                lambda: translate_chunk(batch, local, context), local, "翻譯"
            )
            if adaptive:
                controller.observe(time.monotonic() - started)
            check_translation_cancelled(cfg)
            updates = []
            for source, target in zip(batch, result):
                pos = positions[cursor]
                translated[pos] = target
                updates.append((keys[pos], target))
                cursor += 1
            cache.save(updates)
            if progress:
                progress(completed + cursor, len(translated))
            if live:
                live(list(translated))
    else:
        print("本章已命中本機翻譯快取。")
    if cfg.get("check_glossary", True):
        translated = report_and_check_compliance(episode, translated, cfg, label="初譯")
    result = [normalize_output(value, cfg["target_language"]) for value in translated]
    if live:
        live(result)
    return result


def add_or_update_project(
    episode: Episode,
    translations: list[str],
    work_dir: Path,
    cfg: dict[str, Any],
    images: list[dict[str, str]],
    build_now: bool = True,
) -> Path | None:
    project_path = work_dir / "project.json"
    project = load_json(project_path, {})
    if not project.get("base_epub"):
        raise ValueError("当前项目没有中文母版 EPUB，请先使用菜单 4 导入。")
    fingerprint = hashlib.sha256(
        "".join(re.sub(r"\s+", "", p) for p in episode.paragraphs).encode("utf-8")
    ).hexdigest()
    translation_iter = iter(translations)
    blocks: list[dict[str, str]] = []
    for block in episode.blocks:
        if block.get("type") == "text":
            blocks.append({"type": "text", "translation": next(translation_iter, "")})
        elif block.get("type") == "image":
            blocks.append({
                "type": "image", "key": block.get("url", ""), "alt": block.get("alt", "")
            })
    record = {
        "url": episode.url,
        "title": episode.episode_title,
        "model": cfg["model"],
        "fingerprint": fingerprint,
        "japanese": episode.paragraphs,
        "draft_translation": translations,
        "translation": translations,
        "review_status": "single-stage",
        "review_model": None,
        "blocks": blocks,
        "images": images,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    for i, chapter in enumerate(project["chapters"]):
        old_fingerprint = chapter.get("fingerprint") or hashlib.sha256(
            "".join(re.sub(r"\s+", "", p) for p in chapter.get("japanese", [])).encode("utf-8")
        ).hexdigest()
        if chapter["url"] == episode.url or old_fingerprint == fingerprint:
            project["chapters"][i] = record
            break
    else:
        project["chapters"].append(record)
    project["epub_dirty"] = True
    atomic_json(project_path, project)
    if not build_now:
        return None
    output = build_extended_epub(project, work_dir)
    project["epub_dirty"] = False
    atomic_json(project_path, project)
    return output


def download_episode_images(
    context: BrowserContext, episode: Episode, work_dir: Path
) -> list[dict[str, str]]:
    assets = work_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, str]] = []
    image_blocks = [b for b in episode.blocks if b.get("type") == "image" and b.get("url")]
    for index, block in enumerate(image_blocks, 1):
        url = block["url"]
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        try:
            response = context.request.get(url, timeout=60_000)
            if not response.ok:
                print(f"  插图下载失败（HTTP {response.status}）：{url}")
                continue
            media = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
            ext = mimetypes.guess_extension(media) or Path(urlparse(url).path).suffix or ".jpg"
            if ext == ".jpe":
                ext = ".jpg"
            local = assets / f"{key}{ext}"
            if not local.exists():
                local.write_bytes(response.body())
            saved.append({
                "key": url,
                "local_path": str(local.relative_to(work_dir)),
                "media_type": media,
                "alt": block.get("alt", ""),
            })
            print(f"  已保存插图 {index}/{len(image_blocks)}")
        except Exception as exc:
            print(f"  插图下载失败：{exc}")
    return saved


def save_last_project(work_dir: Path) -> None:
    atomic_json(APP_DIR / "last-project.json", {"work_dir": str(work_dir.resolve())})


def load_last_project() -> Path:
    data = load_json(APP_DIR / "last-project.json", {})
    work_dir = Path(data.get("work_dir", ""))
    if not work_dir.exists() or not (work_dir / "project.json").exists():
        raise ValueError("尚未建立可續接的作品專案，請先使用菜单 4 匯入 EPUB。")
    return work_dir


def _episode_from_source(chapter: SourceChapter) -> Episode:
    return Episode(url=chapter.url, work_title="", episode_title=chapter.title,
                   paragraphs=chapter.paragraphs, blocks=chapter.blocks)


def _translated_chapter(chapter: SourceChapter, translations: list[str]) -> SourceChapter:
    iterator = iter(translations)
    blocks = []
    for block in chapter.blocks:
        if block.get("type") == "text":
            blocks.append({"type": "text", "text": next(iterator, "")})
        else:
            blocks.append(dict(block))
    return SourceChapter(url=chapter.url, title=chapter.title, paragraphs=translations,
                         blocks=blocks, images=chapter.images)


class MissingProjectSource(ValueError):
    """An older translation project needs its original EPUB once."""


def project_exists(work_dir: Path) -> bool:
    return any((work_dir / name).exists() for name in ("translation-project.json", "project.json"))


def load_review_project(work_dir: Path, source: Path | None = None) -> dict:
    path = work_dir / "translation-project.json"
    if not path.exists():
        project = load_json(work_dir / "project.json", {})
        if not project:
            raise ValueError("找不到有效项目。")
        return project
    state = load_translation_state(path)
    missing = [i for i, ch in enumerate(state["chapters"]) if "source_paragraphs" not in ch]
    if missing:
        source = source or (Path(state["source_path"]) if state.get("source_path") else None)
        if source is None or not source.is_file():
            raise MissingProjectSource("旧翻译项目尚未保存原文，请选择创建项目时的原始 EPUB。")
        if hashlib.sha256(source.read_bytes()).hexdigest() != state["source_hash"]:
            raise ValueError("所选 EPUB 与项目原文不匹配。")
        _, originals = extract_epub_chapters(source, work_dir / "assets")
        for index in missing:
            record = state["chapters"][index]
            if index >= len(originals) or record["url"] != originals[index].url or record["source_hash"] != compute_content_hash(originals[index].paragraphs):
                raise ValueError("原文章节与旧项目不匹配，无法恢复对应关系。")
            record["source_paragraphs"] = originals[index].paragraphs
        state["source_path"] = str(source.resolve())
        save_translation_state(path, state, missing)
    return state


def project_glossary(cfg: dict, work_dir: Path, project: dict) -> list:
    local = dict(cfg)
    local["glossary"] = output_glossary(cfg, project.get("language", cfg.get("target_language", "繁體中文")))
    return get_effective_glossary(local, work_dir)


def audit_project(project: dict, entries: list) -> list:
    results = []
    for chapter in project.get("chapters", []):
        source = chapter.get("source_paragraphs") or chapter.get("japanese", [])
        target = chapter.get("translation") or chapter.get("paragraphs", [])
        if not source and not target and any(block.get("type") == "image" for block in chapter.get("blocks", [])):
            continue
        if not source or len(source) != len(target):
            raise ValueError(f"章节《{chapter.get('title', '')}》缺少完整的原文/译文对应关系，不能判定稽核通过。")
        violations, _ = check_glossary_compliance(source, target, entries)
        results.extend((chapter.get("title", ""), violation) for violation in violations)
        # Older projects may not record an output language. Keep their glossary
        # audit available without guessing which translation checks apply.
        if project.get("language") and language_code(project["language"]) != "ja":
            results.extend((chapter.get("title", ""), warning)
                           for warning in check_translation_quality(source, target))
    return results


def rebuild_project(work_dir: Path, project: dict) -> Path:
    if project.get("base_epub"):
        return build_extended_epub(project, work_dir)
    metadata = project.get("metadata") or {"title": project.get("work_title", work_dir.name),
                                           "author": project.get("author", ""),
                                           "description": project.get("description", "")}
    language = project.get("language", "繁體中文")
    chapters = [SourceChapter(url=ch.get("url", ""), title=ch["title"],
                              paragraphs=ch.get("translation") or ch.get("paragraphs", []),
                              blocks=ch.get("blocks", []), images=ch.get("images", []))
                for ch in project["chapters"]]
    # Imported text projects can contain paths relative to their project folder.
    for chapter in chapters:
        chapter.images = [dict(image) for image in chapter.images]
        for image in chapter.images:
            if image.get("local_path") and not Path(image["local_path"]).is_absolute():
                image["local_path"] = str(work_dir / image["local_path"])
    output = work_dir / f"{safe_name(metadata['title'])}_{language_suffix(language)}.epub"

    def build(items, destination):
        temporary = destination.with_suffix(".tmp.epub")
        translated_source_epub(metadata, items, temporary, language)
        temporary.replace(destination)

    build(chapters, output)
    if (work_dir / "translation-project.json").exists():
        for start in range(0, len(chapters), 10):
            end = min(start + 10, len(chapters))
            part = work_dir / f"{output.stem}_{start + 1:04d}-{end:04d}.epub"
            build(chapters[start:end], part)
    return output


def retranslate_project(work_dir: Path, cfg: dict, terms: list[str], progress=None,
                        *, paragraph_location: tuple[str, int, str] | None = None,
                        paragraph_locations: list[tuple[int, int, str, str]] | None = None) -> Path:
    if paragraph_locations is None:
        project = load_review_project(work_dir)
    elif (work_dir / "translation-project.json").exists():
        project = load_translation_state(work_dir / "translation-project.json")
    else:
        project = load_json(work_dir / "project.json", {})
    local = dict(cfg)
    local["target_language"] = project.get("language", cfg.get("target_language", "繁體中文"))
    local["glossary"] = entries_to_dict(project_glossary(cfg, work_dir, project))
    if paragraph_locations is not None:
        if paragraph_location is not None or not paragraph_locations:
            raise ValueError("请选择要重译的段落")
        selected = {}
        for chapter_index, paragraph_number, original_text, translated_text in paragraph_locations:
            if (isinstance(chapter_index, bool) or not isinstance(chapter_index, int)
                    or isinstance(paragraph_number, bool) or not isinstance(paragraph_number, int)
                    or not 1 <= chapter_index <= len(project["chapters"])):
                raise ValueError("段落位置无效，请重新检查项目")
            record = project["chapters"][chapter_index - 1]
            source = record.get("source_paragraphs") or record.get("japanese", [])
            target = record.get("translation") or record.get("paragraphs", [])
            if (not 1 <= paragraph_number <= len(source) or len(source) != len(target)
                    or source[paragraph_number - 1] != original_text
                    or target[paragraph_number - 1] != translated_text):
                raise ValueError("段落内容已变化，请重新检查后选择重译")
            selected.setdefault(chapter_index, set()).add(paragraph_number - 1)
        affected = [{"index": index, "affected_paragraphs": sorted(positions)}
                    for index, positions in sorted(selected.items())]
    elif paragraph_location is None:
        affected = find_affected_chapters(project["chapters"], terms)
    else:
        chapter_title, paragraph_number, original_text = paragraph_location
        matches = []
        for chapter_index, chapter in enumerate(project["chapters"], 1):
            source = chapter.get("source_paragraphs") or chapter.get("japanese", [])
            if (chapter.get("title") == chapter_title
                    and 1 <= paragraph_number <= len(source)
                    and source[paragraph_number - 1] == original_text):
                matches.append(chapter_index)
        if len(matches) != 1:
            raise ValueError("无法唯一定位要重译的段落，请重新检查项目。")
        affected = [{"index": matches[0], "affected_paragraphs": [paragraph_number - 1]}]
    for number, item in enumerate(affected, 1):
        check_translation_cancelled(local)
        index = item["index"] - 1
        record = project["chapters"][index]
        original = record.get("source_paragraphs") or record.get("japanese", [])
        translations = list(record.get("translation") or record.get("paragraphs", []))
        if not original or len(original) != len(translations):
            raise ValueError(f"章节《{record['title']}》缺少完整的原文/译文对应关系。")
        # Translate only affected paragraphs, retaining adjacent translated context.
        for pos in item["affected_paragraphs"]:
            if local.get("_task_stage"):
                local["_task_stage"]("translation", f"{record['title']} · 第 {pos + 1} 段 · 局部重译")
            context = "\n".join(translations[:pos])
            result = run_translation_batch(
                lambda: translate_chunk([original[pos]], local, context), local, "局部重译")
            check_translation_cancelled(local)
            translations[pos] = result[0]
        blocks = []
        iterator = iter(translations)
        source_blocks = record.get("blocks") or [{"type": "text"} for _ in translations]
        if sum(block.get("type") == "text" for block in source_blocks) != len(translations):
            raise ValueError(f"章节《{record['title']}》的排版段落与译文不一致。")
        for block in source_blocks:
            block = dict(block)
            if block.get("type") == "text":
                value = next(iterator)
                block["text"] = value
                if project.get("base_epub"):
                    block["translation"] = value
            blocks.append(block)
        record.update(blocks=blocks, paragraphs=translations)
        if "translation" in record or project.get("base_epub"):
            record["translation"] = translations
            record["draft_translation"] = translations
        project["epub_dirty"] = True
        if (work_dir / "translation-project.json").exists():
            save_translation_state(work_dir / "translation-project.json", project, [index])
        else:
            atomic_json(work_dir / "project.json", project)
        if progress:
            progress(number / len(affected) * 100, record["title"])
    check_translation_cancelled(local)
    output = rebuild_project(work_dir, project)
    project["epub_dirty"] = False
    if (work_dir / "translation-project.json").exists():
        save_translation_state(work_dir / "translation-project.json", project)
    else:
        atomic_json(work_dir / "project.json", project)
    return output


def translation_work_dir(source: Path, cfg: dict[str, Any], language: str) -> Path:
    """Reuse matching legacy projects, otherwise isolate each output language."""
    code = language_code(language)
    root = Path(cfg["output_dir"])
    if cfg.get("_resume_work_dir"):
        folder = Path(cfg["_resume_work_dir"]).resolve()
        if not folder.is_relative_to(root.resolve()):
            raise ValueError("续译项目位于输出目录之外")
        saved = load_json(folder / "translation-project.json", {})
        if not saved or language_code(saved.get("language", "")) != code:
            raise ValueError("续译项目不存在或翻译语言不匹配")
        return folder
    for ending in ("_中文翻譯", "_中文翻译"):
        legacy = root / safe_name(source.stem + ending)
        saved = load_json(legacy / "translation-project.json", {})
        if saved and language_code(saved.get("language", "")) == code and (
            saved.get("source_hash") == hashlib.sha256(source.read_bytes()).hexdigest()
        ):
            return legacy
    return root / safe_name(source.stem + "_翻譯") / code


def translate_epub_language(source: Path, cfg: dict[str, Any], language: str,
                            progress=None, *, chapter_limit: int | None = None) -> Path:
    """One resumable language job, shared by the GUI and command line."""
    check_translation_cancelled(cfg)
    language = language_name(language)
    normalize_output("", language)  # Check the converter before doing model work.
    work_dir = translation_work_dir(source, cfg, language)
    work_dir.mkdir(parents=True, exist_ok=True)
    if cfg.get("_translation_project_ready"):
        cfg["_translation_project_ready"](work_dir)
    if cfg.get("_stream_metadata") is not None:
        metadata, chapters = dict(cfg["_stream_metadata"]), cfg["_stream_prefix"]
    else:
        metadata, chapters = extract_epub_chapters(source, work_dir / "assets")
    if cfg.get("_translation_metadata_ready"):
        cfg["_translation_metadata_ready"](metadata)
    source_chapters = chapters
    source_chapter_count = len(chapters)
    streaming = cfg.get("_chapter_stream") is not None
    if streaming:
        chapters = cfg["_chapter_stream"]
        source_chapter_count = cfg["_stream_total"]
    total_chapters = source_chapter_count
    if chapter_limit is not None:
        if streaming:
            raise ValueError("在线逐章翻译不能同时设置试译范围")
        if isinstance(chapter_limit, bool) or not isinstance(chapter_limit, int) or chapter_limit < 1:
            raise ValueError("试译章节数必须是大于零的整数。")
        chapters = chapters[:chapter_limit]
        total_chapters = len(chapters)
    chapter_cfg = dict(cfg)
    chapter_cfg["target_language"] = language
    if uses_murasaki_profile(chapter_cfg):
        chapter_cfg["translation_chunk_chars"] = min(
            1536, int(chapter_cfg.get("translation_chunk_chars", 2200)))
    if cfg.get("adaptive_translation_batches", True):
        context_length = 4096
        try:
            if not cfg.get("ollama_url"):
                raise ValueError("没有配置 Ollama 地址")
            response = requests.get(cfg["ollama_url"].rstrip("/") + "/api/ps", timeout=5)
            response.raise_for_status()
            model = next((item for item in response.json().get("models", [])
                          if item.get("name") == cfg["model"] or item.get("model") == cfg["model"]), {})
            if model.get("context_length"):
                context_length = int(model["context_length"])
            else:
                # An unloaded model is absent from /api/ps. Read its configured
                # window instead of treating native GGUF capacity as runtime RAM.
                response = requests.post(
                    cfg["ollama_url"].rstrip("/") + "/api/show",
                    json={"model": cfg["model"]}, timeout=5)
                response.raise_for_status()
                parameters = response.json().get("parameters", "")
                match = re.search(r"(?m)^num_ctx\s+(\d+)\s*$", parameters)
                context_length = int(match.group(1)) if match else 4096
        except (requests.RequestException, ValueError, TypeError):
            pass
        if uses_hy_mt_30b_profile(chapter_cfg):
            # The profile explicitly requests this window, so planning must use
            # the same value even when /api/ps still reports the old allocation.
            context_length = int(chapter_cfg.get("hy_mt_num_ctx", 4096))
        chapter_cfg["_adaptive_batcher"] = AdaptiveBatcher(chapter_cfg, context_length)
    chapter_cfg["glossary"] = output_glossary(cfg, language)
    entries = get_effective_glossary(chapter_cfg, work_dir)
    chapter_cfg["glossary"] = entries_to_dict(entries)
    if entries and not (work_dir / "glossary.json").exists():
        save_project_glossary(work_dir, entries)
    chapter_cfg["glossary"] = output_glossary(
        {"glossary_by_language": {language_code(language): chapter_cfg["glossary"]}}, language
    )
    cancelled = cfg.get("_translation_cancelled", lambda: False)

    def check_cancelled():
        if cancelled():
            raise TranslationCancelled()

    completed_units = 0
    total_units = (1 + bool(metadata.get("description"))
                   + (0 if streaming else sum(1 + len(ch.paragraphs) for ch in chapters)))
    completed_chapters = 0
    current_index = 0

    def report(message):
        if progress:
            percent = completed_chapters / total_chapters * 99 if streaming else completed_units / total_units * 99
            progress(percent, f"{language} · {message}")

    def translate(ep, label, reading=None):
        nonlocal completed_units
        check_cancelled()
        if cfg.get("_task_stage"):
            cfg["_task_stage"]("translation", label)
        def episode_progress(done, total):
            if progress:
                if streaming:
                    percent = ((current_index - 1 + done / max(1, total)) / total_chapters * 99
                               if current_index else 0)
                else:
                    percent = (completed_units + done) / total_units * 99
                progress(percent, f"{language} · {label} · 段落 {done}/{total}")

        local_cfg = dict(chapter_cfg)
        local_cfg["_episode_progress"] = episode_progress
        if cfg.get("_translation_activity"):
            local_cfg["_translation_activity"] = lambda message: cfg["_translation_activity"](
                f"{language} · {label} · {message}")
        local_cfg.pop("_episode_live", None)
        if reading and cfg.get("_reading_update"):
            local_cfg["_episode_live"] = lambda texts: cfg["_reading_update"](
                language, reading, ep.episode_title, list(ep.paragraphs),
                [normalize_output(t, language) for t in texts])
        drafts = translate_episode(ep, local_cfg, work_dir)
        completed_units += len(ep.paragraphs)
        return drafts

    values = [metadata.get("title", source.stem)]
    if metadata.get("description"):
        values.append(metadata["description"])
    final = translate(Episode(url="epub://metadata", work_title="", episode_title="作品資訊",
                              paragraphs=values, blocks=[{"type": "text", "text": v} for v in values]), "作品信息")
    metadata["title"] = final[0]
    if len(final) > 1:
        metadata["description"] = final[1]
    book = TranslationBook(source, work_dir, metadata, language, source_chapters,
                           bilingual=cfg.get("bilingual_output", False))
    for index, chapter in enumerate(chapters, 1):
        current_index = index
        check_cancelled()
        label = f"章节 {index}/{total_chapters} · {chapter.title}"
        report(label)
        print(f"[{language} {index}/{total_chapters}] {chapter.title}", flush=True)
        if not book.completed(index, chapter):
            title_ep = Episode(url=chapter.url + "#title", work_title="", episode_title=chapter.title,
                               paragraphs=[chapter.title], blocks=[{"type": "text", "text": chapter.title}])
            title = translate(title_ep, f"{label} · 标题")[0]
            episode = _episode_from_source(chapter)
            episode.episode_title = title
            translated = _translated_chapter(chapter, translate(episode, label, index))
            translated.title = title
            book.save(index, chapter, translated)
        else:
            completed_units += 1 + len(chapter.paragraphs)
            if cfg.get("_reading_update"):
                record = book.state["chapters"][index - 1]
                cfg["_reading_update"](language, index, record["title"], chapter.paragraphs,
                                       [normalize_output(t, language) for t in record["paragraphs"]])
        report(f"{label} · 已保存")
        if cfg.get("_capture_first_terms"):
            from translator.glossary.first_terms import capture_terms
            record = book.state["chapters"][index - 1]
            report(f"{label} · 保存首次译名")
            if cfg.get("_task_stage"):
                cfg["_task_stage"]("glossary", f"{label} · 正在整理术语")
            additions = capture_terms(chapter.paragraphs, record["paragraphs"], chapter_cfg, work_dir)
            chapter_cfg["glossary"].update(entries_to_dict(additions))
            if cfg.get("_task_stage"):
                cfg["_task_stage"]("glossary", f"{label} · 已完成（新增 {len(additions)} 条）")
        completed_chapters = index
        if cfg.get("_chapter_completed"):
            cfg["_chapter_completed"](index)
    check_cancelled()
    if streaming:
        if completed_chapters != total_chapters:
            raise ValueError("获取章节未完成，保留已翻译进度。")
        final_source = cfg["_stream_finalize"]()
        final_metadata, _ = extract_epub_chapters(final_source, work_dir / "assets")
        for key in ("cover_path", "cover_media_type"):
            if key in final_metadata:
                book.metadata[key] = final_metadata[key]
        book.state["source_hash"] = hashlib.sha256(final_source.read_bytes()).hexdigest()
        book.state["source_path"] = str(final_source.resolve())
        save_translation_state(book.path, book.state)
    report("正在生成 EPUB")
    if chapter_limit is None:
        output = book.finish(total_chapters)
    else:
        # A preview must never replace an existing full-book export or truncate its project.
        output = book.output.with_name(f"{book.output.stem}_试译_0001-{len(chapters):04d}.epub")
        book._build(1, len(chapters), output)
        book.state["source_chapter_count"] = source_chapter_count
        save_translation_state(book.path, book.state)
    if progress:
        progress(100.0, f"{language} · 已完成")
    if chapter_limit is None:
        book.state["output_path"] = str(output.resolve())
        save_translation_state(book.path, book.state)
    return output
