"""Book discovery, cached metadata and recent task history."""
import time
from pathlib import Path
from translator.storage.project_storage import load_json
from translator.glossary.manager import load_project_glossary


class LibraryMixin:
    def projects(self):
        with self.lock:
            root = Path(self.cfg["output_dir"])
            if self._discovery_root != root or time.monotonic() >= self._discovery_until:
                self._project_paths = tuple(p for p in root.rglob("translation-project.json") if ".backups" not in p.parts)
                self._network_paths = tuple((root / "network-workflows").glob("*/network.json"))
                self._discovery_root = root
                self._discovery_until = time.monotonic() + 10
            return self._list_projects()

    @staticmethod
    def _file_stamp(path):
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None

    def _cached_json(self, path, default):
        with self.lock:
            stamp = self._file_stamp(path)
            cached = self._json_cache.get(path)
            if cached is None or cached[0] != stamp:
                cached = (stamp, load_json(path, default))
                self._json_cache[path] = cached
            return cached[1]

    def _term_count(self, folder):
        path = folder / "glossary.json"
        stamp = self._file_stamp(path)
        cached = self._glossary_counts.get(path)
        if cached is None or cached[0] != stamp:
            cached = (stamp, len(load_project_glossary(folder)))
            self._glossary_counts[path] = cached
        return cached[1]

    def _list_projects(self):
        from translator.engine import project_exists
        result = []
        root = Path(self.cfg["output_dir"])
        network_sources = {}
        network_totals = {}
        network_books = {}
        for network_file in self._network_paths:
            try:
                saved = self._cached_json(network_file, {})
                if saved.get("source") and saved.get("url"):
                    network_sources[Path(saved["source"]).stem] = saved["url"]
                    network_totals[Path(saved["source"]).stem] = saved.get("total_chapters", 0)
                    network_books[Path(saved["source"]).stem] = (network_file, saved)
            except (OSError, ValueError):
                continue
        paths = [(stamp, path) for path in self._project_paths if (stamp := self._file_stamp(path)) is not None]
        represented = set()
        for _, path in sorted(paths, key=lambda item: item[0], reverse=True):
            folder = path.parent
            if project_exists(folder):
                try:
                    project = self._cached_json(path, {})
                    source = project.get("source_path", "")
                    url = network_sources.get(Path(source).stem, "") if source else ""
                    network = network_books.get(Path(source).stem, (None, {}))[1] if source else {}
                    if network:
                        represented.add(Path(source).stem)
                    output = project.get("output_path", "")
                    if not output:
                        from translator.engine import safe_name
                        from translator.languages import language_suffix
                        if project.get("language"):
                            base = safe_name(project.get("metadata", {}).get("title", folder.name)) + "_" + language_suffix(project["language"])
                            output = next((str(folder / (base + suffix + ".epub")) for suffix in ("", "_双语对照") if (folder / (base + suffix + ".epub")).is_file()), "")
                    download = ""
                    if output:
                        candidate = Path(output).resolve()
                        if candidate.is_relative_to(root.resolve()) and candidate.is_file() and not (project.get("restored_backup") and project.get("epub_dirty")):
                            download = str(candidate.relative_to(root.resolve()))
                    chapters = project.get("chapter_count", len(project.get("chapters", [])))
                    total = max(chapters, project.get("source_chapter_count", 0),
                                project.get("merged_up_to", 0), network_totals.get(Path(source).stem, 0))
                    completed = bool(download and not project.get("epub_dirty", False)
                                     and chapters >= total)
                    source_download = self._source_download(network)
                    result.append({"id": str(folder.relative_to(root)),
                                   "title": project.get("metadata", {}).get("title", folder.name),
                                   "language": project.get("language", ""),
                                   "term_count": self._term_count(folder),
                                   "resume": {"url": "" if source_download else url,
                                              "source": network["source"] if source_download else source if not url else "",
                                              "language": project.get("language", "zh-Hans")} if url or (source and Path(source).is_file()) else None,
                                   "download": download,
                                   "source_download": source_download, "update_url": network.get("url", ""),
                                   "acquired_chapters": network.get("acquired_chapters", 0),
                                   "chapters": chapters, "total_chapters": total,
                                   "completed": completed})
                except (OSError, ValueError, KeyError):
                    continue
        for name, (path, saved) in network_books.items():
            if name in represented or not saved.get("work", {}).get("title"):
                continue
            source_download = self._source_download(saved)
            result.append({"id": str(path.parent.relative_to(root)), "kind": "source",
                           "title": saved["work"]["title"], "language": "原文", "term_count": 0,
                           "chapters": 0, "total_chapters": saved.get("total_chapters", 0),
                           "acquired_chapters": saved.get("acquired_chapters", 0),
                           "completed": bool(source_download), "download": "", "source_download": source_download,
                           "acquire_url": saved["url"], "update_url": saved["url"],
                           "resume": {"source": saved["source"], "language": "zh-Hans"} if source_download else None})
        result.sort(key=lambda item: self._file_stamp(root / item["id"] /
                    ("network.json" if item.get("kind") == "source" else "translation-project.json")) or (0, 0),
                    reverse=True)
        return result

    def _source_download(self, saved):
        if not saved or saved.get("acquisition_complete") is False:
            return ""
        if not 0 < saved.get("total_chapters", 0) <= saved.get("acquired_chapters", 0):
            return ""
        root = Path(self.cfg["output_dir"]).resolve()
        source = Path(saved.get("source", "")).resolve()
        if not source.is_relative_to(root) or source.suffix.lower() != ".epub" or not source.is_file():
            return ""
        if "acquisition_complete" not in saved and saved.get("stage") not in ("acquired", "complete"):
            # Older workflows could overwrite "acquired" when translation failed.
            # Verify the existing EPUB once rather than treating a preview as full.
            from translator.reading.saved_reading import SavedReading
            from zipfile import BadZipFile
            stamp = self._file_stamp(source), saved["total_chapters"]
            cached = self._source_completeness.get(source)
            if cached is None or cached[0] != stamp:
                try:
                    complete = len(SavedReading(source).entries) == saved["total_chapters"]
                except (OSError, ValueError, BadZipFile):
                    complete = False
                cached = stamp, complete
                self._source_completeness[source] = cached
            if not cached[1]:
                return ""
        return str(source.relative_to(root))

    def project(self, identity):
        root = Path(self.cfg["output_dir"]).resolve()
        path = (root / identity).resolve()
        if not path.is_relative_to(root) or not (path / "translation-project.json").is_file():
            raise ValueError("作品不存在")
        return path

    def status(self):
        with self.lock:
            return {"task": dict(self.task), "restart_pending": self.restart_requested,
                    "acquisition_task": dict(self.acquisition_task),
                    "acquisition_busy": self._browser_owner is not None, "projects": self.projects(),
                    "history": self.recent_tasks(), "queue": self.queued_tasks, "completed_tasks": self.completed_tasks, "queue_error": self.queue_error, "queue_paused": self.queue_paused,
                    "model": self.cfg["model"], "ollama_url": self.cfg["ollama_url"]}

    def recent_tasks(self):
        from translator.acquisition.network_workflow import network_path
        result, seen = [], set()
        for item in self._cached_json(self.history_path, []):
            url, source = item.get("url", ""), item.get("source", "")
            title = item.get("title", "")
            if source and not url:
                # Streaming snapshots and completed originals refer to the same
                # downloaded book, rather than novels named after cache files.
                path = Path(source).resolve()
                network_root = (Path(self.cfg["output_dir"]) / "network-workflows").resolve()
                if path.is_relative_to(network_root):
                    folder = path.parent.parent if path.parent.name == "stream" else path.parent
                    try:
                        saved = self._cached_json(folder / "network.json", {})
                        if saved.get("url") and Path(saved.get("source", "")).stem == path.stem:
                            url, source = saved["url"], ""
                            title = saved.get("work", {}).get("title") or title
                    except (OSError, ValueError):
                        pass
            identity = url or source
            if not identity or identity in seen:
                continue
            seen.add(identity)
            if url:
                try:
                    saved = self._cached_json(network_path(url, self.cfg), {})
                    title = saved.get("work", {}).get("title") or title
                except (ValueError, OSError):
                    pass
            else:
                title = title or Path(source).stem
            result.append({**item, "url": url, "source": source, "title": title or "尚未获取书名"})
        return result
