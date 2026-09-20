"""
TXT and Markdown Batch Importer for Kakuyomu / Syosetu Local Translator.
Supports importing single full-text files or directories of chapter files (.txt / .md / .markdown),
intelligent chapter splitting via regex, natural alphanumeric sorting, and converting to EPUB / translation projects.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from source_epub import SourceChapter, WorkInfo, build_source_epub, compute_content_hash, get_chapter_id


COMMON_CHAPTER_PATTERNS = [
    # Kakuyomu / Syosetu / Web novel standard Japanese patterns
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百千萬亿]+\s*(?:話|章|卷|部|篇|回|節|幕)|[0-9０-９]{1,4}\s*[\.、：\s]+[^\n]+|Chapter\s*\d+|Episode\s*\d+|プロローグ|エピローグ|序章|終章|転章|間話|幕間|番外編|後記|あとがき|前書き)(?:\s+[^\n]*)?$",
    # Markdown headings (# or ##)
    r"^\s*#{1,3}\s+(.+)$",
    # Chinese novel standard patterns
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百千萬亿]+\s*[章回节卷部篇幕话])(?:\s+[^\n]*)?$",
]

DEFAULT_SPLIT_REGEX = (
    r"^\s*(?:"
    r"第\s*[0-9０-９一二三四五六七八九十百千萬亿]+\s*(?:話|章|卷|部|篇|回|節|幕)|"
    r"Chapter\s*\d+|"
    r"Episode\s*\d+|"
    r"#{1,3}\s+.+|"
    r"プロローグ|エピローグ|序章|終章|転章|間話|幕間|番外編|後記|あとがき|前書き|"
    r"[0-9０-９]{1,4}\s*[\.、：\s]+[^\n]+"
    r")(?:\s+[^\n]*)?$"
)


def natural_sort_key(s: str) -> list[Any]:
    """Sort strings containing numbers in natural human order (e.g., 1, 2, 10 instead of 1, 10, 2)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", str(s))]


def detect_file_encoding(file_path: Path | str) -> str:
    """Detects encoding by testing common encodings with fallback."""
    raw = Path(file_path).read_bytes()[:16384]
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    
    # Try encodings in order of likelihood
    for enc in ("utf-8", "cp932", "shift_jis", "gb18030", "gbk", "big5", "euc-jp", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def read_text_file(file_path: Path | str, encoding: str | None = None) -> str:
    """Reads a text file with automatic encoding fallback."""
    p = Path(file_path)
    enc = encoding or detect_file_encoding(p)
    try:
        return p.read_text(encoding=enc, errors="replace")
    except Exception:
        return p.read_text(encoding="utf-8", errors="replace")


def clean_markdown_line(line: str) -> str:
    """Strips leading markdown heading symbols while preserving line content."""
    line = re.sub(r"^#{1,6}\s*", "", line)
    return line.strip()


def parse_markdown_images(text: str, base_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    """Extracts markdown image references ![alt](path) and returns clean text plus image items."""
    images: list[dict[str, Any]] = []
    
    def _repl(m: re.Match) -> str:
        alt = m.group(1) or ""
        img_path_str = m.group(2).strip()
        img_p = Path(img_path_str)
        if not img_p.is_absolute():
            img_p = (base_dir / img_p).resolve()
        
        if img_p.exists() and img_p.is_file():
            key = f"img_{hashlib.md5(img_p.name.encode()).hexdigest()[:8]}_{img_p.name}"
            media_type = mimetypes.guess_type(img_p.name)[0] or "image/jpeg"
            images.append({
                "key": key,
                "local_path": str(img_p),
                "media_type": media_type,
                "alt": alt,
                "data": img_p.read_bytes(),
            })
            return f"\n[插圖: {alt or img_p.name}]\n"
        return ""

    cleaned_text = re.sub(r"!\[(.*?)\]\((.*?)\)", _repl, text)
    return cleaned_text, images


def split_text_into_paragraphs(text: str) -> list[str]:
    """Splits raw text content into cleaned, non-empty paragraph strings."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paragraphs: list[str] = []
    for line in lines:
        cleaned = line.strip()
        if cleaned:
            paragraphs.append(cleaned)
    return paragraphs


def parse_single_file_chapters(
    file_path: Path | str,
    pattern: str | None = None,
    encoding: str | None = None,
) -> list[SourceChapter]:
    """Splits a single large TXT/Markdown file into multiple chapters using regex."""
    p = Path(file_path)
    content = read_text_file(p, encoding)
    regex_str = pattern.strip() if pattern and pattern.strip() else DEFAULT_SPLIT_REGEX
    split_pattern = re.compile(regex_str, re.MULTILINE)

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    
    chapters: list[SourceChapter] = []
    current_title: str = ""
    current_lines: list[str] = []
    chapter_index = 1

    for line in lines:
        stripped = line.strip()
        if split_pattern.match(stripped):
            # Save previous chapter if exists
            if current_lines or current_title:
                raw_text = "\n".join(current_lines)
                clean_txt, imgs = parse_markdown_images(raw_text, p.parent)
                paras = split_text_into_paragraphs(clean_txt)
                if paras:
                    ch_title = current_title or f"第 {chapter_index} 章"
                    ch_url = f"file://{p.name}#{chapter_index}"
                    blocks = [{"type": "text", "text": pr} for pr in paras]
                    for img in imgs:
                        blocks.append({"type": "image", "url": img["key"], "alt": img["alt"]})
                    chapters.append(SourceChapter(
                        url=ch_url,
                        title=ch_title,
                        paragraphs=paras,
                        blocks=blocks,
                        images=imgs,
                        chapter_id=f"txt_{hashlib.md5(ch_url.encode()).hexdigest()[:12]}",
                        content_hash=compute_content_hash(paras),
                    ))
                    chapter_index += 1
            current_title = clean_markdown_line(stripped)
            current_lines = []
        else:
            current_lines.append(line)

    # Add final chapter
    if current_lines or current_title:
        raw_text = "\n".join(current_lines)
        clean_txt, imgs = parse_markdown_images(raw_text, p.parent)
        paras = split_text_into_paragraphs(clean_txt)
        if paras:
            ch_title = current_title or (f"第 {chapter_index} 章" if chapter_index > 1 else p.stem)
            ch_url = f"file://{p.name}#{chapter_index}"
            blocks = [{"type": "text", "text": pr} for pr in paras]
            for img in imgs:
                blocks.append({"type": "image", "url": img["key"], "alt": img["alt"]})
            chapters.append(SourceChapter(
                url=ch_url,
                title=ch_title,
                paragraphs=paras,
                blocks=blocks,
                images=imgs,
                chapter_id=f"txt_{hashlib.md5(ch_url.encode()).hexdigest()[:12]}",
                content_hash=compute_content_hash(paras),
            ))

    return chapters


def parse_directory_chapters(
    dir_path: Path | str,
    encoding: str | None = None,
    recursive: bool = False,
) -> list[SourceChapter]:
    """Reads a directory of .txt/.md files in natural numerical order as individual chapters."""
    folder = Path(dir_path)
    if not folder.is_dir():
        raise NotADirectoryError(f"指定的路徑不是資料夾：{dir_path}")

    exts = {".txt", ".md", ".markdown"}
    files = [f for f in (folder.rglob("*") if recursive else folder.iterdir()) if f.is_file() and f.suffix.lower() in exts]
    files.sort(key=lambda x: natural_sort_key(x.name))

    chapters: list[SourceChapter] = []
    for idx, f in enumerate(files, 1):
        content = read_text_file(f, encoding)
        clean_txt, imgs = parse_markdown_images(content, f.parent)
        paras = split_text_into_paragraphs(clean_txt)
        if not paras:
            continue

        # Check if first line looks like a title
        first_line = clean_markdown_line(paras[0])
        if len(first_line) < 60 and not first_line.endswith(("。", "！", "？", "…", "」", "』")):
            title = first_line
            # Remove title from paragraph if it matches first line
            body_paras = paras[1:] if len(paras) > 1 else paras
        else:
            title = f.stem
            body_paras = paras

        if not body_paras:
            body_paras = [title]

        ch_url = f"file://{f.name}"
        blocks = [{"type": "text", "text": pr} for pr in body_paras]
        for img in imgs:
            blocks.append({"type": "image", "url": img["key"], "alt": img["alt"]})

        chapters.append(SourceChapter(
            url=ch_url,
            title=title,
            paragraphs=body_paras,
            blocks=blocks,
            images=imgs,
            chapter_id=f"txt_{hashlib.md5(f.name.encode()).hexdigest()[:12]}",
            content_hash=compute_content_hash(body_paras),
        ))

    return chapters


def import_text_source(
    path: Path | str,
    title: str = "",
    author: str = "",
    description: str = "",
    split_pattern: str | None = None,
    encoding: str | None = None,
    recursive: bool = False,
) -> tuple[WorkInfo, list[SourceChapter]]:
    """Loads a TXT/Markdown file or folder and returns WorkInfo + list of SourceChapter."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"找不到路徑：{path}")

    if src.is_dir():
        chapters = parse_directory_chapters(src, encoding=encoding, recursive=recursive)
        work_title = title.strip() or src.name
    else:
        chapters = parse_single_file_chapters(src, pattern=split_pattern, encoding=encoding)
        work_title = title.strip() or src.stem

    if not chapters:
        raise ValueError(f"未能從「{src.name}」解析出任何有效章節內文。請檢查檔案格式或正則設定。")

    episodes = [{"title": ch.title, "url": ch.url} for ch in chapters]
    work = WorkInfo(
        url=f"local-txt:{src.name}",
        title=work_title,
        author=author.strip() or "未知作者",
        description=description.strip() or f"從外部文字檔案導入之作品（共 {len(chapters)} 章）",
        cover_url="",
        episodes=episodes,
    )
    return work, chapters


def import_text_to_epub(
    source_path: Path | str,
    output_epub_path: Path | str,
    title: str = "",
    author: str = "",
    description: str = "",
    language: str = "日文",
    split_pattern: str | None = None,
    cover_path: Path | str | None = None,
) -> Path:
    """Directly converts a TXT/Markdown source into a standard EPUB file."""
    work, chapters = import_text_source(
        source_path,
        title=title,
        author=author,
        description=description,
        split_pattern=split_pattern,
    )
    cover_data = None
    if cover_path and Path(cover_path).exists():
        c_bytes = Path(cover_path).read_bytes()
        c_mime = mimetypes.guess_type(str(cover_path))[0] or "image/jpeg"
        cover_data = (c_bytes, c_mime)

    out_p = Path(output_epub_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    epub_lang = "ja" if "日" in language else ("zh-Hant" if "繁" in language else "zh-Hans")
    return build_source_epub(work, chapters, out_p, cover=cover_data, language=epub_lang)


def create_project_from_text(
    source_path: Path | str,
    work_dir: Path | str,
    title: str = "",
    author: str = "",
    description: str = "",
    language: str = "繁體中文",
    split_pattern: str | None = None,
) -> Path:
    """Converts a TXT/Markdown source into an editable translation project directory."""
    work, chapters = import_text_source(
        source_path,
        title=title,
        author=author,
        description=description,
        split_pattern=split_pattern,
    )
    p_dir = Path(work_dir)
    p_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = p_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    project_chapters = []
    for ch in chapters:
        # Copy chapter images if any
        ch_images = []
        for img in ch.images:
            local_name = Path(img.get("local_path", img["key"])).name
            target_asset = assets_dir / local_name
            if not target_asset.exists() and img.get("data"):
                target_asset.write_bytes(img["data"])
            ch_images.append({
                "key": img["key"],
                "local_path": str(target_asset.relative_to(p_dir)),
                "media_type": img.get("media_type", "image/jpeg"),
                "alt": img.get("alt", ""),
            })

        project_chapters.append({
            "title": ch.title,
            "url": ch.url,
            "chapter_id": ch.chapter_id,
            "content_hash": ch.content_hash,
            "japanese": ch.paragraphs,
            "paragraphs": [],
            "blocks": ch.blocks,
            "images": ch_images,
        })

    import json
    project_data = {
        "work_title": work.title,
        "author": work.author,
        "description": work.description,
        "language": language,
        "base_epub": None,
        "imported_from": str(Path(source_path).resolve()),
        "created_at": None,
        "chapters": project_chapters,
    }

    proj_file = p_dir / "project.json"
    proj_file.write_text(json.dumps(project_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return proj_file
