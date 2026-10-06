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

from translator.acquisition.source_epub import SourceChapter, WorkInfo, build_source_epub, compute_content_hash, get_chapter_id


COMMON_CHAPTER_PATTERNS = [
    # Kakuyomu / Syosetu / Web novel standard Japanese patterns
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百千萬亿两壹贰貳叁肆伍陆陸柒捌玖拾佰仟萬億〇零]+\s*(?:話|章|卷|部|篇|回|節|幕|集|折)|[0-9０-９]{1,4}\s*[\.、：\s]+[^\n]+|Chapter\s*\d+|Episode\s*\d+|Act\s*\d+|Volume\s*\d+|Section\s*\d+|プロローグ|エピローグ|序章|終章|転章|間話|幕間|番外編|後記|あとがき|前書き)(?:\s+[^\n]*)?$",
    # Markdown headings (# or ## or ###)
    r"^\s*#{1,3}\s+(.+)$",
    # Chinese novel standard patterns with brackets & keywords
    r"^\s*(?:[【\[（(]?\s*第\s*[0-9０-９一二三四五六七八九十百千萬亿两壹贰貳叁肆伍陆陸柒捌玖拾佰仟萬億〇零]+\s*[章回节節卷部篇幕话話集折]\s*[】\]）)]?)(?:\s+[^\n]*)?$",
    # Novel special sections
    r"^\s*(?:序章|序言|楔子|引子|前言|後記|后记|尾聲|尾声|番外|番外篇|結語|结语|感言|附錄|附录|最終話|终话|大結局|大结局)(?:\s+[^\n]*)?$",
]

DEFAULT_SPLIT_REGEX = (
    r"^\s*(?:"
    r"[【\[（(]?\s*第\s*[0-9０-９一二三四五六七八九十百千萬亿两壹贰貳叁肆伍陆陸柒捌玖拾佰仟萬億〇零]+\s*(?:話|章|卷|部|篇|回|節|幕|集|折)\s*[】\]）)]?|"
    r"Chapter\s*\d+|"
    r"CHAPTER\s*\d+|"
    r"Episode\s*\d+|"
    r"Act\s*\d+|"
    r"Volume\s*\d+|"
    r"Section\s*\d+|"
    r"#{1,3}\s+.+|"
    r"プロローグ|エピローグ|序章|序言|楔子|引子|前言|終章|転章|間話|幕間|番外編|番外|後記|后记|あとがき|前書き|尾聲|尾声|結語|结语|感言|附錄|附录|最終話|终话|大結局|大结局|"
    r"[0-9０-９]{1,4}\s*[\.、：\s]+[^\n]+"
    r")(?:\s+[^\n]*)?$"
)


def chinese_numeral_to_int(s: str) -> int | None:
    """
    Parses a Chinese/Japanese numeral string into an integer.
    Supports standard, financial, simplified, traditional, and positional formats,
    as well as strings with prefixes/suffixes like '第十章' or '第10章'.
    Examples: '一' -> 1, '十' -> 10, '十一' -> 11, '二十' -> 20, '一百零五' -> 105, '123' -> 123.
    """
    if not s:
        return None
    s = s.strip()

    # Strip common enclosing prefixes / suffixes like 第, 話, 章, 回, etc.
    s = re.sub(r"^[【\[（(\s]*第?\s*", "", s)
    s = re.sub(r"[\s話章卷部篇回節幕集折】\]）)]*$", "", s)
    s = s.strip()

    if not s:
        return None

    # Normalize full-width digits to ascii
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    if s.isdigit():
        return int(s)

    digit_map = {
        "零": 0, "〇": 0, "0": 0,
        "一": 1, "壹": 1, "壱": 1, "1": 1,
        "二": 2, "贰": 2, "貳": 2, "弐": 2, "两": 2, "兩": 2, "2": 2,
        "三": 3, "叁": 3, "參": 3, "参": 3, "3": 3,
        "四": 4, "肆": 4, "4": 4,
        "五": 5, "伍": 5, "5": 5,
        "六": 6, "陆": 6, "陸": 6, "6": 6,
        "七": 7, "柒": 7, "7": 7,
        "八": 8, "捌": 8, "8": 8,
        "九": 9, "玖": 9, "9": 9,
    }
    unit_map = {
        "十": 10, "拾": 10,
        "百": 100, "佰": 100,
        "千": 1000, "仟": 1000,
        "万": 10000, "萬": 10000,
        "亿": 100000000, "億": 100000000,
    }

    # Positional digits without units (e.g. '一二三' -> 123)
    if all(c in digit_map for c in s):
        return int("".join(str(digit_map[c]) for c in s))

    total = 0
    section = 0
    curr_num = 0

    for c in s:
        if c in digit_map:
            curr_num = digit_map[c]
        elif c in unit_map:
            unit = unit_map[c]
            if unit == 100000000:
                section = (section + (curr_num if curr_num != 0 else (1 if not section else 0))) * unit
                total += section
                section = 0
                curr_num = 0
            elif unit == 10000:
                section = (section + (curr_num if curr_num != 0 else (1 if not section else 0))) * unit
                total += section
                section = 0
                curr_num = 0
            else:
                if curr_num == 0:
                    curr_num = 1
                section += curr_num * unit
                curr_num = 0
        else:
            # Non-numeral char encountered
            return None

    return total + section + curr_num


def natural_sort_key(s: str) -> list[Any]:
    """
    Sort strings containing numbers (Arabic, full-width, or Chinese/Japanese chapter numerals)
    in natural human order (e.g., 第一章, 第二章, 第十章 instead of 第一章, 第十章, 第二章).
    """
    s_clean = str(s).strip()

    # Pattern to match Chinese chapter numbers like 第十章 or pure digits
    token_pattern = re.compile(
        r"(?:第\s*([0-9０-９一二三四五六七八九十百千萬亿两壹贰貳叁肆伍陆陸柒捌玖拾佰仟萬億〇零]+)\s*(?:話|章|卷|部|篇|回|節|幕|集|折)|"
        r"([0-9０-９]+)|"
        r"([一二三四五六七八九十百千萬亿两壹贰貳叁肆伍陆陸柒捌玖拾佰仟萬億〇零]{2,}))"
    )

    parts: list[Any] = []
    last_end = 0

    for match in token_pattern.finditer(s_clean):
        start, end = match.span()
        if start > last_end:
            parts.append(s_clean[last_end:start].lower())

        ch_num_str = match.group(1)
        arabic_str = match.group(2)
        standalone_cn = match.group(3)

        num_val = None
        if ch_num_str:
            num_val = chinese_numeral_to_int(ch_num_str)
        elif arabic_str:
            num_val = chinese_numeral_to_int(arabic_str)
        elif standalone_cn:
            num_val = chinese_numeral_to_int(standalone_cn)

        if num_val is not None:
            parts.append(num_val)
        else:
            parts.append(match.group(0).lower())

        last_end = end

    if last_end < len(s_clean):
        parts.append(s_clean[last_end:].lower())

    return parts


def detect_text_language(samples: str | list[str]) -> str:
    """
    Detects whether text is Japanese ('ja'), Traditional Chinese ('zh-Hant'), or Simplified Chinese ('zh-Hans').
    """
    if isinstance(samples, list):
        sample_text = " ".join(samples[:50])
    else:
        sample_text = samples[:15000]

    kana_count = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF]", sample_text))
    cjk_count = len(re.findall(r"[\u4E00-\u9FFF]", sample_text))

    if kana_count > 10 or (cjk_count > 0 and (kana_count / (cjk_count + kana_count)) > 0.05):
        return "ja"

    hant_markers = len(re.findall(r"[們這為經後與變點體個麼說開時會樣來過實發動國學聲無對從幾頭歡氣聽問讓關應當處現門長車見風動點機經開關買買賣]", sample_text))
    hans_markers = len(re.findall(r"[们这为经后与变点体个么说开时样来过实发动国学声无对从几头欢气听问让关应当处现门长车见风动机开关买买卖]", sample_text))

    if hant_markers > hans_markers:
        return "zh-Hant"
    elif hans_markers > hant_markers:
        return "zh-Hans"
    return "zh-Hant"


def get_language_display_name(lang_code: str) -> str:
    """Returns human-readable Chinese name for language code."""
    if lang_code == "ja":
        return "日文原文"
    elif lang_code == "zh-Hant":
        return "繁體中文"
    elif lang_code == "zh-Hans":
        return "簡體中文"
    return "中文"


def score_cjk_text(text: str) -> float:
    """
    Evaluates text quality and CJK character ratio.
    Returns a score where valid CJK text scores positive, and mojibake / replacement chars score negative.
    """
    if not text:
        return 0.0

    total = len(text)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    kana_count = len(re.findall(r"[\u3040-\u309f\u30a0-\u30ff]", text))
    punct_count = len(re.findall(r"[\u3000-\u303f\uff01-\uffee]", text))
    ascii_count = len(re.findall(r"[\u0020-\u007e\r\n\t]", text))

    bad_count = text.count("\ufffd")
    pua_count = len(re.findall(r"[\ue000-\uf8ff]", text))
    control_count = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", text))

    good_score = (cjk_count * 2.0) + (kana_count * 2.0) + punct_count + (ascii_count * 0.5)
    penalty = (bad_count * 10.0) + (pua_count * 10.0) + (control_count * 20.0)

    return (good_score - penalty) / max(total, 1)


def detect_bytes_encoding(raw: bytes) -> str:
    """
    Intelligently detects character encoding of raw bytes with high accuracy for CJK novels.
    Handles UTF-8, UTF-8-sig, Big5, GB18030/GBK, Shift-JIS/CP932, EUC-JP, UTF-16LE/BE.
    """
    if not raw:
        return "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32le"
    if raw.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32be"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16be"

    # 1. Strict UTF-8 check first (very reliable because UTF-8 byte sequences are rigid)
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    # 2. Try charset_normalizer if available
    try:
        import charset_normalizer
        matches = charset_normalizer.from_bytes(raw)
        best = matches.best()
        if best is not None and best.encoding:
            enc = best.encoding.lower().replace("-", "_")
            if enc in ("gb2312", "gbk", "gb18030", "hz_gb_2312"):
                return "gb18030"
            if enc in ("big5", "big5_hkscs", "cp950"):
                return "big5"
            if enc in ("shift_jis", "cp932", "sjis", "shift_jisx0213", "shift_jis_2004"):
                return "cp932"
            if enc in ("euc_jp", "euc_jis_2004", "euc_jisx0213"):
                return "euc-jp"
            if enc in ("utf_16", "utf_16_le"):
                return "utf-16le"
            if enc in ("utf_16_be",):
                return "utf-16be"
            # Verify the decoded result has decent score
            try:
                test_decoded = raw[:65536].decode(best.encoding, errors="replace")
                if score_cjk_text(test_decoded) > 0.1:
                    return best.encoding
            except Exception:
                pass
    except Exception:
        pass

    # 3. Heuristic scoring across candidate encodings
    sample = raw[:65536]
    candidates = ["gb18030", "big5", "cp932", "euc-jp", "utf-16le", "utf-16be"]
    best_enc = "utf-8"
    best_score = -999999.0

    for cand in candidates:
        try:
            txt = sample.decode(cand, errors="replace")
            sc = score_cjk_text(txt)
            if sc > best_score:
                best_score = sc
                best_enc = cand
        except Exception:
            continue

    return best_enc


def detect_file_encoding(file_path: Path | str) -> str:
    """Detects encoding by reading file bytes and analyzing CJK text quality."""
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return "utf-8"
    raw = p.read_bytes()
    return detect_bytes_encoding(raw)


def read_text_file(file_path: Path | str, encoding: str | None = None) -> str:
    """Reads a text file with automatic encoding fallback and BOM stripping."""
    p = Path(file_path)
    raw = p.read_bytes()
    if not raw:
        return ""

    if encoding and encoding.lower() not in {"auto", "自動", "自動偵測", "自動偵測編碼"}:
        enc_to_try = [encoding, "utf-8", "gb18030", "big5", "cp932"]
    else:
        detected = detect_bytes_encoding(raw)
        enc_to_try = [detected, "utf-8", "gb18030", "big5", "cp932", "latin-1"]

    for enc in enc_to_try:
        try:
            return raw.decode(enc).lstrip("\ufeff")
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace").lstrip("\ufeff")


def clean_markdown_line(line: str) -> str:
    """Strips leading markdown heading symbols and BOM while preserving line content."""
    line = line.lstrip("\ufeff")
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
    sort_chapters: bool = False,
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
    has_matched_first_header = False

    for line in lines:
        stripped = line.strip()
        if split_pattern.match(stripped):
            # Save previous section if exists
            if current_lines or current_title:
                raw_text = "\n".join(current_lines)
                clean_txt, imgs = parse_markdown_images(raw_text, p.parent)
                paras = split_text_into_paragraphs(clean_txt)
                if paras:
                    if not has_matched_first_header and not current_title:
                        # Preamble / Intro before the first chapter
                        ch_title = "作品簡介 / 前言"
                        ch_url = f"file://{p.name}#intro"
                    else:
                        ch_title = current_title or f"第 {chapter_index} 章"
                        ch_url = f"file://{p.name}#{chapter_index}"
                        chapter_index += 1

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
            has_matched_first_header = True
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
            if not has_matched_first_header and not current_title:
                ch_title = p.stem
                ch_url = f"file://{p.name}#1"
            else:
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

    if sort_chapters and len(chapters) > 1:
        # Sort chapters if requested
        intro = [ch for ch in chapters if ch.url.endswith("#intro")]
        rest = [ch for ch in chapters if not ch.url.endswith("#intro")]
        rest.sort(key=lambda c: natural_sort_key(c.title))
        chapters = intro + rest

    return chapters


def parse_directory_chapters(
    dir_path: Path | str,
    encoding: str | None = None,
    recursive: bool = False,
) -> list[SourceChapter]:
    """Reads a directory of .txt/.md/.umd/.jar files in natural numerical order as individual chapters."""
    folder = Path(dir_path)
    if not folder.is_dir():
        raise NotADirectoryError(f"指定的路徑不是資料夾：{dir_path}")

    exts = {".txt", ".md", ".markdown", ".umd", ".jar"}
    files = [f for f in (folder.rglob("*") if recursive else folder.iterdir()) if f.is_file() and f.suffix.lower() in exts]
    files.sort(key=lambda x: natural_sort_key(x.name))

    chapters: list[SourceChapter] = []
    for idx, f in enumerate(files, 1):
        f_ext = f.suffix.lower()
        if f_ext == ".umd":
            from translator.formats.umd_parser import parse_umd_file
            try:
                _, sub_chaps = parse_umd_file(f)
                chapters.extend(sub_chaps)
                continue
            except Exception:
                pass
        elif f_ext == ".jar":
            from translator.formats.jar_parser import parse_jar_file
            try:
                _, sub_chaps = parse_jar_file(f)
                chapters.extend(sub_chaps)
                continue
            except Exception:
                pass

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
    sort_chapters: bool = False,
) -> tuple[WorkInfo, list[SourceChapter]]:
    """Loads a TXT/Markdown/UMD/JAR file or folder and returns WorkInfo + list of SourceChapter."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"找不到路徑：{path}")

    if src.is_dir():
        chapters = parse_directory_chapters(src, encoding=encoding, recursive=recursive)
        work_title = title.strip() or src.name
        work_author = author.strip() or "未知作者"
        work_desc = description.strip() or f"從外部文字資料夾導入之作品（共 {len(chapters)} 章）"
        work_url = f"local-dir:{src.name}"
    else:
        suffix = src.suffix.lower()
        if suffix == ".umd":
            from translator.formats.umd_parser import parse_umd_file
            work, chapters = parse_umd_file(src)
            work_title = title.strip() or work.title
            work_author = author.strip() or work.author
            work_desc = description.strip() or work.description
            work_url = work.url
        elif suffix == ".jar":
            from translator.formats.jar_parser import parse_jar_file
            work, chapters = parse_jar_file(src)
            work_title = title.strip() or work.title
            work_author = author.strip() or work.author
            work_desc = description.strip() or work.description
            work_url = work.url
        else:
            chapters = parse_single_file_chapters(src, pattern=split_pattern, encoding=encoding, sort_chapters=sort_chapters)
            work_title = title.strip() or src.stem
            work_author = author.strip() or "未知作者"
            work_desc = description.strip() or f"從外部文字檔案導入之作品（共 {len(chapters)} 章）"
            work_url = f"local-txt:{src.name}"

    if not chapters:
        raise ValueError(f"未能從「{src.name}」解析出任何有效章節內文。請檢查檔案格式或正則設定。")

    episodes = [{"title": ch.title, "url": ch.url} for ch in chapters]
    work = WorkInfo(
        url=work_url,
        title=work_title,
        author=work_author,
        description=work_desc,
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
    language: str = "auto",
    split_pattern: str | None = None,
    cover_path: Path | str | None = None,
    encoding: str | None = None,
    sort_chapters: bool = False,
) -> Path:
    """Directly converts a TXT/Markdown source into a standard EPUB file."""
    work, chapters = import_text_source(
        source_path,
        title=title,
        author=author,
        description=description,
        split_pattern=split_pattern,
        encoding=encoding,
        sort_chapters=sort_chapters,
    )
    cover_data = None
    if cover_path and Path(cover_path).exists():
        c_bytes = Path(cover_path).read_bytes()
        c_mime = mimetypes.guess_type(str(cover_path))[0] or "image/jpeg"
        cover_data = (c_bytes, c_mime)

    out_p = Path(output_epub_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    if language == "auto":
        sample = "\n".join(["\n".join(ch.paragraphs[:5]) for ch in chapters[:10]])
        epub_lang = detect_text_language(sample)
    elif "日" in language or language.lower() == "ja":
        epub_lang = "ja"
    elif "繁" in language or language.lower() in {"zh-hant", "zh-tw", "zh-hk"}:
        epub_lang = "zh-Hant"
    elif "簡" in language or language.lower() in {"zh-hans", "zh-cn"}:
        epub_lang = "zh-Hans"
    else:
        epub_lang = "zh-Hant"
    return build_source_epub(work, chapters, out_p, cover=cover_data, language=epub_lang)


def create_project_from_text(
    source_path: Path | str,
    work_dir: Path | str,
    title: str = "",
    author: str = "",
    description: str = "",
    language: str = "繁體中文",
    split_pattern: str | None = None,
    as_translated: bool = False,
    encoding: str | None = None,
    sort_chapters: bool = False,
) -> Path:
    """Converts a TXT/Markdown source into an editable translation project directory."""
    work, chapters = import_text_source(
        source_path,
        title=title,
        author=author,
        description=description,
        split_pattern=split_pattern,
        encoding=encoding,
        sort_chapters=sort_chapters,
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

        if as_translated:
            project_chapters.append({
                "title": ch.title,
                "url": ch.url,
                "chapter_id": ch.chapter_id,
                "content_hash": ch.content_hash,
                "japanese": ch.paragraphs,
                "paragraphs": ch.paragraphs,
                "translation": ch.paragraphs,
                "blocks": ch.blocks,
                "images": ch_images,
                "status": "completed",
            })
        else:
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
