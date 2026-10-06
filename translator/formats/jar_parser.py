"""
Robust JAR (Java ME MIDP Ebook) Parser for Python.
Parses .jar ebook files, extracting metadata from MANIFEST.MF, cover/icon images,
and reading/sorting internal chapter text files into SourceChapter structures.
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import zipfile
from pathlib import Path
from typing import Any

from translator.acquisition.source_epub import SourceChapter, WorkInfo, compute_content_hash


class JARParseError(ValueError):
    """Raised when a JAR ebook file cannot be parsed."""
    pass


def parse_manifest_mf(manifest_bytes: bytes) -> dict[str, str]:
    """Parses key-value pairs from JAR META-INF/MANIFEST.MF."""
    meta: dict[str, str] = {}
    lines = manifest_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    
    current_key = ""
    for line in lines:
        if line.startswith(" ") and current_key:
            # Continuation line
            meta[current_key] += line[1:]
        elif ":" in line:
            parts = line.split(":", 1)
            current_key = parts[0].strip()
            meta[current_key] = parts[1].strip()
        else:
            current_key = ""
    return meta


def parse_jar_file(file_path: Path | str) -> tuple[WorkInfo, list[SourceChapter]]:
    """
    Parses a Java ME .jar ebook file.
    Returns (WorkInfo, list[SourceChapter]).
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"找不到檔案：{file_path}")

    try:
        zf = zipfile.ZipFile(p, "r")
    except Exception as exc:
        raise JARParseError(f"無法開啟 JAR 壓縮包：{exc}")

    with zf:
        namelist = zf.namelist()
        
        # 1. Parse MANIFEST.MF
        meta = {}
        manifest_names = [n for n in namelist if n.upper() == "META-INF/MANIFEST.MF"]
        if manifest_names:
            try:
                m_bytes = zf.read(manifest_names[0])
                meta = parse_manifest_mf(m_bytes)
            except Exception:
                pass

        title = meta.get("MIDlet-Name") or meta.get("MIDlet-1", "").split(",")[0].strip() or p.stem
        author = meta.get("MIDlet-Vendor") or meta.get("Author") or "未知作者"
        desc = meta.get("MIDlet-Description") or meta.get("Description") or f"從 JAR 電子書導入之作品：{title}"

        # 2. Extract potential cover or icon images
        cover_data: bytes | None = None
        cover_ext = "png"
        img_candidates = [
            n for n in namelist if any(n.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp"))
            and not n.lower().startswith("__macosx")
        ]
        
        # Priority for cover: 'cover', 'page', 'icon', first image
        cover_candidates = sorted(img_candidates, key=lambda x: (
            0 if "cover" in x.lower() else (1 if "icon" in x.lower() else 2)
        ))
        if cover_candidates:
            try:
                c_bytes = zf.read(cover_candidates[0])
                if len(c_bytes) > 200:  # Avoid 16x16 tiny placeholder icons if bigger image exists
                    cover_data = c_bytes
                    cover_ext = cover_candidates[0].split(".")[-1].lower()
            except Exception:
                pass

        cover_image_dict = None
        if cover_data:
            c_key = f"cover_{hashlib.md5(cover_data).hexdigest()[:8]}.{cover_ext}"
            cover_image_dict = {
                "key": c_key,
                "local_path": "",
                "media_type": f"image/{'jpeg' if cover_ext == 'jpg' else cover_ext}",
                "alt": "Cover Image",
                "data": cover_data,
            }

        # 3. Find all text content files
        # Candidates: *.txt, *.text, *.dat, *.bin, or text-like files inside root/text/res/data
        text_files: list[str] = []
        for n in namelist:
            if n.endswith("/") or n.upper().startswith("META-INF/") or n.startswith("__MACOSX"):
                continue
            lower_n = n.lower()
            if any(lower_n.endswith(ext) for ext in (".txt", ".text", ".md", ".html", ".htm")):
                text_files.append(n)
            elif any(lower_n.startswith(prefix) for prefix in ("text/", "book/", "novel/", "data/", "res/")) and not any(lower_n.endswith(ext) for ext in (".png", ".jpg", ".gif", ".class", ".mf")):
                # Likely raw text chunk
                text_files.append(n)

        # Import sorting function from text_importer
        from translator.formats.text_importer import (
            DEFAULT_SPLIT_REGEX,
            clean_markdown_line,
            detect_bytes_encoding,
            detect_file_encoding,
            natural_sort_key,
            parse_single_file_chapters,
            split_text_into_paragraphs,
        )

        text_files.sort(key=lambda x: natural_sort_key(x))

        chapters: list[SourceChapter] = []

        # Helper to decode raw bytes
        def _decode_bytes(b: bytes) -> str:
            if not b:
                return ""
            enc = detect_bytes_encoding(b)
            for e in (enc, "utf-8", "gb18030", "big5", "cp932", "latin-1"):
                try:
                    return b.decode(e)
                except Exception:
                    continue
            return b.decode("utf-8", errors="replace")

        if len(text_files) > 1:
            # Multi-file chapters
            for idx, tf in enumerate(text_files, 1):
                try:
                    raw_b = zf.read(tf)
                except Exception:
                    continue
                content = _decode_bytes(raw_b)
                paras = split_text_into_paragraphs(content)
                if not paras:
                    continue

                # Title determination
                first_line = clean_markdown_line(paras[0])
                if len(first_line) < 60 and not first_line.endswith(("。", "！", "？", "…", "」", "』")):
                    ch_title = first_line
                    body_paras = paras[1:] if len(paras) > 1 else paras
                else:
                    ch_title = Path(tf).stem
                    body_paras = paras

                if not body_paras:
                    body_paras = [ch_title]

                ch_url = f"jar://{p.name}!/{tf}"
                ch_imgs = [cover_image_dict] if (idx == 1 and cover_image_dict) else []
                blocks = [{"type": "text", "text": pr} for pr in body_paras]
                if ch_imgs:
                    blocks.insert(0, {"type": "image", "url": ch_imgs[0]["key"], "alt": ch_imgs[0]["alt"]})

                chapters.append(SourceChapter(
                    url=ch_url,
                    title=ch_title,
                    paragraphs=body_paras,
                    blocks=blocks,
                    images=ch_imgs,
                    chapter_id=f"jar_{hashlib.md5(ch_url.encode()).hexdigest()[:12]}",
                    content_hash=compute_content_hash(body_paras),
                ))

        elif len(text_files) == 1:
            # Single text file -> split using regex
            raw_b = zf.read(text_files[0])
            content = _decode_bytes(raw_b)
            
            lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            split_pat = re.compile(DEFAULT_SPLIT_REGEX, re.MULTILINE)

            cur_title = ""
            cur_lines: list[str] = []
            ch_idx = 1

            for line in lines:
                stripped = line.strip()
                if split_pat.match(stripped):
                    if cur_lines or cur_title:
                        paras = [l.strip() for l in cur_lines if l.strip()]
                        if paras:
                            c_title = cur_title or f"第 {ch_idx} 章"
                            c_url = f"jar://{p.name}#{ch_idx}"
                            c_imgs = [cover_image_dict] if (ch_idx == 1 and cover_image_dict) else []
                            blks = [{"type": "text", "text": pr} for pr in paras]
                            if c_imgs:
                                blks.insert(0, {"type": "image", "url": c_imgs[0]["key"], "alt": c_imgs[0]["alt"]})
                            chapters.append(SourceChapter(
                                url=c_url,
                                title=c_title,
                                paragraphs=paras,
                                blocks=blks,
                                images=c_imgs,
                                chapter_id=f"jar_{hashlib.md5(c_url.encode()).hexdigest()[:12]}",
                                content_hash=compute_content_hash(paras),
                            ))
                            ch_idx += 1
                    cur_title = clean_markdown_line(stripped)
                    cur_lines = []
                else:
                    cur_lines.append(line)

            if cur_lines or cur_title:
                paras = [l.strip() for l in cur_lines if l.strip()]
                if paras:
                    c_title = cur_title or (f"第 {ch_idx} 章" if ch_idx > 1 else title)
                    c_url = f"jar://{p.name}#{ch_idx}"
                    c_imgs = [cover_image_dict] if (ch_idx == 1 and cover_image_dict) else []
                    blks = [{"type": "text", "text": pr} for pr in paras]
                    if c_imgs:
                        blks.insert(0, {"type": "image", "url": c_imgs[0]["key"], "alt": c_imgs[0]["alt"]})
                    chapters.append(SourceChapter(
                        url=c_url,
                        title=c_title,
                        paragraphs=paras,
                        blocks=blks,
                        images=c_imgs,
                        chapter_id=f"jar_{hashlib.md5(c_url.encode()).hexdigest()[:12]}",
                        content_hash=compute_content_hash(paras),
                    ))

        if not chapters:
            raise JARParseError(f"未能從 JAR 電子書「{p.name}��中找到任何有效的文字章節檔案。")

        episodes = [{"title": ch.title, "url": ch.url} for ch in chapters]
        work = WorkInfo(
            url=f"local-jar:{p.name}",
            title=title,
            author=author,
            description=desc,
            cover_url="",
            episodes=episodes,
        )
        return work, chapters
