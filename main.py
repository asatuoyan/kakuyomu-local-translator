from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
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
    import_text_source,
    import_text_to_epub,
)


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"


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
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            (APP_DIR / "config.example.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["output_dir"] = str((APP_DIR / cfg.get("output_dir", "output")).resolve())
    cfg["browser_profile_dir"] = str(
        (APP_DIR / cfg.get("browser_profile_dir", "browser-profile")).resolve()
    )
    return cfg


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


def ensure_model(cfg: dict[str, Any], model: str | None = None) -> bool:
    model = model or cfg["model"]
    if model_is_installed(model, installed_models(cfg)):
        return True
    answer = input(f"尚未安装 {model}。现在由 Ollama 自动下载？[Y/n] ").strip().lower()
    if answer == "n":
        print("已取消模型切换。")
        return False
    pull_model(cfg, model)
    return True


def select_model(cfg: dict[str, Any], allow_cancel: bool = False) -> bool:
    print("\n选择翻译模型：")
    print("  1. 快速模式  qwen3:8b（较大分块，速度优先）")
    print("  2. 质量模式  qwen3:14b（较小分块，更长前文语境）")
    print("  3. 混合模式  8B 初译后立即由 14B 校对")
    print(f"  4. 自定义模型  {cfg.get('custom_model', cfg.get('model', ''))}")
    if allow_cancel:
        print("  Enter. 保持当前模型")
    choice = input("请选择：").strip()
    if allow_cancel and not choice:
        return False
    profiles = {
        "1": {"model": "qwen3:8b", "translation_chunk_chars": 2800, "context_chars": 900, "profile": "快速", "dual_stage": False},
        "2": {"model": "qwen3:14b", "translation_chunk_chars": 1400, "context_chars": 2600, "profile": "质量", "dual_stage": False},
        "3": {"model": "qwen3:8b", "review_model": "qwen3:14b", "translation_chunk_chars": 2800, "context_chars": 900, "review_chunk_chars": 1400, "review_context_chars": 2600, "profile": "混合", "dual_stage": True},
    }
    if choice == "4":
        current = cfg.get("custom_model", "")
        model = input(f"输入 Ollama 模型名称 [{current}]：").strip() or current
        if not model:
            print("没有填写模型名称。")
            return False
        profile = {
            "model": model,
            "translation_chunk_chars": int(cfg.get("translation_chunk_chars", 2200)),
            "context_chars": int(cfg.get("context_chars", 1200)),
            "profile": "自定义",
            "dual_stage": False,
        }
    elif choice in profiles:
        profile = profiles[choice]
    else:
        print("无效选择。")
        return False
    previous = {key: cfg.get(key) for key in profile}
    cfg.update(profile)
    try:
        if not ensure_model(cfg):
            cfg.update(previous)
            return False
        if cfg.get("dual_stage") and not ensure_model(cfg, cfg["review_model"]):
            cfg.update(previous)
            return False
    except Exception:
        cfg.update(previous)
        raise
    print(
        f"当前：{cfg['profile']}模式 / {cfg['model']} / "
        f"分块约 {cfg['translation_chunk_chars']} 字 / 前文 {cfg['context_chars']} 字"
    )
    if cfg.get("dual_stage"):
        print(
            f"校对：{cfg['review_model']} / 分块约 {cfg['review_chunk_chars']} 字 / "
            f"前文终稿 {cfg['review_context_chars']} 字"
        )
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


def translate_chunk(
    paragraphs: list[str], cfg: dict[str, Any], previous_context: str = ""
) -> list[str]:
    numbered = "\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs, 1))
    system = f"""你是專業日文小說譯者。把輸入翻譯成{cfg['target_language']}。
規則：
1. 保留敘事語氣、角色口吻、段落數及順序，不增刪、摘要或審查內容。
2. 固有名詞保持一致；不確定的日文人名優先保留漢字。
3. 日文引號「」在中文中仍用「」；擬聲詞依語境自然翻譯。
4. 只回傳 JSON，格式為 {{"translations":["第一段", "第二段"]}}。
5. translations 的元素數量必須與輸入段落完全相同。

固定譯名表：
{glossary_text(cfg)}"""
    context_chars = int(cfg.get("context_chars", 1200))
    user = f"前文語境（僅供參考，不要重譯）：\n{previous_context[-context_chars:]}\n\n待翻譯段落：\n{numbered}"
    payload = {
        "model": cfg["model"],
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": cfg.get("temperature", 0.2)},
    }
    base = cfg["ollama_url"].rstrip("/")
    r = requests.post(
        f"{base}/api/chat",
        json=payload,
        timeout=cfg.get("request_timeout_seconds", 600),
    )
    r.raise_for_status()
    content = r.json().get("message", {}).get("content", "")
    try:
        result = json.loads(content)
        translations = result["translations"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"模型沒有回傳有效 JSON：{content[:300]}") from exc
    if not isinstance(translations, list) or len(translations) != len(paragraphs):
        raise RuntimeError(
            f"模型回傳 {len(translations) if isinstance(translations, list) else 0} 段，"
            f"但預期 {len(paragraphs)} 段。請降低 translation_chunk_chars 後重試。"
        )
    return [normalize_text(str(x)) for x in translations]


def make_batches(paragraphs: list[str], limit: int) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if current and size + len(paragraph) > limit:
            batches.append(current)
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph)
    if current:
        batches.append(current)
    return batches


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def translate_episode(episode: Episode, cfg: dict[str, Any], work_dir: Path) -> list[str]:
    cache_path = work_dir / "translation-cache.json"
    cache: dict[str, str] = load_json(cache_path, {})
    translated: list[str] = []
    missing: list[str] = []
    positions: list[int] = []
    keys: list[str] = []
    for i, paragraph in enumerate(episode.paragraphs):
        key_source = json.dumps(
            [cfg["model"], cfg["target_language"], cfg.get("glossary", {}), paragraph],
            ensure_ascii=False,
            sort_keys=True,
        )
        key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
        keys.append(key)
        if key in cache:
            translated.append(cache[key])
        else:
            translated.append("")
            missing.append(paragraph)
            positions.append(i)

    if missing:
        batches = make_batches(missing, int(cfg.get("translation_chunk_chars", 2200)))
        cursor = 0
        print(f"需要翻譯 {len(missing)} 段，共 {len(batches)} 批。")
        for batch_no, batch in enumerate(batches, 1):
            print(f"  翻譯第 {batch_no}/{len(batches)} 批……", flush=True)
            context = "\n".join(x for x in translated[: positions[cursor]] if x)
            result = translate_chunk(batch, cfg, context)
            for source, target in zip(batch, result):
                pos = positions[cursor]
                translated[pos] = target
                cache[keys[pos]] = target
                cursor += 1
            atomic_json(cache_path, cache)
    else:
        print("本章已命中本機翻譯快取。")
    if cfg.get("check_glossary", True):
        translated = report_and_check_compliance(episode, translated, cfg, label="初譯")
    return translated


def make_review_batches(
    originals: list[str], drafts: list[str], limit: int
) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for original, draft in zip(originals, drafts):
        pair_size = len(original) + len(draft)
        if current and size + pair_size > limit:
            batches.append(current)
            current, size = [], 0
        current.append((original, draft))
        size += pair_size
    if current:
        batches.append(current)
    return batches


def review_chunk(
    pairs: list[tuple[str, str]], cfg: dict[str, Any], previous_final: str
) -> list[str]:
    items = "\n".join(
        f"[{index}]\n日文：{original}\n初译：{draft}"
        for index, (original, draft) in enumerate(pairs, 1)
    )
    context_chars = int(cfg.get("review_context_chars", 2600))
    system = f"""你是日中小说译文的资深校对编辑。目标语言是{cfg['target_language']}。
请对照日文原文校订初译，而不是脱离原文重写。
规则：
1. 修正误译、漏译、代词指向、人物口吻、时态、语气和不自然中文。
2. 保持信息完整，不添加原文没有的解释，不删减敏感或困难内容。
3. 固有名词严格遵守术语表，并与前文终稿一致。
4. 保持段落数量和顺序不变。
5. 只返回 JSON：{{"translations":["校对后的第一段", "第二段"]}}。

固定译名表：
{glossary_text(cfg)}"""
    user = (
        f"前文终稿（仅供一致性参考，不要重写）：\n{previous_final[-context_chars:]}"
        f"\n\n本批原文与8B初译：\n{items}"
    )
    payload = {
        "model": cfg["review_model"],
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": min(float(cfg.get("temperature", 0.2)), 0.15)},
    }
    base = cfg["ollama_url"].rstrip("/")
    response = requests.post(
        f"{base}/api/chat", json=payload,
        timeout=cfg.get("request_timeout_seconds", 600),
    )
    response.raise_for_status()
    content = response.json().get("message", {}).get("content", "")
    try:
        translations = json.loads(content)["translations"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"校对模型没有返回有效 JSON：{content[:300]}") from exc
    if not isinstance(translations, list) or len(translations) != len(pairs):
        raise RuntimeError(
            f"校对模型返回 {len(translations) if isinstance(translations, list) else 0} 段，"
            f"但预期 {len(pairs)} 段。"
        )
    return [normalize_text(str(value)) for value in translations]


def proofread_episode(
    episode: Episode, drafts: list[str], cfg: dict[str, Any], work_dir: Path
) -> list[str]:
    cache_path = work_dir / "review-cache.json"
    cache: dict[str, str] = load_json(cache_path, {})
    final: list[str] = []
    missing_pairs: list[tuple[str, str]] = []
    missing_positions: list[int] = []
    keys: list[str] = []
    for index, (original, draft) in enumerate(zip(episode.paragraphs, drafts)):
        source = json.dumps(
            [cfg["review_model"], cfg["target_language"], cfg.get("glossary", {}), original, draft],
            ensure_ascii=False, sort_keys=True,
        )
        key = hashlib.sha256(source.encode("utf-8")).hexdigest()
        keys.append(key)
        if key in cache:
            final.append(cache[key])
        else:
            final.append("")
            missing_pairs.append((original, draft))
            missing_positions.append(index)
    if missing_pairs:
        batches = make_review_batches(
            [pair[0] for pair in missing_pairs],
            [pair[1] for pair in missing_pairs],
            int(cfg.get("review_chunk_chars", 1400)),
        )
        cursor = 0
        print(f"14B 需要校对 {len(missing_pairs)} 段，共 {len(batches)} 批。")
        for batch_no, batch in enumerate(batches, 1):
            print(f"  校对第 {batch_no}/{len(batches)} 批……", flush=True)
            position = missing_positions[cursor]
            previous = "\n".join(value for value in final[:position] if value)
            reviewed = review_chunk(batch, cfg, previous)
            for value in reviewed:
                pos = missing_positions[cursor]
                final[pos] = value
                cache[keys[pos]] = value
                cursor += 1
            atomic_json(cache_path, cache)
    else:
        print("本章已命中14B校对缓存。")
    if cfg.get("check_glossary", True):
        final = report_and_check_compliance(episode, final, cfg, label="14B校對")
    return final


def add_or_update_project(
    episode: Episode,
    translations: list[str],
    work_dir: Path,
    cfg: dict[str, Any],
    images: list[dict[str, str]],
    drafts: list[str] | None = None,
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
        "profile": cfg.get("profile", "自定义"),
        "fingerprint": fingerprint,
        "japanese": episode.paragraphs,
        "draft_translation": drafts or translations,
        "translation": translations,
        "review_status": "reviewed" if drafts is not None else "single-stage",
        "review_model": cfg.get("review_model") if drafts is not None else None,
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
        print(f"当前模型：{cfg.get('profile', '自定义')} / {cfg['model']}\n")
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
                if chapter_cfg.get("dual_stage"):
                    print(f"8B 初译完成，开始由 {chapter_cfg['review_model']} 校对。")
                    translations = proofread_episode(episode, drafts, chapter_cfg, work_dir)
                else:
                    translations = drafts
                images = download_episode_images(context, episode, work_dir)
                next_pending = pending_epub_updates + 1
                build_now = not chapter_cfg.get("dual_stage") or next_pending >= 5
                epub_path = add_or_update_project(
                    episode, translations, work_dir, chapter_cfg, images,
                    drafts=drafts if chapter_cfg.get("dual_stage") else None,
                    build_now=build_now,
                )
                pending_epub_updates = 0 if build_now else next_pending
                save_last_project(work_dir)
                if epub_path:
                    print(f"完成并更新 EPUB：{epub_path}\n")
                else:
                    print(
                        f"校对终稿已保存；累计 {pending_epub_updates}/5 章后更新 EPUB。\n"
                    )
            except KeyboardInterrupt:
                print("\n已中止本章；已完成的翻譯批次仍保留在快取中。")
            except Exception as exc:
                print(f"錯誤：{exc}", file=sys.stderr)
        flush_epub()
        context.close()


def _launch_context(playwright: Any, cfg: dict[str, Any]) -> BrowserContext:
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
    try:
        context = playwright.chromium.launch_persistent_context(str(profile), **options)
    except Exception as exc:
        name = {"msedge": "Microsoft Edge", "chrome": "Google Chrome",
                "chromium": "Playwright Chromium", "custom": "自訂瀏覽器"}.get(browser, browser)
        raise RuntimeError(
            f"無法啟動 {name}。請確認瀏覽器已安裝，或重新執行並選擇其他瀏覽器。\n{exc}"
        ) from exc
    cookie_path = APP_DIR / "cookies.json"
    if cookie_path.exists():
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
    if append_to_existing:
        work_dir = import_epub(cfg)
        if not work_dir:
            return
        project = load_json(work_dir / "project.json", {})
        language = project.get("language", cfg["target_language"])
        assets_dir = work_dir / "assets"
    else:
        language_choice = input("输出中文：1. 简体中文  2. 繁體中文 [2]：").strip()
        language = "简体中文" if language_choice == "1" else "繁體中文"
        work_dir = Path(cfg["output_dir"]) / safe_name(source.stem + "_中文翻译")
        work_dir.mkdir(parents=True, exist_ok=True)
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
            proofread_episode(meta_episode, meta_draft, chapter_cfg, work_dir)
            if chapter_cfg.get("dual_stage") else meta_draft
        )
        metadata["title"] = meta_final[0]
        if len(meta_final) > 1:
            metadata["description"] = meta_final[1]
    translated_chapters: list[SourceChapter] = []
    print(f"读取到 {len(chapters)} 个正文章节；目标：{language}。")
    for index, chapter in enumerate(chapters, 1):
        print(f"\n[{index}/{len(chapters)}] {chapter.title}")
        episode = _episode_from_source(chapter)
        title_episode = Episode(url=chapter.url + "#title", work_title="",
                                episode_title=chapter.title, paragraphs=[chapter.title],
                                blocks=[{"type": "text", "text": chapter.title}])
        title_draft = translate_episode(title_episode, chapter_cfg, work_dir)
        translated_title = (
            proofread_episode(title_episode, title_draft, chapter_cfg, work_dir)[0]
            if chapter_cfg.get("dual_stage") else title_draft[0]
        )
        episode.episode_title = translated_title
        drafts = translate_episode(episode, chapter_cfg, work_dir)
        translations = proofread_episode(episode, drafts, chapter_cfg, work_dir) if chapter_cfg.get("dual_stage") else drafts
        if append_to_existing:
            images = []
            for image in chapter.images:
                local = Path(image["local_path"])
                images.append({"key": image["key"], "local_path": str(local.relative_to(work_dir)),
                               "media_type": image["media_type"], "alt": image.get("alt", "")})
            build_now = (not chapter_cfg.get("dual_stage")) or index % 5 == 0 or index == len(chapters)
            output = add_or_update_project(episode, translations, work_dir, chapter_cfg, images,
                                           drafts=drafts if chapter_cfg.get("dual_stage") else None,
                                           build_now=build_now)
            if output:
                print(f"已更新：{output}")
        else:
            translated_chapter = _translated_chapter(chapter, translations)
            translated_chapter.title = translated_title
            translated_chapters.append(translated_chapter)
            if index % 5 == 0 or index == len(chapters):
                output = work_dir / f"{safe_name(metadata['title'])}_{'繁中' if '繁' in language else '简中'}.epub"
                translated_source_epub(metadata, translated_chapters, output, language)
                print(f"已写入阶段 EPUB：{output}")
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
        atomic_json(CONFIG_PATH, cfg)
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
        atomic_json(CONFIG_PATH, cfg)
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
    atomic_json(CONFIG_PATH, cfg)
    print(f"已儲存術語：「{src}」 => 「{tgt}」【{cat}】")


def audit_glossary_compliance_flow(cfg: dict[str, Any]) -> None:
    print("\n選擇要審查合規性的對象：")
    print("  1. 審查已建立的專案 (project.json)")
    print("  2. 手動輸入日文/中文段落測試")
    opt = input("請輸入 1-2 [1]：").strip()
    glossary = cfg.get("glossary", {})
    if not glossary:
        print("目前尚未設定術語表，請先建立或匯入術語表。")
        return

    if opt in {"", "1"}:
        try:
            work_dir = load_last_project()
        except Exception:
            work_dir = None
        if not work_dir or not (work_dir / "project.json").exists():
            path_str = input("請輸入專案資料夾路徑：").strip().strip('"')
            work_dir = Path(path_str) if path_str else None
        if not work_dir or not (work_dir / "project.json").exists():
            print("找不到有效專案。")
            return
        project = load_json(work_dir / "project.json", {})
        total_violations = 0
        for ch in project.get("chapters", []):
            ch_title = ch.get("title", "")
            ja_paras = ch.get("japanese", []) or ch.get("source_paragraphs", [])
            zh_paras = ch.get("translation", []) or ch.get("paragraphs", [])
            if ja_paras and zh_paras:
                violations, _ = check_glossary_compliance(ja_paras, zh_paras, glossary)
                if violations:
                    print(f"\n【章節：{ch_title}】發現 {len(violations)} 處未符合術語表：")
                    for v in violations:
                        print(f"  - 第 {v.paragraph_index} 段【{v.category}】：原文「{v.source}」-> 應譯為「{v.expected_target}」")
                        print(f"    原文：{v.original_text[:60]}")
                        print(f"    譯文：{v.translated_text[:60]}")
                    total_violations += len(violations)
        if total_violations == 0:
            print("\n審查完成！專案中所有章節皆符合術語表規範。")
        else:
            print(f"\n審查完成，共發現 {total_violations} 處術語合規警示。")
    elif opt == "2":
        ja = input("請輸入日文段落：").strip()
        zh = input("請輸入中文譯文：").strip()
        violations, _ = check_glossary_compliance([ja], [zh], glossary)
        if violations:
            print(f"\n發現 {len(violations)} 處未符合術語表：")
            for v in violations:
                print(f"  - 原文「{v.source}」【{v.category}】未正確譯為「{v.expected_target}」")
        else:
            print("\n審查通過，未發現術語違規！")


def retranslate_by_term_scope_flow(cfg: dict[str, Any]) -> None:
    print("\n================ 按術語影響範圍局部重譯 ================")
    try:
        work_dir = load_last_project()
    except Exception:
        work_dir = None
    if not work_dir or not (work_dir / "project.json").exists():
        path_str = input("請輸入專案資料夾路徑（包含 project.json）：").strip().strip('"')
        work_dir = Path(path_str) if path_str else None
    if not work_dir or not (work_dir / "project.json").exists():
        print("找不到有效專案資料夾。")
        return

    project = load_json(work_dir / "project.json", {})
    chapters = project.get("chapters", [])
    if not chapters:
        print("專案中沒有任何章節記錄。")
        return

    effective_entries = get_effective_glossary(cfg, work_dir)
    effective_dict = entries_to_dict(effective_entries)
    print(f"\n專案：{work_dir.name}（共 {len(chapters)} 章）")
    print(f"目前生效術語表共 {len(effective_dict)} 條。")

    terms_input = input("\n請輸入需要重譯影響範圍的日文術語（多個術語請用逗號分隔，輸入 'all' 檢查所有術語）：").strip()
    if not terms_input:
        print("已取消。")
        return

    if terms_input.lower() == "all":
        query_terms = list(effective_dict.keys())
    else:
        query_terms = [t.strip() for t in re.split(r"[,，、\s]+", terms_input) if t.strip()]

    affected = find_affected_chapters(chapters, query_terms)
    if not affected:
        print("\n未在專案中找到包含指定術語的章節段落，無須重譯。")
        return

    total_paras = sum(len(a["affected_paragraphs"]) for a in affected)
    print(f"\n【受影響範圍分析】")
    print(f"  - 涉及章節：{len(affected)} / {len(chapters)} 章")
    print(f"  - 涉及段落：{total_paras} 段")
    for a in affected[:10]:
        print(f"    * 第 {a['index']} 章《{a['title']}》：{len(a['affected_paragraphs'])} 段命中 [{', '.join(a['matched_terms'])}]")
    if len(affected) > 10:
        print(f"    ... 以及其他 {len(affected) - 10} 個章節。")

    ans = input("\n是否確認僅對以上受影響章節進行局部重譯？[Y/n] ").strip().lower()
    if ans == "n":
        print("已取消重譯。")
        return

    ensure_model(cfg)
    if cfg.get("dual_stage"):
        ensure_model(cfg, cfg["review_model"])

    chapter_cfg = dict(cfg)
    chapter_cfg["target_language"] = project.get("language", cfg.get("target_language", "繁體中文"))
    chapter_cfg["glossary"] = effective_dict

    print("\n開始執行局部重譯……")
    for item in affected:
        ch_idx = item["index"] - 1
        ch_record = chapters[ch_idx]
        title = ch_record.get("title", f"第 {item['index']} 章")
        print(f"\n[重譯章節 {item['index']}/{len(chapters)}] {title}")

        ja_paras = ch_record.get("japanese", []) or ch_record.get("source_paragraphs", [])
        if not ja_paras:
            continue

        blocks = ch_record.get("blocks", [])
        ep_blocks = []
        for b in blocks:
            if b.get("type") == "image":
                ep_blocks.append({"type": "image", "url": b.get("key", ""), "alt": b.get("alt", "")})
            else:
                ep_blocks.append({"type": "text", "text": ""})

        episode = Episode(
            url=ch_record.get("url", f"chapter_{item['index']}"),
            work_title="",
            episode_title=title,
            paragraphs=ja_paras,
            blocks=ep_blocks,
        )

        drafts = translate_episode(episode, chapter_cfg, work_dir)
        translations = (
            proofread_episode(episode, drafts, chapter_cfg, work_dir)
            if chapter_cfg.get("dual_stage")
            else drafts
        )

        images = ch_record.get("images", [])
        add_or_update_project(
            episode,
            translations,
            work_dir,
            chapter_cfg,
            images,
            drafts=drafts if chapter_cfg.get("dual_stage") else None,
            build_now=False,
        )

    rebuilt_project = load_json(work_dir / "project.json", {})
    output_epub = build_extended_epub(rebuilt_project, work_dir)
    print(f"\n局部重譯已完成！EPUB 已同步更新：{output_epub}")


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
            atomic_json(CONFIG_PATH, cfg)
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
    print("\n================ TXT / Markdown 批次導入 ================")
    print("支援格式：單一全本 .txt / .md 檔案（自動正則分章），或包含多話章節的資料夾（自然排序）")
    path_str = input("請輸入檔案或資料夾路徑：").strip().strip('"')
    if not path_str:
        print("已取消導入。")
        return
    src_path = Path(path_str)
    if not src_path.exists():
        print(f"找不到指定路徑：{src_path}")
        return

    custom_regex = None
    if src_path.is_file():
        print("\n章節切分規則：")
        print("  1. 預設標準規則（第X話/章/卷、Chapter X、Markdown # 標題、序章/終章/番外編等）")
        print("  2. Markdown 標題切分（# 或 ##）")
        print("  3. 自訂正則表達式")
        r_choice = input("請選擇切分規則 [1]：").strip()
        if r_choice == "2":
            custom_regex = r"^\s*#{1,3}\s+(.+)$"
        elif r_choice == "3":
            custom_regex = input("請輸入切分章節的正則表���式：").strip()

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

    print(f"\n成功解析作品《{work.title}》（作者：{work.author}），共 {len(chapters)} 章。")
    print("章節預覽（前 5 章）：")
    for i, ch in enumerate(chapters[:5], 1):
        char_count = sum(len(p) for p in ch.paragraphs)
        print(f"  {i:3d}. {ch.title} ({len(ch.paragraphs)} 個段落，約 {char_count} 字)")
    if len(chapters) > 5:
        print(f"  ... 其餘 {len(chapters) - 5} 章已略過預覽。")

    print("\n請選擇後續處理方式：")
    print("  1. 打包生成日文原文 EPUB（可用於日後整本翻譯或提取術語）")
    print("  2. 建立翻譯專案並立即開始本機 AI 翻譯")
    print("  3. 僅建立專案資料夾（以便手動配置獨立術語表）")
    action = input("請輸入 1-3 [1]：").strip() or "1"

    if action == "1":
        output_name = f"{safe_name(work.title)}_日文原文.epub"
        output_path = Path(cfg["output_dir"]) / output_name
        import_text_to_epub(
            src_path,
            output_path,
            title=work.title,
            author=work.author,
            description=work.description,
            split_pattern=custom_regex,
        )
        print(f"\n日文原文 EPUB 打包成功：{output_path.resolve()}")
    elif action in {"2", "3"}:
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
        if action == "2":
            print("檢查 Ollama……")
            installed_models(cfg)
            while not select_model(cfg):
                print("請重新選擇一個模型。")
            translate_source_epub(cfg, append_to_existing=False, source_override=temp_epub)


def main() -> int:
    cfg = load_config()
    Path(cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
    print("\n================ Kakuyomu / Syosetu 翻譯工具 ================")
    print("选择工作：")
    print("  1. Kakuyomu / 小說家になろう → 日文原文 EPUB（选择起止章节）")
    print("  2. 日文 EPUB → 独立中文 EPUB")
    print("  3. 日文 EPUB → 追加到已有中文 EPUB")
    print("  4. 导入已有中文 EPUB，再手动打开网页逐章续接")
    print("  5. 继续上次的网页续接项目")
    print("  6. 術語表管理（AI 實體識別/候選詞提取/多格式匯入匯出/合規檢查/局部重譯）")
    print("  7. 批次導入 TXT / Markdown 檔案或資料夾（製作 EPUB 或建立翻譯專案）")
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
