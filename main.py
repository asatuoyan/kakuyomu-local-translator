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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from adaptive_batches import AdaptiveBatcher
from murasaki_profile import (uses_murasaki_profile, translation_payload,
                              translation_only, ThinkingBudgetExceeded)
from hy_mt_profile import (uses_hy_mt_30b_profile, reference_context_limit,
                           translation_payload as hy_mt_translation_payload)
from app_config import (APP_DIR, CONFIG_PATH, DEFAULT_MODEL, TRANSLATION_MODELS,
                        load_config as read_app_config, update_config)
from project_storage import (TranslationCache, atomic_json, load_json,
                             load_translation_state, save_translation_state)
from quality_checks import check_translation_quality
from languages import LANGUAGES, language_code, language_name, language_suffix, normalize_output, output_glossary
import random
from playwright.sync_api import BrowserContext, Page, sync_playwright

from epub_append import build_extended_epub, create_project_from_epub, inspect_epub
from glossary_manager import (
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
from source_epub import (
    SourceChapter, build_source_epub, compute_content_hash, download_binary,
    download_chapter_images, extract_epub_chapters, extract_source_chapter,
    extract_work_info, get_chapter_id, import_cookie_json, normalize_work_url,
    open_work_page, translated_source_epub,
)
from text_importer import (
    COMMON_CHAPTER_PATTERNS,
    DEFAULT_SPLIT_REGEX,
    create_project_from_text,
    detect_text_language,
    get_language_display_name,
    import_text_source,
    import_text_to_epub,
)


@dataclass
class Episode:
    url: str
    work_title: str
    episode_title: str
    paragraphs: list[str]
    blocks: list[dict[str, str]] = field(default_factory=list)


def select_epub_file(title: str = "選擇已有中文章節的 EPUB") -> Path:
    return select_file(title=title, filetypes=[("EPUB 電子書", "*.epub"), ("所有檔案", "*.*")])


def select_file(
    title: str = "選擇檔案",
    filetypes: list[tuple[str, str]] | None = None,
) -> Path:
    if filetypes is None:
        filetypes = [("術語表檔案", "*.csv;*.xlsx;*.xls;*.json"), ("所有檔案", "*.*")]
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askopenfilename(
            title=title,
            filetypes=filetypes,
        )
        root.destroy()
        if selected:
            return Path(selected)
    except Exception:
        pass
    value = input(f"請輸入檔案完整路徑（{title}）：").strip().strip('"')
    return Path(value)


def load_config() -> dict[str, Any]:
    return read_app_config(CONFIG_PATH, APP_DIR)


def safe_name(value: str, fallback: str = "未命名作品") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:100] or fallback


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\u3000]+", " ", value)
    return value.strip()


def extract_episode(page: Page) -> Episode:
    data = page.evaluate(
        r"""
        () => {
          const first = (selectors) => {
            for (const s of selectors) {
              const e = document.querySelector(s);
              if (e && e.innerText && e.innerText.trim()) return e;
            }
            return null;
          };
          const titleEl = first([
            '.p-novel__title', '.novel_subtitle',
            '[data-testid="episode-title"]', '.widget-episodeTitle',
            'header h1', 'main h1', 'article h1', 'h1'
          ]);
          const bodyEl = first([
            '.p-novel__body', '#novel_honbun', '.p-novel__text',
            '[data-testid="episode-body"]', '.widget-episodeBody',
            '.js-episode-body', '[itemprop="articleBody"]', 'article'
          ]);
          const workEl = first([
            '.p-novel__series', '.novel_title', '.contents_sublist',
            '[data-testid="work-title"]', '.widget-workTitle',
            'a[itemprop="item"]', 'header h2'
          ]);
          let workTitle = workEl ? workEl.innerText.trim() : '';
          const og = document.querySelector('meta[property="og:title"]');
          const episodeTitle = titleEl ? titleEl.innerText.trim() : document.title;
          if (!workTitle && og && og.content) {
            workTitle = og.content.replace(episodeTitle, '').replace(/[\s\-–—|｜]+$/g, '').trim();
          }
          if (!workTitle) {
            const parts = document.title.split(/[\s\-–—|｜]+/);
            if (parts.length > 1) {
              workTitle = parts[0].trim();
            } else {
              workTitle = document.title.trim();
            }
          }
          let paragraphs = [];
          let blocks = [];
          if (bodyEl) {
            const nodes = [...bodyEl.querySelectorAll('p')];
            paragraphs = nodes.length
              ? nodes.map(p => p.innerText.trim()).filter(Boolean)
              : bodyEl.innerText.split(/\n+/).map(s => s.trim()).filter(Boolean);
            const ordered = [...bodyEl.querySelectorAll('p, img')];
            const seenImages = new Set();
            for (const node of ordered) {
              if (node.tagName === 'P') {
                const value = node.innerText.trim();
                if (value) blocks.push({type: 'text', text: value});
              } else if (node.tagName === 'IMG') {
                const src = node.currentSrc || node.src || node.dataset.src;
                if (src && !seenImages.has(src)) {
                  seenImages.add(src);
                  blocks.push({type: 'image', url: new URL(src, location.href).href, alt: node.alt || ''});
                }
              }
            }
            if (!blocks.some(x => x.type === 'text')) {
              blocks = paragraphs.map(text => ({type: 'text', text}));
            }
          }
          return { workTitle, episodeTitle, paragraphs, blocks };
        }
        """
    )
    host = urlparse(page.url).hostname or ""
    if not ("kakuyomu.jp" in host or "syosetu.com" in host):
        raise ValueError("目前頁面不是 Kakuyomu 或 小說家になろう。")
    paragraphs = [normalize_text(p) for p in data["paragraphs"] if normalize_text(p)]
    if not paragraphs:
        raise ValueError("找不到章節正文；請確認目前開啟的是可正常閱讀的小說章節。")
    blocks: list[dict[str, str]] = []
    for block in data.get("blocks", []):
        if block.get("type") == "text" and normalize_text(block.get("text", "")):
            blocks.append({"type": "text", "text": normalize_text(block["text"])})
        elif block.get("type") == "image" and block.get("url"):
            blocks.append({"type": "image", "url": block["url"], "alt": block.get("alt", "")})
    return Episode(
        url=page.url.split("#", 1)[0],
        work_title=normalize_text(data["workTitle"]),
        episode_title=normalize_text(data["episodeTitle"]),
        paragraphs=paragraphs,
        blocks=blocks,
    )


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


def ensure_model(cfg: dict[str, Any], model: str | None = None, interactive: bool = True) -> bool:
    model = model or cfg["model"]
    names = installed_models(cfg)
    if model_is_installed(model, names):
        return True
    if not interactive:
        available = "、".join(sorted(names)) or "無"
        raise RuntimeError(
            f"Ollama 找不到模型 {model}。已安裝的模型：{available}。"
            "請在 config.json 設定與 Ollama 完全相同的模型名稱。"
        )
    answer = input(f"尚未安装 {model}。现在由 Ollama 自动下载？[Y/n] ").strip().lower()
    if answer == "n":
        print("已取消模型切换。")
        return False
    pull_model(cfg, model)
    return True


def select_model(cfg: dict[str, Any], allow_cancel: bool = False) -> bool:
    print("\n选择翻译模型：")
    choices = list(TRANSLATION_MODELS.items())
    for index, (label, _) in enumerate(choices, 1):
        print(f"  {index}. {label}")
    local_choice = str(len(choices) + 1)
    custom_choice = str(len(choices) + 2)
    print(f"  {local_choice}. 选择 Ollama 已部署的本地模型")
    print(f"  {custom_choice}. 手动输入 Ollama 模型名称")
    if allow_cancel:
        print("  Enter. 保持当前模型")
    choice = input("请选择：").strip()
    if allow_cancel and not choice:
        return False
    if choice == local_choice:
        try:
            names = sorted(name for name in installed_models(cfg) if name)
        except RuntimeError as exc:
            print(str(exc))
            return False
        if not names:
            print("当前 Ollama 服务没有已部署的模型。")
            return False
        for index, name in enumerate(names, 1):
            print(f"  {index}. {name}")
        selection = input("请选择本地模型（Enter 取消）：").strip()
        if selection not in {str(i) for i in range(1, len(names) + 1)}:
            return False
        model = names[int(selection) - 1]
    elif choice == custom_choice:
        model = input("请输入完整模型名称（含标签，Enter 取消）：").strip()
        if not model:
            return False
    elif choice in {str(i) for i in range(1, len(choices) + 1)}:
        model = choices[int(choice) - 1][1]
    else:
        print("无效选择。")
        return False
    candidate = dict(cfg)
    candidate["model"] = model
    if not ensure_model(candidate):
        return False
    cfg["model"] = candidate["model"]
    print(f"当前模型：{cfg['model']}")
    return True


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
            raise requests.HTTPError(
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
                    raise RuntimeError("Ollama 输出达到长度限制，译文未完成。")
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
    from translation_prompt import build_prompt, has_prompt_echo
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
    from translation_prompt import has_prompt_echo
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
                    from murasaki_profile import relevant_glossary
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


def select_active_page(context: BrowserContext) -> Page:
    pages = [p for p in context.pages if not p.is_closed()]
    if not pages:
        return context.new_page()
    targets = [p for p in pages if "kakuyomu.jp" in p.url or "syosetu.com" in p.url]
    return targets[-1] if targets else pages[-1]


def save_last_project(work_dir: Path) -> None:
    atomic_json(APP_DIR / "last-project.json", {"work_dir": str(work_dir.resolve())})


def load_last_project() -> Path:
    data = load_json(APP_DIR / "last-project.json", {})
    work_dir = Path(data.get("work_dir", ""))
    if not work_dir.exists() or not (work_dir / "project.json").exists():
        raise ValueError("尚未建立可續接的作品專案，請先使用菜单 4 匯入 EPUB。")
    return work_dir


def import_epub(cfg: dict[str, Any]) -> Path | None:
    path = select_epub_file()
    if not path.exists() or path.suffix.lower() != ".epub":
        raise ValueError("找不到有效的 EPUB 文件。")
    output_root = Path(cfg["output_dir"])
    info = inspect_epub(path)
    work_title = info["title"]
    work_dir = output_root / safe_name(work_title)
    work_dir.mkdir(parents=True, exist_ok=True)
    existing = load_json(work_dir / "project.json", {})
    if existing:
        base = work_dir / existing.get("base_epub", "base-original.epub")
        if not base.exists() or hashlib.sha256(base.read_bytes()).digest() != hashlib.sha256(path.read_bytes()).digest():
            raise ValueError("已有同名作品專案，但 EPUB 母版不同；請使用原母版或更換作品資料夾。")
        save_last_project(work_dir)
        print(f"已接續既有作品專案（{len(existing.get('chapters', []))} 章），不覆蓋舊文章。")
        return work_dir
    project = create_project_from_epub(path, work_dir)
    print(f"\n作品：{work_title}")
    print(f"作者：{project.get('author') or 'EPUB 未提供'}")
    scores = project.get("variant_scores", {})
    print(
        f"检测到现有 EPUB 使用：{project['language']} "
        f"（繁体特征 {scores.get('traditional', 0)}，简体特征 {scores.get('simplified', 0)}）"
    )
    answer = input("输入 y 确认并建立续接项目，其他键取消：").strip().lower()
    if answer != "y":
        print("已取消；没有修改原 EPUB。")
        return None
    atomic_json(work_dir / "project.json", project)
    epub_path = build_extended_epub(project, work_dir)
    save_last_project(work_dir)
    print(f"\n中文母版已完整复制：{epub_path}")
    return work_dir


def browser_mode(cfg: dict[str, Any], fixed_work_dir: Path | None = None) -> None:
    output_root = Path(cfg["output_dir"])
    output_root.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = _launch_context(p, cfg)
        page = select_active_page(context)
        if "kakuyomu.jp" not in page.url and "syosetu.com" not in page.url:
            page.goto("https://kakuyomu.jp/")
        print("\n請在瀏覽器中登入並開啟 Kakuyomu 或 小說家になろう 的小說章節。")
        print("回到此視窗按 Enter 翻譯當前章；输入 m 切换模型；输入 q 结束。")
        print(f"当前模型：{cfg['model']}\n")
        pending_epub_updates = 0
        active_work_dir = fixed_work_dir

        if active_work_dir and (active_work_dir / "project.json").exists():
            startup_project = load_json(active_work_dir / "project.json", {})
            if startup_project.get("epub_dirty"):
                epub_path = build_extended_epub(startup_project, active_work_dir)
                startup_project["epub_dirty"] = False
                atomic_json(active_work_dir / "project.json", startup_project)
                print(f"已恢复上次未打包的校对终稿：{epub_path}")

        def flush_epub() -> None:
            nonlocal pending_epub_updates
            if pending_epub_updates and active_work_dir:
                project = load_json(active_work_dir / "project.json", {})
                epub_path = build_extended_epub(project, active_work_dir)
                project["epub_dirty"] = False
                atomic_json(active_work_dir / "project.json", project)
                print(f"已更新 EPUB：{epub_path}")
                pending_epub_updates = 0

        while True:
            command = input("> ").strip().lower()
            if command in {"q", "quit", "exit"}:
                flush_epub()
                break
            if command in {"m", "model"}:
                try:
                    flush_epub()
                    select_model(cfg, allow_cancel=True)
                except Exception as exc:
                    print(f"模型切换失败：{exc}", file=sys.stderr)
                continue
            try:
                page = select_active_page(context)
                episode = extract_episode(page)
                print(f"作品：{episode.work_title}")
                print(f"章節：{episode.episode_title}（{len(episode.paragraphs)} 段）")
                work_dir = fixed_work_dir or output_root / safe_name(episode.work_title)
                active_work_dir = work_dir
                work_dir.mkdir(parents=True, exist_ok=True)
                if not (work_dir / "project.json").exists():
                    raise ValueError("请先使用菜单 4 导入已有中文 EPUB，再追加网页章节。")
                project = load_json(work_dir / "project.json", {})
                chapter_cfg = dict(cfg)
                chapter_cfg["target_language"] = project.get("language", cfg["target_language"])
                drafts = translate_episode(episode, chapter_cfg, work_dir)
                translations = drafts
                images = download_episode_images(context, episode, work_dir)
                next_pending = pending_epub_updates + 1
                build_now = next_pending >= 10
                epub_path = add_or_update_project(
                    episode, translations, work_dir, chapter_cfg, images,
                    build_now=build_now,
                )
                pending_epub_updates = 0 if build_now else next_pending
                save_last_project(work_dir)
                if epub_path:
                    print(f"完成并更新 EPUB：{epub_path}\n")
                else:
                    print(
                        f"校对终稿已保存；累计 {pending_epub_updates}/10 章后更新 EPUB。\n"
                    )
            except KeyboardInterrupt:
                print("\n已中止本章；已完成的翻譯批次仍保留在快取中。")
            except Exception as exc:
                print(f"錯誤：{exc}", file=sys.stderr)
        flush_epub()
        context.close()


def _launch_context(
    playwright: Any, cfg: dict[str, Any], *, prompt_for_cookies: bool = True,
) -> BrowserContext:
    browser = cfg.get("browser_engine", "msedge")
    profile_base = Path(cfg["browser_profile_dir"])
    profile = profile_base if browser == "chromium" else profile_base.with_name(
        f"{profile_base.name}-{browser}"
    )
    options: dict[str, Any] = {
        "headless": bool(cfg.get("headless", False)),
        "viewport": {"width": 1280, "height": 900},
        "locale": "ja-JP",
    }
    if browser in {"msedge", "chrome"}:
        options["channel"] = browser
    elif browser == "custom":
        executable = str(cfg.get("browser_executable", "")).strip()
        if not executable or not Path(executable).is_file():
            raise ValueError("自訂瀏覽器執行檔不存在，請重新選擇瀏覽器。")
        options["executable_path"] = executable
    elif browser == "chromium" and not Path(playwright.chromium.executable_path).is_file():
        print("首次使用 Playwright Chromium，正在安装浏览器……", flush=True)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    try:
        context = playwright.chromium.launch_persistent_context(str(profile), **options)
    except Exception as exc:
        name = {"msedge": "Microsoft Edge", "chrome": "Google Chrome",
                "chromium": "Playwright Chromium", "custom": "自訂瀏覽器"}.get(browser, browser)
        raise RuntimeError(
            f"無法啟動 {name}。請確認瀏覽器已安裝，或重新執行並選擇其他瀏覽器。\n{exc}"
        ) from exc
    cookie_path = APP_DIR / "cookies.json"
    if prompt_for_cookies and cookie_path.exists():
        answer = input("发现 cookies.json，是否导入其中的 Cookie？[y/N] ").strip().lower()
        if answer == "y":
            print(f"已导入 {import_cookie_json(context, cookie_path)} 条 Cookie。")
    return context


def _choose_browser(cfg: dict[str, Any]) -> None:
    current = cfg.get("browser_engine", "msedge")
    labels = {"1": "msedge", "2": "chrome", "3": "chromium", "4": "custom"}
    defaults = {value: key for key, value in labels.items()}
    print("\n選擇瀏覽器（使用各自的程式專用設定檔，登入一次後會保留）：")
    print("  1. Microsoft Edge（推薦）")
    print("  2. Google Chrome")
    print("  3. Playwright Chromium")
    print("  4. 自訂 Chromium 瀏覽器執行檔")
    choice = input(f"請輸入 1-4［預設 {defaults.get(current, '1')}］：").strip()
    engine = labels.get(choice, current if current in defaults else "msedge")
    cfg["browser_engine"] = engine
    if engine == "custom":
        value = input("請輸入瀏覽器 exe 完整路徑：").strip().strip('"')
        cfg["browser_executable"] = value


def _select_range(total: int) -> tuple[int, int]:
    while True:
        value = input(f"输入起止章节编号（1-{total}，例如 21-40）：").strip()
        match = re.fullmatch(r"(\d+)\s*[-~～]\s*(\d+)", value)
        if match:
            start, end = map(int, match.groups())
            if 1 <= start <= end <= total:
                return start, end
        print("范围无效，请重新输入。")


def create_japanese_epub(cfg: dict[str, Any]) -> None:
    work_url = normalize_work_url(input("作品網址（Kakuyomu 或 小說家になろう）："))
    output_root = Path(cfg["output_dir"])
    with sync_playwright() as p:
        context = _launch_context(p, cfg)
        page = select_active_page(context)
        if "kakuyomu.jp" in work_url:
            redirected = open_work_page(page, work_url, allow_login=True)
            if redirected:
                print("Kakuyomu 要求登入。請在瀏覽器中完成登入，並等待它返回作品頁。")
            else:
                print("瀏覽器已開啟作品頁；如需登入，請先在瀏覽器中完成登入。")
            input("確認瀏覽器已顯示作品頁後，回到這裡按 Enter 讀取目錄：")
        else:
            page.goto(work_url, wait_until="domcontentloaded", timeout=120_000)
            print("瀏覽器已開啟作品頁。")
            input("確認瀏覽器已顯示作品頁後，回到這裡按 Enter 讀取目錄：")
        work = extract_work_info(page, work_url)
        print(f"\n作品：{work.title}\n作者：{work.author or '未提供'}\n可访问目录：{len(work.episodes)} 章")
        for index, item in enumerate(work.episodes, 1):
            print(f"{index:4d}. {item['title']}")
        start, end = _select_range(len(work.episodes))
        selected = work.episodes[start - 1:end]
        work_dir = output_root / safe_name(work.title) / f"日文原文_{start:04d}-{end:04d}"
        assets_dir = work_dir / "source-assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        cache_path = work_dir / "source-cache.json"
        cache: dict[str, Any] = load_json(cache_path, {})
        chapters: list[SourceChapter] = []
        # Kakuyomu may temporarily block bursty automated access.  A conservative
        # floor plus jitter avoids a machine-like fixed request cadence.
        delay = max(float(cfg.get("request_delay_seconds", 8.0)), 8.0)
        for offset, item in enumerate(selected, start):
            chapter_url = item["url"]
            expected_cid = get_chapter_id(chapter_url)
            record = cache.get(chapter_url) or cache.get(expected_cid)
            if record and record.get("paragraphs"):
                curr_hash = record.get("content_hash") or compute_content_hash(record["paragraphs"])
                chapter = SourceChapter(
                    url=chapter_url,
                    title=record["title"],
                    paragraphs=record["paragraphs"],
                    blocks=record["blocks"],
                    chapter_id=record.get("chapter_id", expected_cid),
                    content_hash=curr_hash,
                )
                for image in record.get("images", []):
                    local = work_dir / image["local_path"]
                    if local.exists():
                        image_dict = dict(image)
                        image_dict["data"] = local.read_bytes()
                        chapter.images.append(image_dict)
                print(f"[{offset}/{end}] 使用下載快取（ID: {chapter.chapter_id}）：{chapter.title}")
            else:
                print(f"[{offset}/{end}] 下載中：{item['title']}")
                chapter = extract_source_chapter(page, item["url"], item["title"])
                download_chapter_images(context, chapter)
                saved_images = []
                for image in chapter.images:
                    local = assets_dir / image["name"]
                    if not local.exists():
                        local.write_bytes(image["data"])
                    saved_images.append({k: v for k, v in image.items() if k != "data"} | {
                        "local_path": str(local.relative_to(work_dir))
                    })
                cache[item["url"]] = {
                    "chapter_id": chapter.chapter_id,
                    "content_hash": chapter.content_hash,
                    "title": chapter.title,
                    "paragraphs": chapter.paragraphs,
                    "blocks": chapter.blocks,
                    "images": saved_images,
                    "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                atomic_json(cache_path, cache)
                time.sleep(delay + random.uniform(1.0, 3.5))
            chapters.append(chapter)
        cover = None
        if work.cover_url:
            try:
                cover = download_binary(context, work.cover_url)
            except Exception as exc:
                print(f"封面下载失败，将生成无封面 EPUB：{exc}")
        output = work_dir / f"{safe_name(work.title)}_日文_{start:04d}-{end:04d}.epub"
        build_source_epub(work, chapters, output, cover=cover)
        atomic_json(work_dir / "source-project.json", {
            "work_url": work.url, "title": work.title, "author": work.author,
            "start": start, "end": end, "chapter_urls": [c.url for c in chapters],
        })
        context.close()
    print(f"\n日文 EPUB 已生成：{output}")


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


class TranslationBook:
    """Persist completed chapters and build small EPUBs before merging the full book."""

    def __init__(self, source: Path, work_dir: Path, metadata: dict[str, Any], language: str,
                 source_chapters: list[SourceChapter] | None = None, bilingual: bool = False):
        self.work_dir = work_dir
        self.bilingual = bilingual
        self.language = language
        self.path = work_dir / "translation-project.json"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        saved = load_translation_state(self.path)
        if saved and language_code(saved.get("language", "")) != language_code(language):
            raise ValueError("翻譯專案的目標語言已變更，請使用另一個輸出資料夾。")
        if saved and saved.get("source_hash") != source_hash:
            records = saved["chapters"]
            if source_chapters is None or len(source_chapters) < len(records) or any(
                record["url"] != source_chapters[i].url or
                record["source_hash"] != compute_content_hash(source_chapters[i].paragraphs)
                for i, record in enumerate(records)
            ):
                raise ValueError("原文已變更或章節順序不符，請先核對變更，再使用另一個輸出資料夾。")
            saved["source_hash"] = source_hash
            saved["epub_dirty"] = True
            save_translation_state(self.path, saved)
        self.state = saved or {"source_hash": source_hash, "language": language,
                               "metadata": metadata, "chapters": []}
        self.state["source_path"] = str(source.resolve())
        self.metadata = self.state["metadata"]
        for key in ("title", "description"):
            if self.metadata.get(key):
                self.metadata[key] = normalize_output(self.metadata[key], language)
        self.output = work_dir / f"{safe_name(self.metadata['title'])}_{language_suffix(language)}.epub"
        if bilingual:
            self.output = self.output.with_name(self.output.stem + "_双语对照.epub")

    def completed(self, index: int, chapter: SourceChapter) -> bool:
        records = self.state["chapters"]
        if index > len(records):
            return False
        if records[index - 1]["url"] != chapter.url or (
            records[index - 1]["source_hash"] != compute_content_hash(chapter.paragraphs)
        ):
            raise ValueError("原文章節已變更，請使用另一個輸出資料夾以免覆蓋已譯內容。")
        if "source_paragraphs" not in records[index - 1]:
            records[index - 1]["source_paragraphs"] = chapter.paragraphs
            save_translation_state(self.path, self.state, [index - 1])
        return True

    def save(self, index: int, original: SourceChapter, translated: SourceChapter) -> None:
        if index != len(self.state["chapters"]) + 1:
            raise ValueError("章節必須依原書順序接續寫入。")
        self.state["chapters"].append({
            "url": original.url, "source_hash": compute_content_hash(original.paragraphs),
            "source_paragraphs": original.paragraphs,
            "title": translated.title, "paragraphs": translated.paragraphs,
            "blocks": translated.blocks,
            "images": [{k: str(v) if isinstance(v, Path) else v for k, v in image.items()
                        if k != "data"} for image in translated.images],
        })
        save_translation_state(self.path, self.state, [index - 1])

    def _chapters(self, start: int, end: int) -> list[SourceChapter]:
        chapters = [SourceChapter(url=record["url"], title=record["title"],
                              paragraphs=record["paragraphs"], blocks=record["blocks"],
                              images=record["images"])
                for record in self.state["chapters"][start - 1:end]]
        if self.bilingual:
            for chapter, record in zip(chapters, self.state["chapters"][start - 1:end]):
                originals = record.get("source_paragraphs")
                if originals is None or len(originals) != len(chapter.paragraphs):
                    raise ValueError("双语对照输出缺少匹配的原文，请重新读取原始 EPUB。")
                blocks = chapter.blocks or [{"type": "text", "text": p}
                                            for p in chapter.paragraphs]
                paired = []
                position = 0
                for block in blocks:
                    if block.get("type") == "text":
                        paired.append({"type": "text", "text": originals[position],
                                       "role": "original"})
                        paired.append({**block, "role": "translation"})
                        position += 1
                    else:
                        paired.append(dict(block))
                chapter.blocks = paired
        return chapters

    def _build(self, start: int, end: int, output: Path) -> Path:
        temporary = output.with_suffix(".tmp.epub")
        translated_source_epub(self.metadata, self._chapters(start, end), temporary, self.language)
        temporary.replace(output)
        return output

    def checkpoint(self, index: int, total: int) -> list[Path]:
        outputs = []
        if index % 10 == 0 or index == total:
            start = ((index - 1) // 10) * 10 + 1
            part = self.work_dir / f"{self.output.stem}_{start:04d}-{index:04d}.epub"
            if not part.exists() or self.state.get("epub_dirty"):
                outputs.append(self._build(start, index, part))
        if index % 100 == 0 and (
            self.state.get("merged_up_to", 0) < index or not self.output.exists()
        ):
            outputs.append(self._build(1, index, self.output))
            self.state["merged_up_to"] = index
            save_translation_state(self.path, self.state)
        return outputs

    def finish(self, total: int) -> Path:
        if len(self.state["chapters"]) != total:
            raise ValueError("尚有章節未完成，不能合併全書。")
        output = self._build(1, total, self.output)
        self.state["merged_up_to"] = total
        self.state["epub_dirty"] = False
        save_translation_state(self.path, self.state)
        return output


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
                        *, paragraph_location: tuple[str, int, str] | None = None) -> Path:
    project = load_review_project(work_dir)
    local = dict(cfg)
    local["target_language"] = project.get("language", cfg.get("target_language", "繁體中文"))
    local["glossary"] = entries_to_dict(project_glossary(cfg, work_dir, project))
    if paragraph_location is None:
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
            from first_translation_terms import capture_terms
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


def translate_source_epub(
    cfg: dict[str, Any],
    append_to_existing: bool,
    source_override: Path | None = None,
) -> None:
    if source_override is not None and source_override.exists():
        source = source_override
    else:
        source = select_epub_file("选择需要翻译的日文 EPUB")
    if not source.exists() or source.suffix.lower() != ".epub":
        raise ValueError("找不到有效的日文 EPUB。")
    if not append_to_existing:
        choices = list(LANGUAGES)
        print("\n輸出語言（可用逗號多選，各自產生 EPUB）：")
        for index, code in enumerate(choices, 1):
            print(f"  {index}. {language_name(code)}")
        selected = input("請選擇 [1]：").strip() or "1"
        indices = list(dict.fromkeys(re.split(r"[,，\s]+", selected)))
        if any(not item.isdigit() or not 1 <= int(item) <= len(choices) for item in indices):
            raise ValueError("請輸入有效的語言編號。")
        for item in indices:
            output = translate_epub_language(source, cfg, choices[int(item) - 1])
            print(f"已完成：{output}")
        return
    work_dir = import_epub(cfg)
    if not work_dir:
        return
    project = load_json(work_dir / "project.json", {})
    language = project.get("language", cfg["target_language"])
    assets_dir = work_dir / "assets"
    metadata, chapters = extract_epub_chapters(source, assets_dir)
    effective_entries = get_effective_glossary(cfg, work_dir)
    if not (work_dir / "glossary.json").exists() and effective_entries:
        save_project_glossary(work_dir, effective_entries)
    chapter_cfg = dict(cfg)
    chapter_cfg["target_language"] = language
    chapter_cfg["glossary"] = entries_to_dict(effective_entries)
    if not append_to_existing:
        meta_values = [metadata.get("title", source.stem)]
        if metadata.get("description"):
            meta_values.append(metadata["description"])
        meta_episode = Episode(url="epub://metadata", work_title="", episode_title="作品信息",
                               paragraphs=meta_values,
                               blocks=[{"type": "text", "text": value} for value in meta_values])
        meta_draft = translate_episode(meta_episode, chapter_cfg, work_dir)
        meta_final = (
            meta_draft
        )
        metadata["title"] = meta_final[0]
        if len(meta_final) > 1:
            metadata["description"] = meta_final[1]
    book = TranslationBook(source, work_dir, metadata, language) if not append_to_existing else None
    print(f"读取到 {len(chapters)} 个正文章节；目标：{language}。")
    for index, chapter in enumerate(chapters, 1):
        print(f"\n[{index}/{len(chapters)}] {chapter.title}")
        if book and book.completed(index, chapter):
            print("已接續上次完成的章節。")
            for output in book.checkpoint(index, len(chapters)):
                print(f"已寫入 EPUB：{output}")
            continue
        episode = _episode_from_source(chapter)
        title_episode = Episode(url=chapter.url + "#title", work_title="",
                                episode_title=chapter.title, paragraphs=[chapter.title],
                                blocks=[{"type": "text", "text": chapter.title}])
        title_draft = translate_episode(title_episode, chapter_cfg, work_dir)
        translated_title = (
            title_draft[0]
        )
        episode.episode_title = translated_title
        drafts = translate_episode(episode, chapter_cfg, work_dir)
        translations = drafts
        if append_to_existing:
            images = []
            for image in chapter.images:
                local = Path(image["local_path"])
                images.append({"key": image["key"], "local_path": str(local.relative_to(work_dir)),
                               "media_type": image["media_type"], "alt": image.get("alt", "")})
            add_or_update_project(episode, translations, work_dir, chapter_cfg, images,
                                                build_now=False)
            if index % 10 == 0 or index == len(chapters):
                current = load_json(work_dir / "project.json", {})
                count = 10 if index % 10 == 0 else index % 10
                part = work_dir / f"{safe_name(current['work_title'])}-續譯_{index - count + 1:04d}-{index:04d}.epub"
                build_extended_epub({**current, "chapters": current["chapters"][-count:]}, work_dir, part)
                print(f"已寫入階段 EPUB：{part}")
            if index % 100 == 0 or index == len(chapters):
                current = load_json(work_dir / "project.json", {})
                output = build_extended_epub(current, work_dir)
                current["epub_dirty"] = False
                atomic_json(work_dir / "project.json", current)
                print(f"已合併到原書：{output}")
        else:
            translated_chapter = _translated_chapter(chapter, translations)
            translated_chapter.title = translated_title
            book.save(index, chapter, translated_chapter)
            for output in book.checkpoint(index, len(chapters)):
                print(f"已寫入 EPUB：{output}")
    if book:
        print(f"已合併全部章節：{book.finish(len(chapters))}")
    elif len(chapters) % 100 == 0:
        current = load_json(work_dir / "project.json", {})
        print(f"已再次合併全部章節：{build_extended_epub(current, work_dir)}")
    print("\n处理完成。")


def scan_glossary_flow(cfg: dict[str, Any]) -> None:
    print("\n選擇實體掃描來源：")
    print("  1. 從日文 EPUB 檔案掃描")
    print("  2. 從目前已有作品專案/下載快取掃描")
    print("  3. 從 Kakuyomu / 小說家になろう 網址在線採樣掃描")
    source_choice = input("請輸入 1-3：").strip()
    texts: list[str] = []

    if source_choice == "1":
        path = select_epub_file("選擇要掃描實體的日文 EPUB")
        if not path.exists():
            print("找不到該 EPUB 檔案。")
            return
        tmp_dir = Path(cfg["output_dir"]) / ".tmp_scan"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        _, chapters = extract_epub_chapters(path, tmp_dir)
        for ch in chapters:
            texts.extend(ch.paragraphs)
    elif source_choice == "2":
        try:
            work_dir = load_last_project()
        except Exception:
            work_dir = None
        if not work_dir or not work_dir.exists():
            path_str = input("請輸入作品專案資料夾路徑：").strip().strip('"')
            work_dir = Path(path_str) if path_str else None
        if not work_dir or not work_dir.exists():
            print("作品專案資料夾不存在。")
            return
        cache_path = work_dir / "source-cache.json"
        if cache_path.exists():
            cache = load_json(cache_path, {})
            for rec in cache.values():
                texts.extend(rec.get("paragraphs", []))
        elif (work_dir / "project.json").exists():
            proj = load_json(work_dir / "project.json", {})
            for ch in proj.get("chapters", []):
                texts.extend(ch.get("japanese", []) or ch.get("paragraphs", []))
    elif source_choice == "3":
        work_url = normalize_work_url(input("請輸入小說網址（Kakuyomu 或 小說家になろう）："))
        _choose_browser(cfg)
        with sync_playwright() as p:
            context = _launch_context(p, cfg)
            page = select_active_page(context)
            work = extract_work_info(page, work_url)
            print(f"作品：{work.title}，目錄共 {len(work.episodes)} 章。")
            sample_count = min(5, len(work.episodes))
            print(f"正在採樣前 {sample_count} 章進行實體掃描……")
            for ep in work.episodes[:sample_count]:
                ch = extract_source_chapter(page, ep["url"], ep["title"])
                texts.extend(ch.paragraphs)
            context.close()
    else:
        print("已取消掃描。")
        return

    if not texts:
        print("未提取到有效小說段落。")
        return

    print(f"\n已採樣 {len(texts)} 段文字，正由 AI 深度識別人物名、地名、組織、技能、魔法、稱號及特殊設定……")
    ensure_model(cfg)
    entries = scan_novel_entities(texts, cfg)
    if not entries:
        print("未能識別出實體條目。")
        return

    print(f"\n掃描成功！識別到 {len(entries)} 筆專有名詞與設定：")
    by_cat: dict[str, list[GlossaryEntry]] = {}
    for e in entries:
        by_cat.setdefault(e.category, []).append(e)
    for cat in VALID_CATEGORIES:
        if cat in by_cat:
            print(f"\n【{cat}】({len(by_cat[cat])} 筆):")
            for e in by_cat[cat]:
                print(f"  - {e.source} => {e.target}" + (f" ({e.note})" if e.note else ""))

    ans = input("\n是否將以上術語合併至目前設定檔 config.json？[Y/n] ").strip().lower()
    if ans != "n":
        existing_entries = dict_to_entries(cfg.get("glossary", {}))
        merged = merge_glossaries(existing_entries, entries, overwrite=True)
        cfg["glossary"] = entries_to_dict(merged)
        update_config({"glossary": cfg["glossary"]}, CONFIG_PATH, APP_DIR)
        print(f"已成功更新至 config.json（目前共 {len(cfg['glossary'])} 條術語）！")

    exp = input("是否將本次生成的術語表匯出為檔案 (CSV / Excel / JSON)？[y/N] ").strip().lower()
    if exp == "y":
        export_path_str = input("請輸入匯出路徑（例如 glossary.xlsx 或 glossary.csv）：").strip().strip('"')
        if export_path_str:
            export_glossary_to_file(entries, export_path_str)
            print(f"術語表已匯出至：{export_path_str}")


def import_glossary_flow(cfg: dict[str, Any]) -> None:
    path = select_file("選擇術語表檔案 (CSV / Excel / JSON)", [
        ("術語表檔案", "*.csv;*.xlsx;*.xls;*.json"),
        ("CSV 表格", "*.csv"),
        ("Excel 試算表", "*.xlsx;*.xls"),
        ("JSON 檔案", "*.json"),
        ("所有檔案", "*.*"),
    ])
    if not path.exists():
        print("檔案不存在。")
        return
    try:
        entries = import_glossary_from_file(path)
        print(f"\n成功讀取 {len(entries)} 筆術語條目：")
        by_cat: dict[str, list[GlossaryEntry]] = {}
        for e in entries:
            by_cat.setdefault(e.category, []).append(e)
        for cat in VALID_CATEGORIES:
            if cat in by_cat:
                print(f"  【{cat}】：{len(by_cat[cat])} 筆")

        mode = input("\n選擇匯入方式：1. 與現有術語表合併  2. 完全覆蓋現有術語表 [1]：").strip()
        if mode == "2":
            cfg["glossary"] = entries_to_dict(entries)
        else:
            existing = dict_to_entries(cfg.get("glossary", {}))
            merged = merge_glossaries(existing, entries, overwrite=True)
            cfg["glossary"] = entries_to_dict(merged)
        update_config({"glossary": cfg["glossary"]}, CONFIG_PATH, APP_DIR)
        print(f"已成功匯入並更新 config.json（目前共 {len(cfg['glossary'])} 條有效術語）。")
    except Exception as exc:
        print(f"匯入失敗：{exc}")


def export_glossary_flow(cfg: dict[str, Any]) -> None:
    entries = dict_to_entries(cfg.get("glossary", {}))
    if not entries:
        print("目前術語表為空，無法匯出。")
        return
    print(f"\n目前有 {len(entries)} 筆術語。選擇匯出格式：")
    print("  1. Excel 試算表 (.xlsx，含分類多工作表)")
    print("  2. CSV 逗號分隔檔 (.csv)")
    print("  3. JSON 格式 (.json)")
    fmt = input("請輸入 1-3 [1]：").strip()
    ext = ".xlsx" if fmt in {"", "1"} else (".csv" if fmt == "2" else ".json")
    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"glossary{ext}"
    custom_path = input(f"匯出路徑 [預設 {out_path}]：").strip().strip('"')
    target = Path(custom_path) if custom_path else out_path
    export_glossary_to_file(entries, target)
    print(f"已匯出術語表至：{target}")


def view_glossary_flow(cfg: dict[str, Any]) -> None:
    entries = dict_to_entries(cfg.get("glossary", {}))
    if not entries:
        print("\n目前尚未設定任何術語。")
        return
    print(f"\n【目前生效術語表（共 {len(entries)} 條）】")
    by_cat: dict[str, list[GlossaryEntry]] = {}
    for e in entries:
        by_cat.setdefault(e.category, []).append(e)
    for cat in VALID_CATEGORIES:
        if cat in by_cat:
            print(f"\n--- 【{cat}】({len(by_cat[cat])} 筆) ---")
            for e in by_cat[cat]:
                print(f"  {e.source} => {e.target}" + (f"  // {e.note}" if e.note else ""))
    for cat, items in by_cat.items():
        if cat not in VALID_CATEGORIES:
            print(f"\n--- 【{cat}】({len(items)} 筆) ---")
            for e in items:
                print(f"  {e.source} => {e.target}" + (f"  // {e.note}" if e.note else ""))


def add_edit_glossary_flow(cfg: dict[str, Any]) -> None:
    print("\n手動新增/編輯術語：")
    src = input("日文原文（例如「アイリス」）：").strip()
    if not src:
        print("日文原文不可為空。")
        return
    tgt = input(f"中文譯名（例如「愛麗絲」）：").strip()
    if not tgt:
        print("中文譯名不可為空。")
        return
    print("選擇類別：1. 人物名  2. 地名  3. 組織  4. 技能  5. 魔法  6. 稱號  7. 特殊設定 [7]：")
    cat_map = {"1": "人物名", "2": "地名", "3": "組織", "4": "技能", "5": "魔法", "6": "稱號", "7": "特殊設定"}
    cat_choice = input("請選擇 1-7：").strip()
    cat = cat_map.get(cat_choice, "特殊設定")
    note = input("備註說明（選填）：").strip()

    entries = dict_to_entries(cfg.get("glossary", {}))
    new_entry = GlossaryEntry(source=src, target=tgt, category=cat, note=note)
    merged = merge_glossaries(entries, [new_entry], overwrite=True)
    cfg["glossary"] = entries_to_dict(merged)
    update_config({"glossary": cfg["glossary"]}, CONFIG_PATH, APP_DIR)
    print(f"已儲存術語：「{src}」 => 「{tgt}」【{cat}】")


def _select_review_project() -> tuple[Path, dict]:
    try:
        work_dir = load_last_project()
    except (ValueError, FileNotFoundError):
        work_dir = None
    if not work_dir or not project_exists(work_dir):
        work_dir = Path(input("请输入项目文件夹路径：").strip().strip('"'))
    try:
        return work_dir, load_review_project(work_dir)
    except MissingProjectSource:
        source = select_epub_file("选择创建旧项目时的原始 EPUB")
        return work_dir, load_review_project(work_dir, source)


def audit_glossary_compliance_flow(cfg: dict[str, Any]) -> None:
    print("\n1. 稽核项目（支持新旧项目）\n2. 手动输入原文/译文测试")
    if input("请选择 [1]：").strip() == "2":
        source = input("原文：").strip()
        target = input("译文：").strip()
        violations, _ = check_glossary_compliance([source], [target], cfg.get("glossary", {}))
        results = [("手动测试", violation) for violation in violations]
    else:
        work_dir, project = _select_review_project()
        results = audit_project(project, project_glossary(cfg, work_dir, project))
    for title, violation in results:
        print(f"《{title}》第 {violation.paragraph_index} 段：{violation.source} → {violation.expected_target}")
    print(f"稽核完成，共 {len(results)} 处术语警示。")


def retranslate_by_term_scope_flow(cfg: dict[str, Any]) -> None:
    work_dir, project = _select_review_project()
    entries = project_glossary(cfg, work_dir, project)
    value = input("请输入要重译的原文术语（逗号分隔，all 表示全部术语）：").strip()
    terms = list(entries_to_dict(entries)) if value.lower() == "all" else [t for t in re.split(r"[,，、\s]+", value) if t]
    affected = find_affected_chapters(project.get("chapters", []), terms)
    if not affected:
        print("没有受影响的段落。")
        return
    count = sum(len(item["affected_paragraphs"]) for item in affected)
    if input(f"将重译 {len(affected)} 章中的 {count} 个段落，继续？[Y/n] ").strip().lower() == "n":
        return
    ensure_model(cfg)
    output = retranslate_project(work_dir, cfg, terms,
                                 lambda percent, title: print(f"{percent:.0f}% {title}"))
    print(f"局部重译完成：{output}")

def heuristic_candidate_flow(cfg: dict[str, Any]) -> None:
    print("\n================ 快��提取候選術語（啟發式） ================")
    print("選擇來源：")
    print("  1. 從日文 EPUB 檔案提取")
    print("  2. 從目前已有專案/下載快取提取")
    src = input("請輸入 1-2 [1]：").strip()
    texts: list[str] = []
    work_dir = None
    if src in {"", "1"}:
        path = select_epub_file("選擇日文 EPUB")
        if not path.exists():
            return
        tmp_dir = Path(cfg["output_dir"]) / ".tmp_scan"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        _, chapters = extract_epub_chapters(path, tmp_dir)
        for ch in chapters:
            texts.extend(ch.paragraphs)
    else:
        try:
            work_dir = load_last_project()
        except Exception:
            work_dir = None
        if not work_dir or not work_dir.exists():
            p_str = input("請輸入專案資料夾路徑：").strip().strip('"')
            work_dir = Path(p_str) if p_str else None
        if not work_dir or not work_dir.exists():
            print("專案不存在。")
            return
        cache_path = work_dir / "source-cache.json"
        if cache_path.exists():
            for rec in load_json(cache_path, {}).values():
                texts.extend(rec.get("paragraphs", []))
        elif (work_dir / "project.json").exists():
            for ch in load_json(work_dir / "project.json", {}).get("chapters", []):
                texts.extend(ch.get("japanese", []) or ch.get("paragraphs", []))

    if not texts:
        print("未提取到文本。")
        return

    candidates = extract_candidate_terms(texts, min_count=2, top_n=80)
    print(f"\n快速統計完成！共發現 {len(candidates)} 個候選名詞：")
    for i, c in enumerate(candidates[:40], 1):
        ctx = f" (例句: {c.contexts[0][:40]}...)" if c.contexts else ""
        print(f"  {i:2d}. 【{c.category}】{c.source}（出現 {c.count} 次）{ctx}")

    ans = input("\n是否將候選術語轉換為待確認條目並匯入？[y/N] ").strip().lower()
    if ans == "y":
        entries = [GlossaryEntry(source=c.source, target=c.source, category=c.category, note=c.note) for c in candidates]
        target_choice = input("儲存至：1. 全域 config.json  2. 作品專屬 glossary.json [1]：").strip()
        if target_choice == "2" and work_dir:
            existing = load_project_glossary(work_dir)
            merged = merge_glossaries(existing, entries, overwrite=False)
            save_project_glossary(work_dir, merged)
            print(f"已儲存至作品專屬術語表：{work_dir / 'glossary.json'}（共 {len(merged)} 條）")
        else:
            existing = dict_to_entries(cfg.get("glossary", {}))
            merged = merge_glossaries(existing, entries, overwrite=False)
            cfg["glossary"] = entries_to_dict(merged)
            update_config({"glossary": cfg["glossary"]}, CONFIG_PATH, APP_DIR)
            print(f"已儲存至全域設定檔 config.json（共 {len(cfg['glossary'])} 條）")


def launch_gui_flow() -> None:
    import subprocess
    gui_script = APP_DIR / "gui.py"
    if not gui_script.exists():
        print("GUI 模組正在準備中……")
    try:
        subprocess.Popen([sys.executable, str(gui_script)])
        print("已成功啟動圖形化介面視窗！")
    except Exception as exc:
        print(f"啟動圖形化介面時發生錯誤：{exc}")


def manage_glossary_menu(cfg: dict[str, Any]) -> None:
    while True:
        print("\n================ 術語表管理 ================")
        print("  1. 自動掃描小說並由 AI 生成術語表（人物/地名/組織/技能/魔法/稱號/特殊設定）")
        print("  2. 快速提取候選術語（啟發式統計秒級提取）")
        print("  3. 從獨立 CSV / Excel (.xlsx) / JSON 檔案匯入術語表")
        print("  4. 匯出術語表為 CSV / Excel (.xlsx) / JSON")
        print("  5. 檢視目前生效的術語表（分類展示 / 作品獨立覆蓋）")
        print("  6. 手動新增或編輯術語")
        print("  7. 檢查既有專案譯文是否違反術語表")
        print("  8. 按術語影響範圍局部重譯（免全本重翻）")
        print("  9. 開啟圖形化介面 (GUI)")
        print("  0. 返回主選單")
        choice = input("請輸入 0-9：").strip()
        if choice in {"0", "q", "quit", "exit"}:
            break
        elif choice == "1":
            scan_glossary_flow(cfg)
        elif choice == "2":
            heuristic_candidate_flow(cfg)
        elif choice == "3":
            import_glossary_flow(cfg)
        elif choice == "4":
            export_glossary_flow(cfg)
        elif choice == "5":
            view_glossary_flow(cfg)
        elif choice == "6":
            add_edit_glossary_flow(cfg)
        elif choice == "7":
            audit_glossary_compliance_flow(cfg)
        elif choice == "8":
            retranslate_by_term_scope_flow(cfg)
        elif choice == "9":
            launch_gui_flow()


def import_text_batch_flow(cfg: dict[str, Any]) -> None:
    print("\n================ TXT / Markdown / UMD / JAR 批次導入 ================")
    print("支援格式：全本 .txt / .md / .umd / .jar 檔案（自動正則分章），或包含多話章節的資料夾（自然排序）")
    path_str = input("請輸入檔案或資料夾路徑：").strip().strip('"')
    if not path_str:
        print("已取消導入。")
        return
    src_path = Path(path_str)
    if not src_path.exists():
        print(f"找不到指定路徑：{src_path}")
        return

    custom_regex = None
    if src_path.is_file() and src_path.suffix.lower() not in {".umd", ".jar"}:
        print("\n章節切分規則：")
        print("  1. 預設標準規則（第X話/章/卷、Chapter X、Markdown # 標題、序章/終章/番外編等）")
        print("  2. Markdown 標題切分（# 或 ##）")
        print("  3. 自訂正則表達式")
        r_choice = input("請選擇切分規則 [1]：").strip()
        if r_choice == "2":
            custom_regex = r"^\s*#{1,3}\s+(.+)$"
        elif r_choice == "3":
            custom_regex = input("請輸入切分章節的正則表達式：").strip()

    title_input = input(f"作品書名 [{src_path.stem}]：").strip() or src_path.stem
    author_input = input("作者名稱 [未知作者]：").strip() or "未知作者"

    try:
        work, chapters = import_text_source(
            src_path,
            title=title_input,
            author=author_input,
            split_pattern=custom_regex,
        )
    except Exception as exc:
        print(f"解析失敗：{exc}")
        return

    sample_paras = []
    for ch in chapters[:10]:
        sample_paras.extend(ch.paragraphs[:3])
    detected_lang = detect_text_language(sample_paras)
    lang_name = get_language_display_name(detected_lang)

    print(f"\n成功解析作品《{work.title}》（作者：{work.author}，自動辨識語系：{lang_name}），共 {len(chapters)} 章。")
    print("章節預覽（前 5 章）：")
    for i, ch in enumerate(chapters[:5], 1):
        char_count = sum(len(p) for p in ch.paragraphs)
        print(f"  {i:3d}. {ch.title} ({len(ch.paragraphs)} 個段落，約 {char_count} 字)")
    if len(chapters) > 5:
        print(f"  ... 其餘 {len(chapters) - 5} 章已略過預覽。")

    print("\n請選擇後續處理方式：")
    if detected_lang != "ja":
        # Chinese novel options
        print("  1. 📦 一鍵打包為中文 EPUB 電子書")
        print("  2. 📋 導入為中文專案（用於術語合規稽核、局部修正或接續新章節）")
        print("  3. ⚡ 作為日文原文專案並開始本機 AI 翻譯")
        print("  4. 📁 僅建立專案資料夾")
        action = input("請輸入 1-4 [1]：").strip() or "1"
        if action == "1":
            output_name = f"{safe_name(work.title)}_中文.epub"
            output_path = Path(cfg["output_dir"]) / output_name
            import_text_to_epub(
                src_path,
                output_path,
                title=work.title,
                author=work.author,
                description=work.description,
                language="繁體中文" if detected_lang == "zh-Hant" else "簡體中文",
                split_pattern=custom_regex,
            )
            print(f"\n中文 EPUB 電子書打包成功：{output_path.resolve()}")
        elif action == "2":
            work_dir = Path(cfg["output_dir"]) / safe_name(f"{work.title}_中文專案")
            create_project_from_text(
                src_path,
                work_dir,
                title=work.title,
                author=work.author,
                description=work.description,
                language="繁體中文" if detected_lang == "zh-Hant" else "簡體中文",
                split_pattern=custom_regex,
                as_translated=True,
            )
            out_epub = Path(cfg["output_dir"]) / f"{safe_name(work.title)}_中文.epub"
            if not out_epub.exists():
                import_text_to_epub(src_path, out_epub, title=work.title, author=work.author, language="中文", split_pattern=custom_regex)
            print(f"\n中文專案已建立完成：{work_dir.resolve()}")
            print("您可使用主選單「6. 術語表管理」->「7. 檢查既有專案譯文是否違反術語表」進行稽核！")
        elif action == "3":
            work_dir = Path(cfg["output_dir"]) / safe_name(f"{work.title}_中文翻译")
            create_project_from_text(
                src_path,
                work_dir,
                title=work.title,
                author=work.author,
                description=work.description,
                language=cfg.get("target_language", "繁體中文"),
                split_pattern=custom_regex,
            )
            temp_epub = Path(cfg["output_dir"]) / f"{safe_name(work.title)}_原文.epub"
            if not temp_epub.exists():
                import_text_to_epub(src_path, temp_epub, title=work.title, author=work.author, split_pattern=custom_regex)
            print(f"\n專案資料夾已成功建立：{work_dir.resolve()}")
            print("檢查 Ollama……")
            installed_models(cfg)
            while not select_model(cfg):
                print("請重新選擇一個模型。")
            translate_source_epub(cfg, append_to_existing=False, source_override=temp_epub)
        else:
            work_dir = Path(cfg["output_dir"]) / safe_name(f"{work.title}_中文翻译")
            create_project_from_text(
                src_path,
                work_dir,
                title=work.title,
                author=work.author,
                description=work.description,
                language=cfg.get("target_language", "繁體中文"),
                split_pattern=custom_regex,
            )
            print(f"\n專案資料夾已成功建立：{work_dir.resolve()}")
    else:
        # Japanese source novel options
        print("  1. ⚡ 建立翻譯專案並立即開始本機 AI 翻譯")
        print("  2. 📦 打包生成日文原文 EPUB（可用於日後整本翻譯或提取術語）")
        print("  3. 📁 僅建立專案資料夾（以便手動配置獨立術語表）")
        action = input("請輸入 1-3 [1]：").strip() or "1"
        if action == "1":
            work_dir = Path(cfg["output_dir"]) / safe_name(f"{work.title}_中文翻译")
            create_project_from_text(
                src_path,
                work_dir,
                title=work.title,
                author=work.author,
                description=work.description,
                language=cfg.get("target_language", "繁體中文"),
                split_pattern=custom_regex,
            )
            temp_epub = Path(cfg["output_dir"]) / f"{safe_name(work.title)}_日文原文.epub"
            if not temp_epub.exists():
                import_text_to_epub(src_path, temp_epub, title=work.title, author=work.author, split_pattern=custom_regex)
            print(f"\n專案資料夾已成功建立：{work_dir.resolve()}")
            print("檢查 Ollama……")
            installed_models(cfg)
            while not select_model(cfg):
                print("請重新選擇一個模型。")
            translate_source_epub(cfg, append_to_existing=False, source_override=temp_epub)
        elif action == "2":
            output_name = f"{safe_name(work.title)}_日文原文.epub"
            output_path = Path(cfg["output_dir"]) / output_name
            import_text_to_epub(
                src_path,
                output_path,
                title=work.title,
                author=work.author,
                description=work.description,
                language="日文",
                split_pattern=custom_regex,
            )
            print(f"\n日文原文 EPUB 打包成功：{output_path.resolve()}")
        else:
            work_dir = Path(cfg["output_dir"]) / safe_name(f"{work.title}_中文翻译")
            create_project_from_text(
                src_path,
                work_dir,
                title=work.title,
                author=work.author,
                description=work.description,
                language=cfg.get("target_language", "繁體中文"),
                split_pattern=custom_regex,
            )
            temp_epub = Path(cfg["output_dir"]) / f"{safe_name(work.title)}_日文原文.epub"
            if not temp_epub.exists():
                import_text_to_epub(src_path, temp_epub, title=work.title, author=work.author, split_pattern=custom_regex)
            print(f"\n專案資料夾已成功建立：{work_dir.resolve()}")


def main() -> int:
    cfg = load_config()
    Path(cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
    print("\n================ Kakuyomu / Syosetu 翻譯工具 ================")
    print("选择工作：")
    print("  1. Kakuyomu / 小說家になろう → 日文原文 EPUB（选择起止章节）")
    print("  2. 日文 EPUB → 多語言 EPUB（可多選）")
    print("  3. 日文 EPUB → 追加到已有中文 EPUB")
    print("  4. 导入已有中文 EPUB，再手动打开网页逐章续接")
    print("  5. 继续上次的网页续接项目")
    print("  6. 術語表管理（AI 實體識別/候選詞提取/多格式匯入匯出/合規檢查/局部重譯）")
    print("  7. 批次導入 TXT / Markdown / UMD / JAR 電子書（製作 EPUB 或建立翻譯專案）")
    print("  8. 開啟圖形化使用者介面 (GUI 簡易操作)")
    mode = input("请输入 1-8：").strip()
    if mode == "8":
        launch_gui_flow()
        return 0
    if mode == "7":
        import_text_batch_flow(cfg)
        return 0
    if mode == "6":
        manage_glossary_menu(cfg)
        return 0
    if mode in {"1", "4", "5"}:
        _choose_browser(cfg)
    if mode == "1":
        create_japanese_epub(cfg)
        return 0
    if mode not in {"2", "3", "4", "5"}:
        print("未选择有效模式。")
        return 0
    print("检查 Ollama……")
    installed_models(cfg)
    while not select_model(cfg):
        print("请重新选择一个模型。")
    if mode == "2":
        translate_source_epub(cfg, append_to_existing=False)
    elif mode == "3":
        translate_source_epub(cfg, append_to_existing=True)
    elif mode == "4":
        work_dir = import_epub(cfg)
        if work_dir and input("\n現在開啟瀏覽器續接剩餘章節？[Y/n] ").strip().lower() != "n":
            browser_mode(cfg, fixed_work_dir=work_dir)
    elif mode == "5":
        browser_mode(cfg, fixed_work_dir=load_last_project())
    else:
        print("未選擇有效模式。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
