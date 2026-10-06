"""Acquire only the preview range, then acquire the rest after explicit confirmation."""
from dataclasses import asdict
import hashlib
from pathlib import Path
import random
import time

from playwright.sync_api import sync_playwright

from translator.storage.project_storage import atomic_json, load_json
from translator.acquisition.source_epub import (SourceChapter, normalize_work_url, open_work_page, extract_work_info,
                         extract_source_chapter, download_chapter_images, download_binary, build_source_epub)
from translator.translation.translation_workflow import run_workflow, workflow_path, ready_for_full


def network_path(url, cfg):
    identity = hashlib.sha256(normalize_work_url(url).encode()).hexdigest()[:16]
    return Path(cfg["output_dir"]) / "network-workflows" / identity / "network.json"


def _wait(seconds, cfg):
    from translator.engine import check_translation_cancelled
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_translation_cancelled(cfg)
        time.sleep(min(.2, max(0, end - time.monotonic())))


def acquire_source(url, cfg, count, *, full=False, progress=None, work_ready=None, chapter_ready=None, updates_only=False):
    from translator.acquisition.browser_session import launch_context as _launch_context, select_active_page
    from translator.engine import check_translation_cancelled
    url = normalize_work_url(url)
    path = network_path(url, cfg)
    state = load_json(path, {})
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    cache_path = folder / "source-cache.json"
    cache = load_json(cache_path, {})
    # The filename participates in translation project routing; isolate each work.
    source = folder / f"source_{folder.name}.epub"
    state.update(url=url, source=str(source.resolve()), stage="acquiring", preview_count=count,
                 acquisition_complete=state.get("acquisition_complete", False) if updates_only else False)
    atomic_json(path, state)
    with sync_playwright() as playwright:
        automatic_login = cfg.get("_automatic_browser_login", False)
        browser_cfg = {**cfg, "headless": True} if automatic_login else cfg
        context = _launch_context(playwright, browser_cfg, prompt_for_cookies=False)
        try:
            check_translation_cancelled(cfg)
            page = select_active_page(context)
            on_login = open_work_page(page, url, allow_login=True)
            if on_login:
                if automatic_login:
                    context.close()
                    context = None
                    check_translation_cancelled(cfg)
                    context = _launch_context(playwright, {**cfg, "headless": False}, prompt_for_cookies=False)
                    page = select_active_page(context)
                    on_login = open_work_page(page, url, allow_login=True)
                elif cfg.get("headless"):
                    raise ValueError("请先用“登录 Kakuyomu”完成登录，再启动一键试译。")
                if progress:
                    progress(0, "请在打开的浏览器中完成登录，完成后自动继续……")
                while "/auth/" in page.url:
                    check_translation_cancelled(cfg)
                    if page.is_closed():
                        raise ValueError("登录窗口已关闭，尚未完成登录。请重新启动获取。")
                    page.wait_for_timeout(250)
                open_work_page(page, url)
                if automatic_login:
                    # Closing flushes the persistent profile before the same profile
                    # is reopened without a window; login credentials stay local.
                    context.close()
                    context = None
                    check_translation_cancelled(cfg)
                    context = _launch_context(playwright, browser_cfg, prompt_for_cookies=False)
                    page = select_active_page(context)
                    open_work_page(page, url)
                    if progress:
                        progress(0, "登录完成，已隐藏浏览器，继续获取……")
            work = extract_work_info(page, url)
            check_translation_cancelled(cfg)
            requested = len(work.episodes) if full else max(count, int(state.get("acquired_chapters", 0)))
            selected = work.episodes[:requested]
            if not selected:
                raise ValueError("作品没有可获取的章节。")
            previous_urls = state.get("source_urls", [])
            current_urls = [item["url"] for item in work.episodes]
            if previous_urls and current_urls[:len(previous_urls)] != previous_urls:
                raise ValueError("已获取章节的目录顺序发生变化，请核对后使用新的项目。")
            if updates_only and previous_urls == current_urls and source.is_file():
                state.update(work=asdict(work), total_chapters=len(current_urls), stage="acquired",
                             acquired_chapters=len(current_urls), acquisition_complete=True)
                atomic_json(path, state)
                if progress:
                    progress(100, "原站没有新增章节")
                return source, work
            if work_ready:
                work_ready(source, work, cache)
            chapters = []
            for i, item in enumerate(selected, 1):
                check_translation_cancelled(cfg)
                if progress:
                    progress((i - 1) / len(selected) * 100, f"获取 {i}/{len(selected)} · {item['title']}")
                fresh = False
                record = cache.get(item["url"])
                if record and record.get("paragraphs"):
                    images = []
                    for image in record.get("images", []):
                        local = folder / image["local_path"]
                        if not local.is_file():
                            raise ValueError("缓存图片缺失，请恢复该项目的 source-assets 目录。")
                        images.append({**image, "data": local.read_bytes()})
                    chapter = SourceChapter(url=item["url"], title=record["title"],
                                            paragraphs=record["paragraphs"], blocks=record["blocks"], images=images)
                else:
                    fresh = True
                    if updates_only and i <= len(previous_urls):
                        raise ValueError("已获取章节的原文缓存缺失，请恢复 source-cache.json 后再检查新增章节")
                    chapter = extract_source_chapter(page, item["url"], item["title"])
                    check_translation_cancelled(cfg)
                    download_chapter_images(context, chapter)
                    images = []
                    for image in chapter.images:
                        local = folder / "source-assets" / image["name"]
                        local.parent.mkdir(parents=True, exist_ok=True)
                        local.write_bytes(image["data"])
                        images.append({**{k: v for k, v in image.items() if k != "data"},
                                       "local_path": str(local.relative_to(folder))})
                    cache[item["url"]] = {"title": chapter.title, "paragraphs": chapter.paragraphs,
                                          "blocks": chapter.blocks, "images": images}
                    atomic_json(cache_path, cache)
                chapters.append(chapter)
                if chapter_ready:
                    state.update(work=asdict(work), total_chapters=len(work.episodes))
                    if len(chapters) >= state.get("acquired_chapters", 0):
                        state.update(source_urls=[c.url for c in chapters], acquired_chapters=len(chapters))
                    atomic_json(path, state)
                    chapter_ready(i, chapter)
                if fresh:
                    _wait(max(8, float(cfg.get("request_delay_seconds", 8))) + random.uniform(1, 3.5), cfg)
            check_translation_cancelled(cfg)
            cover = None
            if state.get("cover_media") and (folder / "cover.bin").exists():
                cover = ((folder / "cover.bin").read_bytes(), state["cover_media"])
            elif work.cover_url:
                try:
                    cover = download_binary(context, work.cover_url)
                    (folder / "cover.bin").write_bytes(cover[0])
                    state["cover_media"] = cover[1]
                except Exception as exc:
                    if cfg.get("_translation_activity"):
                        cfg["_translation_activity"](f"封面获取失败，继续处理正文：{exc}")
            check_translation_cancelled(cfg)
            temporary = source.with_suffix(".tmp.epub")
            build_source_epub(work, chapters, temporary, cover=cover)
            temporary.replace(source)
            state.update(work=asdict(work), source_urls=[c.url for c in chapters],
                         acquired_chapters=len(chapters), total_chapters=len(work.episodes), stage="acquired",
                         acquisition_complete=len(chapters) == len(work.episodes))
            atomic_json(path, state)
            return source, work
        finally:
            if context is not None:
                context.close()


def run_network_workflow(url, cfg, languages, count=20, *, full=False, progress=None, source_ready=None):
    from translator.engine import check_translation_cancelled
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("试译章节数必须是大于零的整数。")
    path = network_path(url, cfg)
    saved = load_json(path, {})
    if full:
        source = Path(saved.get("source", ""))
        state = load_json(workflow_path(source, cfg), {}) if source.is_file() else {}
        if not ready_for_full(state, source, languages):
            raise ValueError("请先完成该网址及所选语言的获取与试译，再确认翻译全部。")
    try:
        def acquiring(percent, message):
            if progress:
                progress(percent * .3, message)
        source, work = acquire_source(url, cfg, count, full=full, progress=acquiring)
        check_translation_cancelled(cfg)
        if source_ready:
            source_ready(source, work)
        def translating(percent, message):
            if progress:
                progress(30 + percent * .7, message)
        state = run_workflow(source, cfg, languages, count, full=full, progress=translating,
                             allow_source_append=full)
        state["network_url"] = normalize_work_url(url)
        state["network_total_chapters"] = len(work.episodes)
        atomic_json(workflow_path(source, cfg), state)
        net_state = load_json(path, {})
        net_state["stage"] = state["stage"]
        atomic_json(path, net_state)
        return state
    except Exception as exc:
        from translator.engine import TranslationCancelled
        net_state = load_json(path, {})
        net_state.update(stage="stopped" if isinstance(exc, TranslationCancelled) else "failed", error=str(exc))
        atomic_json(path, net_state)
        raise
