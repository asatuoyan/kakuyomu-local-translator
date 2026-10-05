"""Text-only, on-demand reading of saved novels."""
import hashlib
import json
import os
import posixpath
import threading
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from project_storage import atomic_json


class SavedReading:
    def __init__(self, path, *, chapter_limit=None):
        self.path = Path(path).resolve()
        self.identity = str(self.path)
        self.lock = threading.Lock()
        self.loaded = OrderedDict()
        self.entries = []
        self.title = self.path.stem
        self.language = "译文"
        self.legacy = None
        if self.path.suffix.lower() == ".epub":
            self._epub_index()
        else:
            self._project_index()
        if chapter_limit is not None:
            if isinstance(chapter_limit, bool) or not isinstance(chapter_limit, int) or chapter_limit < 1:
                raise ValueError("阅读范围必须是大于零的整数。")
            self.entries = self.entries[:chapter_limit]
        if not self.entries:
            raise ValueError("所选文件没有可阅读的章节。")
        self.catalog = []
        volume = "未分卷"
        import re
        for i, entry in enumerate(self.entries, 1):
            match = re.search(r"第\s*[0-9０-９一二三四五六七八九十百千零〇两]+\s*[卷巻部]", entry["title"])
            volume = entry.get("volume") or (match.group(0) if match else volume)
            self.catalog.append({"id": str(i), "title": entry["title"], "language": self.language,
                                 "volume": volume, "revision": 1})

    def _epub_index(self):
        with ZipFile(self.path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(n for n in container.iter() if n.tag.endswith("}rootfile"))
            opf = rootfile.attrib["full-path"]
            root = ET.fromstring(archive.read(opf))
            self.title = next((n.text for n in root.iter() if n.tag.endswith("}title") and n.text), self.title)
            manifest = {n.attrib["id"]: n.attrib for n in root.iter() if n.tag.endswith("}item")}
            def resolve(base, href):
                return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(href.split("#")[0])))
            labels = {}
            for item in manifest.values():
                if "nav" in item.get("properties", "").split():
                    navpath = resolve(opf, item["href"])
                    soup = BeautifulSoup(archive.read(navpath), "html.parser")
                    nav = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
                    if nav:
                        for a in nav.find_all("a", href=True):
                            labels.setdefault(resolve(navpath, a["href"]), a.get_text(" ", strip=True))
                elif item.get("media-type") == "application/x-dtbncx+xml":
                    ncxpath = resolve(opf, item["href"])
                    ncx = ET.fromstring(archive.read(ncxpath))
                    for point in ncx.iter():
                        if not point.tag.endswith("}navPoint"):
                            continue
                        content = next((n for n in point if n.tag.endswith("}content")), None)
                        label = next((n for n in point if n.tag.endswith("}navLabel")), None)
                        if content is not None and label is not None:
                            labels.setdefault(resolve(ncxpath, content.attrib["src"]), "".join(label.itertext()).strip())
            for node in root.iter():
                if not node.tag.endswith("}itemref"):
                    continue
                idref = node.attrib["idref"]
                item = manifest.get(idref, {})
                if item.get("media-type") != "application/xhtml+xml" or "nav" in item.get("properties", "").split() or idref in {"nav", "cover", "introduction"}:
                    continue
                name = resolve(opf, item["href"])
                self.entries.append({"file": name, "title": labels.get(name) or f"第{len(self.entries)+1}章"})

    def _project_index(self):
        if self.path.name not in {"translation-project.json", "project.json"}:
            raise ValueError("请选择译文 EPUB 或项目清单。")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.title = state.get("metadata", {}).get("title") or state.get("title") or self.path.parent.name
        self.language = state.get("language") or "译文"
        if state.get("schema_version") != 2 or self.path.name != "translation-project.json":
            self.legacy = state.get("chapters", [])
            self.entries = [{"title": ch.get("title") or ch.get("episode_title") or f"第{i+1}章", "record": i}
                            for i, ch in enumerate(self.legacy) if any(ch.get("translation") or ch.get("paragraphs", []))]
            return
        files = [self.path.parent / "chapters" / f"{i:06d}.json" for i in range(1, state["chapter_count"] + 1)]
        fingerprint = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in [self.path, *files]]
        digest = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()
        cache_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache"))) / "kakuyomu-local-translator" / "reader-cache"
        cache_path = cache_root / (hashlib.sha256(self.identity.encode()).hexdigest() + ".json")
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if cached.get("fingerprint") == digest:
            self.entries = cached["entries"]
            return
        for p in files:
            ch = json.loads(p.read_text(encoding="utf-8"))
            if any(ch.get("translation") or ch.get("paragraphs", [])):
                self.entries.append({"file": str(p), "title": ch.get("title") or ch.get("episode_title") or p.stem})
        try:
            atomic_json(cache_path, {"fingerprint": digest, "entries": self.entries})
        except OSError:
            pass

    def chapter(self, key):
        if not key.isascii() or not key.isdecimal() or not 1 <= int(key) <= len(self.entries) or str(int(key)) != key:
            return None
        with self.lock:
            if key in self.loaded:
                self.loaded.move_to_end(key)
                return self.loaded[key]
            entry = self.entries[int(key) - 1]
            title = entry["title"]
            if self.path.suffix.lower() == ".epub":
                with ZipFile(self.path) as archive:
                    soup = BeautifulSoup(archive.read(entry["file"]), "html.parser")
                heading = soup.find(["h1", "h2"]) or soup.title
                if heading:
                    title = heading.get_text(" ", strip=True)
                texts, pending = [], []
                def flush():
                    text = "".join(pending).strip()
                    pending.clear()
                    if text:
                        texts.append(text)
                def walk(node):
                    if isinstance(node, Comment):
                        return
                    if isinstance(node, NavigableString):
                        pending.append(str(node))
                        return
                    if not isinstance(node, Tag) or node is heading or node.name in {"head", "script", "style", "rt", "rp", "img", "image"}:
                        return
                    boundary = node.name in {"p", "div", "section", "article", "blockquote", "li", "ul", "ol", "pre", "br", "hr", "h1", "h2", "h3", "tr", "td"}
                    if boundary:
                        flush()
                    for child in node.children:
                        walk(child)
                    if boundary:
                        flush()
                walk(soup.body or soup)
                flush()
                originals = [""] * len(texts)
            else:
                ch = self.legacy[entry["record"]] if self.legacy is not None else json.loads(Path(entry["file"]).read_text(encoding="utf-8"))
                texts = ch.get("translation") or ch.get("paragraphs", [])
                originals = ((ch.get("source_paragraphs") or ch.get("japanese") or []) + [""] * len(texts))[:len(texts)]
            result = {"title": title, "originals": originals, "translations": texts, "revision": 1}
            self.loaded[key] = result
            while len(self.loaded) > 8:
                self.loaded.popitem(last=False)
            return result
