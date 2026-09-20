from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from ebooklib import epub
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page


BLOCK_MARKERS = (
    "不正と思われるアクセスを検知したためブロックしています",
    "短時間のあいだの大量アクセス",
)


class KakuyomuAccessBlocked(RuntimeError):
    pass


def ensure_not_blocked(page: Page) -> None:
    text = page.locator("body").inner_text(timeout=10_000)
    if any(marker in text for marker in BLOCK_MARKERS):
        raise KakuyomuAccessBlocked(
            "Kakuyomu 已暫時封鎖目前連線。程式已停止，請勿立即重試；"
            "關閉程式與 VPN/代理，等待數小時後再用一般家用網路和可正常瀏覽的登入狀態嘗試。"
        )


def open_work_page(page: Page, work_url: str, *, allow_login: bool = False) -> bool:
    """Open a work page and tolerate Kakuyomu's competing login redirect.

    Returns True when the browser ended on the login page.
    """
    if "kakuyomu.jp" in work_url:
        try:
            page.goto(work_url, wait_until="domcontentloaded", timeout=120_000)
        except PlaywrightError as exc:
            location = page.url
            detail = f"{location}\n{exc}"
            if "/auth/login" not in detail:
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except PlaywrightError:
                pass
        on_login = "/auth/login" in page.url
        if on_login and not allow_login:
            raise RuntimeError(
                "尚未完成 Kakuyomu 登入。請在程式開啟的瀏覽器中登入，"
                "確認已回到作品頁後，再返回命令視窗按 Enter。"
            )
        if not on_login:
            ensure_not_blocked(page)
        return on_login
    else:
        page.goto(work_url, wait_until="domcontentloaded", timeout=120_000)
        ensure_not_blocked(page)
        return False


@dataclass
class SourceChapter:
    url: str
    title: str
    paragraphs: list[str]
    blocks: list[dict[str, str]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    chapter_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.chapter_id and self.url:
            self.chapter_id = get_chapter_id(self.url)
        if not self.content_hash and self.paragraphs:
            self.content_hash = compute_content_hash(self.paragraphs)


def get_chapter_id(url: str) -> str:
    """Extracts a unique, stable chapter identifier from Kakuyomu or Syosetu URLs."""
    m_k = re.search(r"/episodes/(\d+)", url)
    if m_k:
        return f"kakuyomu_{m_k.group(1)}"
    m_s = re.search(r"syosetu\.com/([^/]+)/(\d+)", url)
    if m_s:
        return f"syosetu_{m_s.group(1)}_{m_s.group(2)}"
    m_s_single = re.search(r"syosetu\.com/([^/]+)/?$", url)
    if m_s_single:
        return f"syosetu_{m_s_single.group(1)}_1"
    return "ch_" + hashlib.sha256(url.encode()).hexdigest()[:16]


def compute_content_hash(paragraphs: list[str]) -> str:
    """Calculates SHA-256 hash of normalized text paragraphs for change detection."""
    normalized = "\n".join(re.sub(r"\s+", "", p) for p in paragraphs)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class WorkInfo:
    url: str
    title: str
    author: str
    description: str
    cover_url: str
    episodes: list[dict[str, str]]


def _clean(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t\u3000]+", " ", value).strip()


def normalize_work_url(value: str) -> str:
    val = value.strip()
    match_k = re.search(r"https?://kakuyomu\.jp/works/\d+", val)
    if match_k:
        return match_k.group(0)
    match_s = re.search(r"https?://([a-zA-Z0-9.-]*syosetu\.com)/(n\w+)", val)
    if match_s:
        domain = match_s.group(1).lower()
        ncode = match_s.group(2).lower()
        return f"https://{domain}/{ncode}/"
    raise ValueError("請輸入 Kakuyomu 或 小說家になろう (Syosetu) 作品網址，例如 https://kakuyomu.jp/works/123456 或 https://ncode.syosetu.com/n2027ci/。")


def import_cookie_json(context: BrowserContext, path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    cookies = data.get("cookies", []) if isinstance(data, dict) else data
    if not isinstance(cookies, list):
        raise ValueError("Cookie JSON 必須是 Playwright storage_state 或 Cookie 陣列。")
    allowed = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or not cookie.get("name") or "value" not in cookie:
            continue
        domain = str(cookie.get("domain", ""))
        url = str(cookie.get("url", ""))
        if not ("kakuyomu.jp" in domain or "kakuyomu.jp" in url or "syosetu.com" in domain or "syosetu.com" in url):
            continue
        item = {k: v for k, v in cookie.items() if k in {
            "name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"
        }}
        if not item.get("url") and not item.get("domain"):
            item["url"] = "https://kakuyomu.jp/"
        allowed.append(item)
    if allowed:
        context.add_cookies(allowed)
    return len(allowed)


def _extract_kakuyomu_work_info(page: Page, work_url: str) -> WorkInfo:
    if page.url.split("#", 1)[0].rstrip("/") != work_url.rstrip("/"):
        open_work_page(page, work_url)
    # One page load is sufficient: the complete ordered TOC is embedded in
    # __NEXT_DATA__.  Do not expand/scroll every section, which can trigger
    # additional lazy-load requests and look like rapid catalogue crawling.
    page.wait_for_timeout(random.randint(4500, 7000))
    ensure_not_blocked(page)
    data = page.evaluate(
        r"""
        () => {
          const text = (...selectors) => {
            for (const selector of selectors) {
              const node = document.querySelector(selector);
              if (node && node.textContent.trim()) return node.textContent.trim();
            }
            return '';
          };
          const meta = (name, attr='property') => {
            const node = document.querySelector(`meta[${attr}="${name}"]`);
            return node ? (node.content || '') : '';
          };
          const seen = new Set();
          const episodes = [];
          // The visible TOC is virtualised/collapsed and may expose only one
          // section (for this work that misleadingly produced 39/182 items).
          // Next.js embeds the complete, ordered TOC in Apollo's initial state.
          try {
            const node = document.querySelector('#__NEXT_DATA__');
            const next = node ? JSON.parse(node.textContent) : null;
            const state = next?.props?.pageProps?.__APOLLO_STATE__ || {};
            const workId = location.pathname.match(/\/works\/(\d+)/)?.[1];
            const work = workId ? state[`Work:${workId}`] : null;
            for (const chapterRef of (work?.tableOfContentsV2 || [])) {
              const chapter = state[chapterRef.__ref] || {};
              for (const episodeRef of (chapter.episodeUnions || [])) {
                const item = state[episodeRef.__ref] || {};
                const id = item.id || String(episodeRef.__ref || '').split(':').pop();
                if (!/^\d+$/.test(id || '') || seen.has(id)) continue;
                seen.add(id);
                episodes.push({
                  url: `${location.origin}/works/${workId}/episodes/${id}`,
                  title: (item.title || '').trim() || `第${episodes.length + 1}章`
                });
              }
            }
          } catch (_) {
            // Fall through to visible-link extraction for older page formats.
          }
          if (!episodes.length) {
            for (const a of document.querySelectorAll('a[href*="/episodes/"]')) {
              const href = new URL(a.href, location.href).href.split(/[?#]/)[0];
              const ownPrefix = location.origin + location.pathname.replace(/\/$/, '') + '/episodes/';
              if (!href.startsWith(ownPrefix) || !/\/episodes\/\d+$/.test(href) || seen.has(href)) continue;
              seen.add(href);
              const rawTitle = a.textContent.trim().replace(/\s*\d{4}年\d{1,2}月\d{1,2}日公開\s*$/, '');
              episodes.push({url: href, title: rawTitle || `第${episodes.length + 1}章`});
            }
          }
          let author = text('[itemprop="author"]', '[data-testid="author-name"]');
          if (!author) {
            const authorLink = [...document.querySelectorAll('a[href*="/users/"]')]
              .find(a => a.textContent.trim());
            author = authorLink ? authorLink.textContent.trim() : '';
          }
          return {
            title: text('h1', '[data-testid="work-title"]') || meta('og:title'),
            author,
            description: text('[data-testid="work-description"]', '.widget-workIntroduction', '[itemprop="description"]') || meta('description', 'name') || meta('og:description'),
            cover: meta('og:image'),
            episodes
          };
        }
        """
    )
    if not data["episodes"]:
        raise ValueError("作品目錄中沒有找到可訪問章節。請確認登入狀態與作品網址。")
    return WorkInfo(
        url=work_url,
        title=_clean(data["title"]), author=_clean(data["author"]),
        description=_clean(data["description"]), cover_url=data["cover"],
        episodes=data["episodes"],
    )


def _extract_syosetu_work_info(page: Page, work_url: str) -> WorkInfo:
    if page.url.split("#", 1)[0].rstrip("/") != work_url.rstrip("/"):
        open_work_page(page, work_url)
    page.wait_for_timeout(random.randint(1500, 3000))
    ensure_not_blocked(page)
    data = page.evaluate(
        r"""
        () => {
          const text = (...selectors) => {
            for (const selector of selectors) {
              const node = document.querySelector(selector);
              if (node && node.textContent.trim()) return node.textContent.trim();
            }
            return '';
          };
          const meta = (name, attr='property') => {
            const node = document.querySelector(`meta[${attr}="${name}"]`);
            return node ? (node.content || '') : '';
          };

          const title = text('.p-novel__title', '.novel_title', 'h1') || meta('og:title');
          let author = text('.p-novel__author', '.novel_writername') || meta('twitter:creator', 'name');
          if (!author) {
            const authorLink = document.querySelector('a[href*="mypage.syosetu.com"]');
            if (authorLink) author = authorLink.textContent.trim();
          }
          const description = text('.p-novel__summary', '#novel_ex') || meta('description', 'name') || meta('og:description');
          let cover = meta('og:image');
          if (cover && (cover.includes('twitter.png') || cover.includes('default.png'))) {
            cover = '';
          }

          const episodes = [];
          const seen = new Set();
          const links = document.querySelectorAll('.p-eplist__subtitle, .p-eplist__sublist a, .novel_sublist2 .subtitle a, .index_box .subtitle a, .subtitle a');
          for (const a of links) {
            const href = new URL(a.href, location.href).href.split(/[?#]/)[0];
            if (!seen.has(href)) {
              seen.add(href);
              episodes.push({ url: href, title: a.textContent.trim() || `第${seen.size}章` });
            }
          }

          let maxPage = 1;
          const pagerLinks = document.querySelectorAll('.c-pager a, .p-pager a, .pager a, a[href*="?p="]');
          for (const a of pagerLinks) {
            const m = a.href.match(/[?&]p=(\d+)/);
            if (m) {
              const pNum = parseInt(m[1], 10);
              if (pNum > maxPage) maxPage = pNum;
            }
          }

          const isTanpen = !episodes.length && !!document.querySelector('.p-novel__body, #novel_honbun, .p-novel__text');

          return {
            title,
            author,
            description,
            cover,
            episodes,
            maxPage,
            isTanpen
          };
        }
        """
    )
    title = _clean(data.get("title", ""))
    author = _clean(data.get("author", ""))
    author = re.sub(r"^(?:作者|作)\s*[:：]\s*", "", author).strip()
    description = _clean(data.get("description", ""))
    cover_url = data.get("cover", "")
    episodes = data.get("episodes", [])
    max_page = int(data.get("maxPage", 1))
    is_tanpen = bool(data.get("isTanpen", False))

    if is_tanpen:
        episodes = [{"url": work_url, "title": title or "第1章"}]
    elif max_page > 1:
        seen = {ep["url"] for ep in episodes}
        for p_idx in range(2, max_page + 1):
            p_url = f"{work_url.rstrip('/')}/?p={p_idx}"
            page.goto(p_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(random.randint(800, 1500))
            page_episodes = page.evaluate(
                r"""
                () => {
                  const list = [];
                  const links = document.querySelectorAll('.p-eplist__subtitle, .p-eplist__sublist a, .novel_sublist2 .subtitle a, .index_box .subtitle a, .subtitle a');
                  for (const a of links) {
                    const href = new URL(a.href, location.href).href.split(/[?#]/)[0];
                    list.push({ url: href, title: a.textContent.trim() });
                  }
                  return list;
                }
                """
            )
            for ep in page_episodes:
                u = ep.get("url", "")
                if u and u not in seen:
                    seen.add(u)
                    episodes.append({"url": u, "title": ep.get("title", "").strip() or f"第{len(episodes) + 1}章"})

    if not episodes:
        raise ValueError("作品目錄中沒有找到可訪問章節。請確認登入狀態與作品網址。")

    return WorkInfo(
        url=work_url,
        title=title,
        author=author,
        description=description,
        cover_url=cover_url,
        episodes=episodes,
    )


def extract_work_info(page: Page, work_url: str) -> WorkInfo:
    if "syosetu.com" in work_url:
        return _extract_syosetu_work_info(page, work_url)
    return _extract_kakuyomu_work_info(page, work_url)


def fetch_with_backoff(
    page: Page,
    url: str,
    max_retries: int = 4,
    base_delay: float = 3.0,
    backoff_factor: float = 2.0,
) -> None:
    """Navigates to a URL with exponential backoff and jitter for rate-limiting resilience."""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(random.randint(900, 1800))
            ensure_not_blocked(page)
            return
        except KakuyomuAccessBlocked:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                break
            sleep_time = (base_delay * (backoff_factor ** attempt)) + random.uniform(1.0, 3.0)
            print(f"  [連線重試] 請求遇到問題（{exc}），將在 {sleep_time:.1f} 秒後進行第 {attempt + 2}/{max_retries} 次重試……")
            time.sleep(sleep_time)
    if last_exc:
        raise last_exc


def extract_source_chapter(page: Page, url: str, fallback_title: str) -> SourceChapter:
    fetch_with_backoff(page, url)
    data = page.evaluate(
        r"""
        () => {
          const first = selectors => selectors.map(s => document.querySelector(s)).find(Boolean);
          const titleEl = first(['.p-novel__title', '.novel_subtitle', '[data-testid="episode-title"]', '.widget-episodeTitle', 'header h1', 'main h1', 'article h1', 'h1']);
          const body = first(['.p-novel__body', '#novel_honbun', '.p-novel__text', '[data-testid="episode-body"]', '.widget-episodeBody', '.js-episode-body', '[itemprop="articleBody"]', 'article']);
          if (!body) return {title:'', paragraphs:[], blocks:[]};
          const paragraphs = [...body.querySelectorAll('p')].map(p => p.innerText.trim()).filter(Boolean);
          const blocks = [];
          const seen = new Set();
          for (const node of body.querySelectorAll('p, img')) {
            if (node.tagName === 'P') {
              const value = node.innerText.trim();
              if (value) blocks.push({type:'text', text:value});
            } else {
              const src = node.currentSrc || node.src || node.dataset.src;
              if (src && !seen.has(src)) {
                seen.add(src);
                blocks.push({type:'image', url:new URL(src, location.href).href, alt:node.alt || ''});
              }
            }
          }
          return {title:titleEl ? titleEl.innerText.trim() : '', paragraphs, blocks};
        }
        """
    )
    paragraphs = [_clean(p) for p in data["paragraphs"] if _clean(p)]
    if not paragraphs:
        raise ValueError(f"無法讀取章節正文：{url}")
    return SourceChapter(
        url=url,
        title=_clean(data["title"]) or fallback_title,
        paragraphs=paragraphs,
        blocks=data["blocks"],
    )


def download_binary(
    context: BrowserContext,
    url: str,
    max_retries: int = 3,
) -> tuple[bytes, str]:
    last_exc = None
    for attempt in range(max_retries):
        try:
            response = context.request.get(url, timeout=60_000)
            if response.ok:
                media = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
                return response.body(), media
            elif response.status in {429, 503, 502, 504}:
                time.sleep(2.0 * (2 ** attempt) + random.uniform(0.5, 1.5))
                continue
            else:
                raise RuntimeError(f"HTTP {response.status}: {url}")
        except Exception as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"下載資源失敗：{url} ({last_exc})")


def download_chapter_images(context: BrowserContext, chapter: SourceChapter,
                            delay_range: tuple[float, float] = (1.5, 3.0)) -> None:
    images: list[dict[str, Any]] = []
    for block in chapter.blocks:
        if block.get("type") != "image" or not block.get("url"):
            continue
        try:
            time.sleep(random.uniform(*delay_range))
            data, media = download_binary(context, block["url"])
            key = hashlib.sha256(block["url"].encode()).hexdigest()
            ext = mimetypes.guess_extension(media) or Path(urlparse(block["url"]).path).suffix or ".jpg"
            images.append({"key": block["url"], "name": f"{key}{ext}", "media_type": media,
                           "data": data, "alt": block.get("alt", "")})
        except Exception as exc:
            print(f"  插图下载失败：{exc}")
    chapter.images = images


def _chapter_html(chapter: SourceChapter, image_paths: dict[str, str], lang: str = "ja") -> str:
    parts = [f"<h1>{html.escape(chapter.title)}</h1>"]
    for block in chapter.blocks:
        if block.get("type") == "text":
            parts.append(f"<p>{html.escape(block.get('text', ''))}</p>")
        elif block.get("type") == "image" and block.get("url") in image_paths:
            parts.append(f'<p class="illustration"><img src="{html.escape(image_paths[block["url"]])}" alt="{html.escape(block.get("alt", ""))}"/></p>')
    return "<html xmlns=\"http://www.w3.org/1999/xhtml\"><head><title>" + html.escape(chapter.title) + "</title></head><body>" + "\n".join(parts) + "</body></html>"


def build_source_epub(work: WorkInfo, chapters: list[SourceChapter], output: Path,
                      cover: tuple[bytes, str] | None = None, language: str = "ja") -> Path:
    book = epub.EpubBook()
    book.set_identifier(hashlib.sha256(work.url.encode()).hexdigest())
    book.set_title(work.title)
    book.set_language(language)
    if work.author:
        book.add_author(work.author)
    book.add_metadata("DC", "source", work.url)
    if work.description:
        book.add_metadata("DC", "description", work.description)
    css = epub.EpubItem(uid="style", file_name="styles/main.css", media_type="text/css",
                        content="body{font-family:serif;line-height:1.8}p{margin:.8em 0}img{max-width:100%;height:auto;display:block;margin:1em auto}")
    book.add_item(css)
    if cover:
        cover_data, cover_media = cover
        ext = mimetypes.guess_extension(cover_media) or ".jpg"
        book.set_cover(f"images/cover{ext}", cover_data)
    spine: list[Any] = ["nav"]
    toc: list[Any] = []
    if work.description:
        intro = epub.EpubHtml(title="作品简介", file_name="text/introduction.xhtml", lang=language, uid="introduction")
        intro.content = f"<h1>作品简介</h1><p>{html.escape(work.description)}</p>"
        intro.add_item(css); book.add_item(intro); spine.append(intro); toc.append(intro)
    added_images: set[str] = set()
    for index, chapter in enumerate(chapters, 1):
        image_paths: dict[str, str] = {}
        for image in chapter.images:
            path = f"images/{image['name']}"
            image_paths[image["key"]] = "../" + path
            if path not in added_images:
                book.add_item(epub.EpubItem(uid="img_" + hashlib.md5(path.encode()).hexdigest(),
                                            file_name=path, media_type=image["media_type"], content=image["data"]))
                added_images.add(path)
        item = epub.EpubHtml(title=chapter.title, file_name=f"text/chapter_{index:04d}.xhtml",
                             lang=language, uid=f"chapter_{index:04d}")
        item.content = _chapter_html(chapter, image_paths)
        item.add_item(css); book.add_item(item); spine.append(item); toc.append(item)
    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    output.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(output), book, {})
    return output


def extract_epub_chapters(source: Path, assets_dir: Path) -> tuple[dict[str, Any], list[SourceChapter]]:
    book = epub.read_epub(str(source), options={"ignore_ncx": True})
    metadata = {
        "title": (book.get_metadata("DC", "title") or [(source.stem, {})])[0][0],
        "author": (book.get_metadata("DC", "creator") or [("", {})])[0][0],
        "description": (book.get_metadata("DC", "description") or [("", {})])[0][0],
    }
    assets_dir.mkdir(parents=True, exist_ok=True)
    cover_items = list(book.get_items_of_type(10)) + list(book.get_items_of_type(1))
    cover_item = next((item for item in cover_items if "cover" in item.get_name().lower()), None)
    if cover_item is not None:
        cover_path = assets_dir / ("cover" + (Path(cover_item.get_name()).suffix or ".jpg"))
        cover_path.write_bytes(cover_item.get_content())
        metadata["cover_path"] = str(cover_path)
        metadata["cover_media_type"] = cover_item.media_type
    chapters: list[SourceChapter] = []
    image_items = {item.get_name(): item for item in book.get_items_of_type(1)}
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != 9 or idref in {"nav", "introduction", "cover"}:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        title = (soup.find(["h1", "h2"]) or soup.title)
        chapter_title = _clean(title.get_text(" ", strip=True)) if title else item.get_name()
        blocks: list[dict[str, str]] = []
        paragraphs: list[str] = []
        images: list[dict[str, Any]] = []
        for node in soup.select("p, img"):
            if node.name == "p" and node.find("img"):
                continue
            if node.name == "p":
                value = _clean(node.get_text(" ", strip=True))
                if value:
                    paragraphs.append(value); blocks.append({"type": "text", "text": value})
            elif node.name == "img" and node.get("src"):
                candidates = [k for k in image_items if k.endswith(Path(node["src"]).name)]
                if not candidates:
                    continue
                image_item = image_items[candidates[0]]
                key = "epub://" + image_item.get_name()
                local = assets_dir / Path(image_item.get_name()).name
                if not local.exists():
                    local.write_bytes(image_item.get_content())
                blocks.append({"type": "image", "url": key, "alt": node.get("alt", "")})
                images.append({"key": key, "local_path": str(local),
                               "media_type": image_item.media_type, "alt": node.get("alt", "")})
        if paragraphs:
            chapters.append(SourceChapter(url=f"epub://{idref}", title=chapter_title,
                                          paragraphs=paragraphs, blocks=blocks, images=images))
    if not chapters:
        raise ValueError("EPUB 中没有识别到包含正文段落的章节。")
    return metadata, chapters


def translated_source_epub(metadata: dict[str, Any], chapters: list[SourceChapter], output: Path,
                           language: str) -> Path:
    work = WorkInfo(url="local-epub:" + output.stem, title=metadata["title"],
                    author=metadata.get("author", ""), description=metadata.get("description", ""),
                    cover_url="", episodes=[])
    for chapter in chapters:
        for image in chapter.images:
            if "data" not in image and image.get("local_path"):
                image["data"] = Path(image["local_path"]).read_bytes()
                image["name"] = Path(image["local_path"]).name
    cover = None
    if metadata.get("cover_path") and Path(metadata["cover_path"]).exists():
        cover = (Path(metadata["cover_path"]).read_bytes(), metadata.get("cover_media_type", "image/jpeg"))
    return build_source_epub(work, chapters, output, cover=cover,
                             language="zh-Hant" if "繁" in language else "zh-Hans")
