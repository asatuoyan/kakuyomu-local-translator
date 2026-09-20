from __future__ import annotations

import html
import mimetypes
import posixpath
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup


CONTAINER = "META-INF/container.xml"
EPUB_NS = "http://www.idpf.org/2007/ops"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _opf_path(archive: zipfile.ZipFile) -> str:
    root = ET.fromstring(archive.read(CONTAINER))
    for node in root.iter():
        if _local(node.tag) == "rootfile" and node.get("full-path"):
            return node.get("full-path", "")
    raise ValueError("EPUB 的 container.xml 沒有 OPF 路徑。")


def inspect_epub(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        opf_path = _opf_path(zf)
        opf = ET.fromstring(zf.read(opf_path))
        title = ""
        author = ""
        manifest: dict[str, tuple[str, str, str]] = {}
        spine_ids: list[str] = []
        for node in opf.iter():
            name = _local(node.tag)
            if name == "title" and not title:
                title = "".join(node.itertext()).strip()
            elif name == "creator" and not author:
                author = "".join(node.itertext()).strip()
            elif name == "item":
                manifest[node.get("id", "")] = (
                    node.get("href", ""), node.get("media-type", ""), node.get("properties", "")
                )
            elif name == "itemref":
                spine_ids.append(node.get("idref", ""))
        opf_dir = posixpath.dirname(opf_path)
        samples: list[str] = []
        for item_id in spine_ids:
            href, media_type, _ = manifest.get(item_id, ("", "", ""))
            if media_type not in {"application/xhtml+xml", "text/html"}:
                continue
            full = posixpath.normpath(posixpath.join(opf_dir, href))
            if full not in zf.namelist():
                continue
            soup = BeautifulSoup(zf.read(full), "html.parser")
            text = soup.get_text("", strip=True)
            if text:
                samples.append(text[:3000])
            if sum(map(len, samples)) >= 12000:
                break
    return {"title": title or path.stem, "author": author, "sample": "".join(samples)}


def detect_chinese_variant(text: str) -> tuple[str, int, int]:
    traditional = set("體學國會為與這個們來時說後裡書見點開關萬東車長門風雲龍劍戰聲無嗎還過從將讓實現發現應該於")
    simplified = set("体学国会为与这个们来时说后里书见点开关万东车长门风云龙剑战声无吗还过从将让实现发现应该于")
    t_score = sum(text.count(ch) for ch in traditional)
    s_score = sum(text.count(ch) for ch in simplified)
    return ("繁體中文" if t_score >= s_score else "简体中文", t_score, s_score)


def create_project_from_epub(source: Path, work_dir: Path) -> dict[str, Any]:
    info = inspect_epub(source)
    variant, traditional_score, simplified_score = detect_chinese_variant(info["sample"])
    base = work_dir / "base-original.epub"
    shutil.copy2(source, base)
    project = {
        "work_title": info["title"],
        "author": info["author"],
        "language": variant,
        "variant_scores": {"traditional": traditional_score, "simplified": simplified_score},
        "base_epub": base.name,
        "chapters": [],
    }
    return project


def _find_manifest_and_spine(opf: ET.Element) -> tuple[ET.Element, ET.Element]:
    manifest = spine = None
    for node in opf.iter():
        if _local(node.tag) == "manifest":
            manifest = node
        elif _local(node.tag) == "spine":
            spine = node
    if manifest is None or spine is None:
        raise ValueError("EPUB OPF 缺少 manifest 或 spine。")
    return manifest, spine


def _namespace_of(element: ET.Element) -> str:
    return element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""


def _tag(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}" if ns else name


def _append_nav(nav_bytes: bytes, additions: list[tuple[str, str]]) -> bytes:
    root = ET.fromstring(nav_bytes)
    toc_nav = None
    for node in root.iter():
        if _local(node.tag) != "nav":
            continue
        if node.get(f"{{{EPUB_NS}}}type") == "toc" or node.get("epub:type") == "toc":
            toc_nav = node
            break
    if toc_nav is None:
        raise ValueError("原 EPUB 的导航文件中找不到 toc。")
    ol = next((x for x in toc_nav.iter() if _local(x.tag) == "ol"), None)
    if ol is None:
        raise ValueError("原 EPUB 的 toc 中找不到有序列表。")
    ns = _namespace_of(ol)
    for title, href in additions:
        li = ET.SubElement(ol, _tag(ns, "li"))
        anchor = ET.SubElement(li, _tag(ns, "a"), {"href": href})
        anchor.text = title
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _append_ncx(ncx_bytes: bytes, additions: list[tuple[str, str]]) -> bytes:
    root = ET.fromstring(ncx_bytes)
    nav_map = next((x for x in root.iter() if _local(x.tag) == "navMap"), None)
    if nav_map is None:
        return ncx_bytes
    ns = _namespace_of(nav_map)
    orders = []
    for node in nav_map.iter():
        if _local(node.tag) == "navPoint":
            try:
                orders.append(int(node.get("playOrder", "0")))
            except ValueError:
                pass
    start = max(orders, default=0)
    for offset, (title, href) in enumerate(additions, 1):
        point = ET.SubElement(nav_map, _tag(ns, "navPoint"), {
            "id": f"translated-nav-{start + offset}", "playOrder": str(start + offset)
        })
        label = ET.SubElement(point, _tag(ns, "navLabel"))
        ET.SubElement(label, _tag(ns, "text")).text = title
        ET.SubElement(point, _tag(ns, "content"), {"src": href})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _chapter_xhtml(title: str, blocks: list[dict[str, str]], image_hrefs: dict[str, str], lang: str) -> bytes:
    content = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang}" xml:lang="{lang}">',
        "<head>", f"<title>{html.escape(title)}</title>",
        "<style>body{font-family:serif;line-height:1.8}p{margin:.8em 0}img{max-width:100%;height:auto;display:block;margin:1em auto}</style>",
        "</head><body>", f"<h1>{html.escape(title)}</h1>"
    ]
    for block in blocks:
        if block.get("type") == "text":
            content.append(f"<p>{html.escape(block.get('translation', ''))}</p>")
        elif block.get("type") == "image" and block.get("key") in image_hrefs:
            src = image_hrefs[block["key"]]
            content.append(f'<p class="illustration"><img src="{html.escape(src)}" alt="{html.escape(block.get("alt", ""))}"/></p>')
    content.append("</body></html>")
    return "\n".join(content).encode("utf-8")


def build_extended_epub(project: dict[str, Any], work_dir: Path) -> Path:
    base = work_dir / project["base_epub"]
    output = work_dir / f"{project['work_title']}-續譯.epub"
    if not project.get("chapters"):
        shutil.copy2(base, output)
        return output
    temp = output.with_suffix(".epub.tmp")
    with zipfile.ZipFile(base) as source:
        opf_path = _opf_path(source)
        opf_dir = posixpath.dirname(opf_path)
        opf = ET.fromstring(source.read(opf_path))
        manifest, spine = _find_manifest_and_spine(opf)
        ns = _namespace_of(manifest)
        manifest_items = [x for x in manifest if _local(x.tag) == "item"]
        nav_item = next((x for x in manifest_items if "nav" in x.get("properties", "").split()), None)
        ncx_item = next((x for x in manifest_items if x.get("media-type") == "application/x-dtbncx+xml"), None)
        replacements: dict[str, bytes] = {}
        additions_for_nav: list[tuple[str, str]] = []
        extra_files: dict[str, tuple[bytes, str]] = {}
        added_image_ids: set[str] = set()
        for index, chapter in enumerate(project.get("chapters", []), 1):
            uid = f"translated-chapter-{index:04d}"
            chapter_rel = f"translated/chapter_{index:04d}.xhtml"
            chapter_full = posixpath.normpath(posixpath.join(opf_dir, chapter_rel))
            image_hrefs: dict[str, str] = {}
            for image in chapter.get("images", []):
                local = work_dir / image["local_path"]
                if not local.exists():
                    continue
                ext = local.suffix.lower() or ".bin"
                image_id = f"translated-image-{uuid.uuid5(uuid.NAMESPACE_URL, image['key']).hex}"
                image_rel = f"translated/images/{uuid.uuid5(uuid.NAMESPACE_URL, image['key']).hex}{ext}"
                image_full = posixpath.normpath(posixpath.join(opf_dir, image_rel))
                media = image.get("media_type") or mimetypes.guess_type(local.name)[0] or "application/octet-stream"
                extra_files[image_full] = (local.read_bytes(), media)
                image_hrefs[image["key"]] = posixpath.relpath(image_full, posixpath.dirname(chapter_full))
                if image_id not in added_image_ids:
                    ET.SubElement(manifest, _tag(ns, "item"), {"id": image_id, "href": image_rel, "media-type": media})
                    added_image_ids.add(image_id)
            lang = "zh-Hant" if "繁" in project.get("language", "") else "zh-Hans"
            extra_files[chapter_full] = (_chapter_xhtml(chapter["title"], chapter["blocks"], image_hrefs, lang), "application/xhtml+xml")
            ET.SubElement(manifest, _tag(ns, "item"), {"id": uid, "href": chapter_rel, "media-type": "application/xhtml+xml"})
            ET.SubElement(spine, _tag(ns, "itemref"), {"idref": uid})
            nav_href = posixpath.relpath(chapter_full, posixpath.dirname(posixpath.normpath(posixpath.join(opf_dir, nav_item.get("href"))))) if nav_item is not None else chapter_rel
            additions_for_nav.append((chapter["title"], nav_href))
        replacements[opf_path] = ET.tostring(opf, encoding="utf-8", xml_declaration=True)
        if nav_item is not None:
            nav_path = posixpath.normpath(posixpath.join(opf_dir, nav_item.get("href", "")))
            replacements[nav_path] = _append_nav(source.read(nav_path), additions_for_nav)
        if ncx_item is not None:
            ncx_path = posixpath.normpath(posixpath.join(opf_dir, ncx_item.get("href", "")))
            ncx_links = []
            for i, chapter in enumerate(project.get("chapters", []), 1):
                chapter_full = posixpath.normpath(posixpath.join(opf_dir, f"translated/chapter_{i:04d}.xhtml"))
                ncx_links.append((chapter["title"], posixpath.relpath(chapter_full, posixpath.dirname(ncx_path))))
            replacements[ncx_path] = _append_ncx(source.read(ncx_path), ncx_links)
        with zipfile.ZipFile(temp, "w") as target:
            if "mimetype" in source.namelist():
                target.writestr("mimetype", source.read("mimetype"), compress_type=zipfile.ZIP_STORED)
            for info in source.infolist():
                if info.filename == "mimetype":
                    continue
                data = replacements.get(info.filename, source.read(info.filename))
                target.writestr(info, data)
            for name, (data, _) in extra_files.items():
                target.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    temp.replace(output)
    return output
