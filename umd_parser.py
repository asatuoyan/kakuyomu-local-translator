"""
Robust UMD (Universal Mobile Document) Binary Parser for Python.
Parses .umd ebook files, extracting metadata, cover image, chapter titles and text content.
"""
from __future__ import annotations

import hashlib
import io
import struct
import zlib
from pathlib import Path
from typing import Any

from source_epub import SourceChapter, WorkInfo, compute_content_hash


UMD_MAGIC = b"\xde\x9a\x9b\x89"


class UMDParseError(ValueError):
    """Raised when a UMD file cannot be parsed."""
    pass


def parse_umd_file(file_path: Path | str) -> tuple[WorkInfo, list[SourceChapter]]:
    """
    Parses a binary UMD ebook file.
    Returns (WorkInfo, list[SourceChapter]).
    """
    p = Path(file_path)
    data = p.read_bytes()

    if len(data) < 8 or not data.startswith(UMD_MAGIC):
        raise UMDParseError(f"不是合法的 UMD 檔案（標頭魔數不符）：{p.name}")

    stream = io.BytesIO(data)
    stream.seek(4)  # Skip magic

    title = p.stem
    author = "未知作者"
    year = ""
    month = ""
    day = ""
    gender = ""
    publisher = ""
    cover_data: bytes | None = None
    cover_ext = "jpg"
    
    chapter_titles: list[str] = []
    chapter_offsets: list[int] = []
    content_chunks: list[bytes] = []

    file_size = len(data)

    while stream.tell() < file_size:
        b = stream.read(1)
        if not b:
            break
        if b != b"#":
            continue

        header_bytes = stream.read(4)
        if len(header_bytes) < 4:
            break

        cmd, flag, length = struct.unpack("<HBB", header_bytes)

        if cmd == 0x0001:  # File type
            _ = stream.read(length - 5) if length >= 5 else None
        elif cmd == 0x0002:  # Title
            raw = stream.read(length - 5) if length >= 5 else b""
            try:
                t = raw.decode("utf-16le", errors="ignore").strip()
                if t:
                    title = t
            except Exception:
                pass
        elif cmd == 0x0003:  # Author
            raw = stream.read(length - 5) if length >= 5 else b""
            try:
                a = raw.decode("utf-16le", errors="ignore").strip()
                if a:
                    author = a
            except Exception:
                pass
        elif cmd == 0x0004:  # Year
            raw = stream.read(length - 5) if length >= 5 else b""
            year = raw.decode("utf-16le", errors="ignore").strip()
        elif cmd == 0x0005:  # Month
            raw = stream.read(length - 5) if length >= 5 else b""
            month = raw.decode("utf-16le", errors="ignore").strip()
        elif cmd == 0x0006:  # Day
            raw = stream.read(length - 5) if length >= 5 else b""
            day = raw.decode("utf-16le", errors="ignore").strip()
        elif cmd == 0x0007:  # Gender / Category
            raw = stream.read(length - 5) if length >= 5 else b""
            gender = raw.decode("utf-16le", errors="ignore").strip()
        elif cmd == 0x0008:  # Publisher
            raw = stream.read(length - 5) if length >= 5 else b""
            publisher = raw.decode("utf-16le", errors="ignore").strip()
        elif cmd == 0x0009:  # Vendor
            _ = stream.read(length - 5) if length >= 5 else None
        elif cmd == 0x000A:  # Content ID
            _ = stream.read(length - 5) if length >= 5 else None
        elif cmd == 0x000B:  # Content length
            _ = stream.read(length - 5) if length >= 5 else None
        elif cmd == 0x0081:  # Cover image
            try:
                # 0x81 block: 1 byte random, 4 bytes check/len, then image
                stream.seek(1, io.SEEK_CUR)  # skip 1 byte
                chunk_len_bytes = stream.read(4)
                if len(chunk_len_bytes) == 4:
                    chunk_len = struct.unpack("<I", chunk_len_bytes)[0]
                    # read chunk_len - 9 or remaining
                    if chunk_len > 9:
                        img_bytes = stream.read(chunk_len - 9)
                        if img_bytes.startswith(b"\xff\xd8\xff"):
                            cover_data = img_bytes
                            cover_ext = "jpg"
                        elif img_bytes.startswith(b"\x89PNG"):
                            cover_data = img_bytes
                            cover_ext = "png"
                        elif img_bytes.startswith(b"GIF8"):
                            cover_data = img_bytes
                            cover_ext = "gif"
                        elif img_bytes.startswith(b"BM"):
                            cover_data = img_bytes
                            cover_ext = "bmp"
            except Exception:
                pass
        elif cmd == 0x0082:  # Chapter title list
            try:
                stream.seek(1, io.SEEK_CUR)  # skip 1 byte
                chunk_len_bytes = stream.read(4)
                if len(chunk_len_bytes) == 4:
                    chunk_len = struct.unpack("<I", chunk_len_bytes)[0]
                    ch_data = stream.read(chunk_len - 9) if chunk_len > 9 else b""
                    ch_stream = io.BytesIO(ch_data)
                    while ch_stream.tell() < len(ch_data):
                        t_len_byte = ch_stream.read(1)
                        if not t_len_byte:
                            break
                        t_len = struct.unpack("B", t_len_byte)[0]
                        if t_len == 0:
                            continue
                        t_bytes = ch_stream.read(t_len)
                        ch_t = t_bytes.decode("utf-16le", errors="ignore").strip()
                        if ch_t:
                            chapter_titles.append(ch_t)
            except Exception:
                pass
        elif cmd == 0x0083:  # Chapter offset table
            try:
                stream.seek(1, io.SEEK_CUR)
                chunk_len_bytes = stream.read(4)
                if len(chunk_len_bytes) == 4:
                    chunk_len = struct.unpack("<I", chunk_len_bytes)[0]
                    off_data = stream.read(chunk_len - 9) if chunk_len > 9 else b""
                    num_offsets = len(off_data) // 4
                    for i in range(num_offsets):
                        off = struct.unpack("<I", off_data[i * 4:(i + 1) * 4])[0]
                        chapter_offsets.append(off)
            except Exception:
                pass
        elif cmd == 0x0084:  # Content chunks
            try:
                stream.seek(1, io.SEEK_CUR)
                chunk_len_bytes = stream.read(4)
                if len(chunk_len_bytes) == 4:
                    chunk_len = struct.unpack("<I", chunk_len_bytes)[0]
                    c_data = stream.read(chunk_len - 9) if chunk_len > 9 else b""
                    try:
                        decompressed = zlib.decompress(c_data)
                        content_chunks.append(decompressed)
                    except Exception:
                        content_chunks.append(c_data)
            except Exception:
                pass
        else:
            # Unknown block, skip
            if length >= 5:
                stream.seek(length - 5, io.SEEK_CUR)

    # Reconstruct text from content_chunks
    full_content_bytes = b"".join(content_chunks)
    full_text = ""
    from text_importer import detect_bytes_encoding
    detected_enc = detect_bytes_encoding(full_content_bytes) if full_content_bytes else "utf-16le"
    for enc in (detected_enc, "utf-16le", "utf-8", "gb18030", "big5"):
        try:
            full_text = full_content_bytes.decode(enc)
            break
        except Exception:
            continue

    if not full_text:
        full_text = full_content_bytes.decode("utf-16le", errors="replace")

    # Clean text
    full_text = full_text.replace("\r\n", "\n").replace("\r", "\n")

    # Cover image dict if extracted
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

    # Build chapters
    chapters: list[SourceChapter] = []

    # Case 1: Chapter offsets and titles exist and align
    if chapter_offsets and len(chapter_offsets) >= len(chapter_titles) and len(chapter_titles) > 1:
        # Offsets in UMD are character or byte counts in UTF-16
        # Convert offsets if needed
        for idx, title_text in enumerate(chapter_titles):
            start_off = chapter_offsets[idx]
            end_off = chapter_offsets[idx + 1] if idx + 1 < len(chapter_offsets) else len(full_text)
            
            # Check if offsets are character indices or byte indices
            if start_off < len(full_text):
                ch_text = full_text[start_off:min(end_off, len(full_text))]
            else:
                # byte offset
                s_char = start_off // 2
                e_char = end_off // 2
                ch_text = full_text[s_char:min(e_char, len(full_text))]
            
            lines = [l.strip() for l in ch_text.split("\n") if l.strip()]
            if not lines:
                continue
            
            ch_url = f"umd://{p.name}#{idx + 1}"
            ch_imgs = [cover_image_dict] if (idx == 0 and cover_image_dict) else []
            blocks = [{"type": "text", "text": pr} for pr in lines]
            if ch_imgs:
                blocks.insert(0, {"type": "image", "url": ch_imgs[0]["key"], "alt": ch_imgs[0]["alt"]})

            chapters.append(SourceChapter(
                url=ch_url,
                title=title_text or f"第 {idx + 1} 章",
                paragraphs=lines,
                blocks=blocks,
                images=ch_imgs,
                chapter_id=f"umd_{hashlib.md5(ch_url.encode()).hexdigest()[:12]}",
                content_hash=compute_content_hash(lines),
            ))

    # Case 2: No valid offset table or fallback to intelligent chapter splitting
    if not chapters:
        from text_importer import DEFAULT_SPLIT_REGEX, split_text_into_paragraphs
        import re
        lines = full_text.split("\n")
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
                        c_url = f"umd://{p.name}#{ch_idx}"
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
                            chapter_id=f"umd_{hashlib.md5(c_url.encode()).hexdigest()[:12]}",
                            content_hash=compute_content_hash(paras),
                        ))
                        ch_idx += 1
                cur_title = stripped
                cur_lines = []
            else:
                cur_lines.append(line)
                
        if cur_lines or cur_title:
            paras = [l.strip() for l in cur_lines if l.strip()]
            if paras:
                c_title = cur_title or (f"第 {ch_idx} 章" if ch_idx > 1 else title)
                c_url = f"umd://{p.name}#{ch_idx}"
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
                    chapter_id=f"umd_{hashlib.md5(c_url.encode()).hexdigest()[:12]}",
                    content_hash=compute_content_hash(paras),
                ))

    # Case 3: Ultimate fallback - single chapter
    if not chapters:
        paras = [l.strip() for l in full_text.split("\n") if l.strip()]
        if paras:
            c_url = f"umd://{p.name}#1"
            c_imgs = [cover_image_dict] if cover_image_dict else []
            blks = [{"type": "text", "text": pr} for pr in paras]
            if c_imgs:
                blks.insert(0, {"type": "image", "url": c_imgs[0]["key"], "alt": c_imgs[0]["alt"]})
            chapters.append(SourceChapter(
                url=c_url,
                title=title,
                paragraphs=paras,
                blocks=blks,
                images=c_imgs,
                chapter_id=f"umd_{hashlib.md5(c_url.encode()).hexdigest()[:12]}",
                content_hash=compute_content_hash(paras),
            ))

    episodes = [{"title": ch.title, "url": ch.url} for ch in chapters]
    work_desc = f"從 UMD 電子書導入（共 {len(chapters)} 章"
    if publisher:
        work_desc += f"，出版：{publisher}"
    if year:
        work_desc += f"，年份：{year}"
    work_desc += "）"

    work = WorkInfo(
        url=f"local-umd:{p.name}",
        title=title,
        author=author,
        description=work_desc,
        cover_url="",
        episodes=episodes,
    )

    return work, chapters
