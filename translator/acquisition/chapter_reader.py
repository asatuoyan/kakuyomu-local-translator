"""Read chapter text and ordered blocks from an open browser page."""
from __future__ import annotations

from urllib.parse import urlparse
from playwright.sync_api import Page

from translator.domain import Episode, normalize_text


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
