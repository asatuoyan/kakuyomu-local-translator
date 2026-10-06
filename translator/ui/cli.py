"""Interactive command-line workflows; translation is delegated to the engine."""
from __future__ import annotations

import hashlib
import re
import sys
import subprocess
import time
from pathlib import Path
from typing import Any

from translator.domain import Episode, safe_name
from translator.acquisition.chapter_reader import extract_episode
from translator.storage.translation_book import TranslationBook
from translator.config import APP_DIR, CONFIG_PATH, TRANSLATION_MODELS, update_config
from translator.storage.project_storage import atomic_json, load_json
from translator.languages import LANGUAGES, language_name
import random
from playwright.sync_api import sync_playwright

from translator.formats.epub_append import build_extended_epub, create_project_from_epub, inspect_epub
from translator.glossary.manager import (
    VALID_CATEGORIES, GlossaryEntry, check_glossary_compliance, dict_to_entries,
    entries_to_dict, export_glossary_to_file, extract_candidate_terms,
    find_affected_chapters, get_effective_glossary, import_glossary_from_file,
    load_project_glossary, merge_glossaries, save_project_glossary, scan_novel_entities,
)
from translator.acquisition.source_epub import (
    SourceChapter, build_source_epub, compute_content_hash, download_binary,
    download_chapter_images, extract_epub_chapters, extract_source_chapter,
    extract_work_info, get_chapter_id, import_cookie_json, normalize_work_url,
    open_work_page,
)
from translator.formats.text_importer import (
    create_project_from_text, detect_text_language, get_language_display_name,
    import_text_source, import_text_to_epub,
)


from translator.engine import (
    MissingProjectSource,
    _episode_from_source,
    _translated_chapter,
    add_or_update_project,
    audit_project,
    download_episode_images,
    installed_models,
    load_config,
    load_last_project,
    load_review_project,
    model_is_installed,
    project_exists,
    project_glossary,
    pull_model,
    retranslate_project,
    save_last_project,
    translate_episode,
    translate_epub_language,
)
from translator.ui.file_dialogs import (
    DialogCancelled, select_file, select_epub_file, select_directory,
    select_save_file, select_import_source,
)
from translator.acquisition.browser_session import launch_context, select_active_page


def _launch_context(playwright, cfg):
    context = launch_context(playwright, cfg)
    cookie_path = APP_DIR / "cookies.json"
    if cookie_path.exists():
        answer = input("发现 cookies.json，是否导入其中的 Cookie？[y/N] ").strip().lower()
        if answer == "y":
            print(f"已导入 {import_cookie_json(context, cookie_path)} 条 Cookie。")
    return context


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
        cfg["browser_executable"] = str(select_file("选择浏览器程序", [("浏览器程序", "*.exe")]))


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
            work_dir = select_directory("选择作品项目文件夹")
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
        export_path = select_save_file(initial_path=Path(cfg["output_dir"]) / "glossary.xlsx")
        export_glossary_to_file(entries, export_path)
        print(f"術語表已匯出至：{export_path}")


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
    target = select_save_file(initial_path=out_path, filetypes=[("术语表", "*" + ext)])
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
        work_dir = select_directory("选择作品项目文件夹")
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
            work_dir = select_directory("选择作品项目文件夹")
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
    src_path = select_import_source()
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


def _main() -> int:
    from translator.version import VERSION
    cfg = load_config()
    Path(cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
    print(f"\n================ Kakuyomu / Syosetu 翻譯工具 v{VERSION} ================")
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


def main() -> int:
    try:
        return _main()
    except DialogCancelled:
        print("已取消当前操作。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
