"""Local web UI sharing the existing acquisition and translation engine."""
import argparse
from dataclasses import asdict
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from translator.config import load_config, update_config
from translator.version import VERSION
from translator.paths import APP_DIR, WEB_DIR
from translator.glossary.manager import (import_from_json_list,
                              load_project_glossary, merge_glossaries, save_project_glossary)
from translator.storage.project_storage import atomic_json, load_json


from translator.ui.web_library import LibraryMixin
from translator.ui.web_recovery import RecoveryMixin
from translator.ui.web_queue import QueueMixin
from translator.ui.web_backups import BackupMixin
from translator.ui.web_book_updates import BookUpdatesMixin
from translator.ui.web_diagnostics import DiagnosticsMixin


class Application(LibraryMixin, RecoveryMixin, QueueMixin, BackupMixin, BookUpdatesMixin, DiagnosticsMixin):
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.acquisition_cancel = threading.Event()
        self.acquisition_task = {"running": False, "percent": 0, "message": "准备就绪"}
        self._browser_owner = None
        self.task = {"running": False, "percent": 0, "message": "准备就绪"}
        self.reader = None
        self._project_paths = ()
        self._network_paths = ()
        self._discovery_root = None
        self._discovery_until = 0
        self._json_cache = {}
        self._glossary_counts = {}
        self._source_completeness = {}
        self._stage_context = {}
        self.history_path = Path(self.cfg["output_dir"]) / "web-tasks.json"
        from translator.ui.web_updates import UpdateMonitor
        self.updates = UpdateMonitor(APP_DIR)
        self.restart_callback = None
        self.restart_requested = False
        self.restart_error = ""
        self._restart_generation = 0
        self._restart_committed = False
        from translator.ui.web_task_recovery import TaskJournal
        self.recovery = TaskJournal(self.cfg["output_dir"])
        self.closing = threading.Event()
        self._recovery_started = False
        self._recovery_retry_delay = 10
        self.init_queue()
        self.init_diagnostics()
        self.recover_backup_transactions()

    def models(self):
        from translator.engine import installed_models, model_is_installed
        names = sorted(name for name in installed_models(self.cfg) if name)
        preferred = self.cfg["model"]
        selected = next((name for name in names if name == preferred), "")
        if not selected and model_is_installed(preferred, set(names)):
            selected = next(name for name in names if name.split(":", 1)[0] == preferred)
        return {"models": names, "selected": selected or (names[0] if names else ""),
                "ollama_url": self.cfg["ollama_url"]}

    def start(self, body, *, _restore_id=None):
        from translator.acquisition.source_epub import normalize_work_url
        from translator.languages import language_code
        with self.lock:
            if self.queue_paused:
                if _restore_id is not None or body.get("_queue_id"):
                    return
                raise ValueError("\u961f\u5217\u5df2\u6682\u505c\uff0c\u8bf7\u5148\u70b9\u51fb\u7ee7\u7eed\u961f\u5217")
            if self.closing.is_set() or (_restore_id is None and not self.queue_matches(body)) or _restore_id is not None and not self.recovery.matches("translation", _restore_id):
                return
            if self.task["running"]:
                raise ValueError("已有任务正在运行")
        url = str(body.get("url", "")).strip()
        source = str(body.get("source", "")).strip()
        if url:
            url = normalize_work_url(url)
            source = ""
            from translator.acquisition.network_workflow import network_path
            with self.lock:
                saved = self._cached_json(network_path(url, self.cfg), {})
                if self._source_download(saved):
                    source = saved["source"]
                    url = ""
            with self.lock:
                if url and self._browser_owner is not None:
                    raise ValueError("已有作品正在获取，请等待获取完成")
        elif source:
            path = Path(source).resolve()
            network_root = (Path(self.cfg["output_dir"]) / "network-workflows").resolve()
            if path.is_relative_to(network_root) and path.parent.name == "stream":
                with self.lock:
                    saved = self._cached_json(path.parent.parent / "network.json", {})
                    if Path(saved.get("source", "")).stem == path.stem and self._source_download(saved):
                        source = saved["source"]
        if not url and (not source or not Path(source).is_file() or Path(source).suffix.lower() != ".epub"):
            raise ValueError("请输入小说网址或有效的本地 EPUB 路径")
        language = language_code(body.get("language", "zh-Hans"))
        model = str(body.get("model", self.cfg["model"])).strip()
        if not model:
            raise ValueError("请选择或填写模型")
        from translator.engine import ensure_model
        ensure_model({**self.cfg, "model": model}, interactive=False)
        with self.lock:
            if self.queue_paused:
                if _restore_id is not None or body.get("_queue_id"):
                    return
                raise ValueError("\u961f\u5217\u5df2\u6682\u505c\uff0c\u8bf7\u5148\u70b9\u51fb\u7ee7\u7eed\u961f\u5217")
            if self.closing.is_set() or (_restore_id is None and not self.queue_matches(body)) or _restore_id is not None and not self.recovery.matches("translation", _restore_id):
                return
            if self.restart_requested:
                raise ValueError("服务正在等待更新重启，请稍后开始任务")
            if self.task["running"]:
                raise ValueError("已有任务正在运行")
            if url and self._browser_owner is not None:
                raise ValueError("已有作品正在获取，请等待获取完成")
            if self.restart_requested:
                raise ValueError("服务正在等待更新重启，请稍后开始任务")
            resume_folder = self.project(body["_resume_project"]) if body.get("_resume_project") else None
            update_config({"model": model})
            self.cfg["model"] = model
            history = load_json(self.history_path, [])
            item = {"url": url, "source": source, "language": language}
            identity = url or source
            atomic_json(self.history_path, [item] + [x for x in history if (x.get("url") or x.get("source")) != identity][:19])
            job_id = self.recovery.begin("translation", {**item, "model": model,
                **{key: body[key] for key in ("_queue_id", "_resume_project", "title") if body.get(key)},
                "_book_identity": self.queue_identity("translation", body)[0]})
            self.consume_queue(body)
            self.cancel.clear()
            self._stage_context.clear()
            self.task = {"running": True, "percent": 0, "message": "正在启动…", "output": "",
                         "stages": {"acquisition": "等待获取" if url else "本地 EPUB，无需获取",
                                    "translation": "等待原文", "glossary": "等待译文"},
                         "url": url, "source": source, "language": language,
                         "title": body.get("title") or Path(source).stem or "等待获取书名", "job_id": job_id}
            cfg = dict(self.cfg)
            if resume_folder is not None:
                cfg["_resume_work_dir"] = str(resume_folder)
            browser_owner = object() if url else None
            if browser_owner is not None:
                self._browser_owner = browser_owner
        cfg.update(_translation_cancelled=self.cancel.is_set, _capture_first_terms=True,
                   _automatic_browser_login=True,
                   _acquisition_finished=lambda: self._release_browser(browser_owner),
                   _translation_activity=self.model_activity, _task_stage=self.stage,
                   _translation_project_ready=self.project_ready)
        def worker():
            from translator.engine import translate_epub_language, translation_work_dir, TranslationCancelled
            try:
                actual = Path(source)
                if url:
                    from translator.translation.streaming_workflow import run_streaming_workflow
                    actual, output, work_dir = run_streaming_workflow(url, cfg, language,
                        progress=self.progress, counts=self.pipeline_counts)
                else:
                    work_dir = translation_work_dir(actual, cfg, language)
                    self.progress(0, "开始翻译，默认保存首次译名…")
                    output = translate_epub_language(actual, cfg, language, self.progress)
                from translator.storage.group_backups import checkpoint
                checkpoint(work_dir, force=True, reason="completed")
                with self.lock:
                    self.task.update(percent=100, message="翻译完成", output=str(output),
                                     project=str(work_dir.relative_to(Path(cfg["output_dir"]))))
                    self.task["stages"].update(acquisition="获取完成" if url else "本地 EPUB，无需获取",
                                              translation="翻译完成", glossary="术语整理完成")
                    self.finish_queue_task("translation", body, body.get("title") or actual.stem, self.task["project"])
                    self.recovery.clear("translation", job_id)
            except TranslationCancelled:
                self.progress(None, "已暂停，已保存章节可续译")
            except Exception as exc:
                self.record_task_failure("translation", {**item, "model": model,
                    "_book_identity": self.queue_identity("translation", body)[0],
                    **{key: body[key] for key in ("_queue_id", "_resume_project", "title") if body.get(key)}}, exc)
                if not self.cancel.is_set():
                    self.recovery.clear("translation", job_id)
            finally:
                with self.lock:
                    self._release_browser(browser_owner)
                    self.task["running"] = False
        threading.Thread(target=worker, daemon=True).start()

    def progress(self, percent, message):
        with self.lock:
            self.task["message"] = message
            if percent is not None:
                self.task["percent"] = round(percent, 1)

    def stage(self, name, message):
        with self.lock:
            self.task.setdefault("stages", {})[name] = message
            self._stage_context[name] = message
            if name in ("translation", "glossary"):
                self._model_stage = name

    def model_activity(self, message):
        with self.lock:
            name = getattr(self, "_model_stage", "translation")
            context = self._stage_context.get(name, "")
            details = message.strip(" ·")
            self.task.setdefault("stages", {})[name] = (
                details if not context or context in details else f"{context} · {details}")

    def term_revision(self, path):
        glossary = path / "glossary.json"
        return hashlib.sha256(glossary.read_bytes() if glossary.exists() else b"").hexdigest()

    def term_examples(self, path, source):
        if not source or len(source) > 500:
            raise ValueError("请提供有效术语")
        manifest = load_json(path / "translation-project.json", {})
        if manifest.get("schema_version") == 2:
            chapters = (load_json(path / "chapters" / f"{index:06d}.json", {})
                        for index in range(1, manifest.get("chapter_count", 0) + 1))
        else:
            chapters = iter(manifest.get("chapters", []))
        examples = []
        has_originals = False
        for index, chapter in enumerate(chapters, 1):
            originals = chapter.get("source_paragraphs", chapter.get("japanese", []))
            has_originals = has_originals or bool(originals)
            for original, translation in zip(originals,
                                             chapter.get("paragraphs", chapter.get("translation", []))):
                if source in original:
                    examples.append({"chapter": chapter.get("title", str(index)), "original": original,
                                     "translation": translation})
                    if len(examples) >= 2:
                        return {"examples": examples}
        if examples:
            return {"examples": examples}
        return {"examples": [], "reason": "term_not_found" if has_originals else "no_originals"}

    def pipeline_counts(self, counts):
        with self.lock:
            self.task["counts"] = counts

    def project_ready(self, path):
        with self.lock:
            self._discovery_until = 0
            self.task["project"] = str(path.relative_to(Path(self.cfg["output_dir"])))

    def write_terms(self, body):
        with self.lock:
            if self.restart_requested:
                raise ValueError("服务正在等待更新重启，请稍后保存术语，或取消等待重启")
            if self.task["running"]:
                raise ValueError("请停止翻译后修改术语，重新开始会使用更新的译名")
            path = self.project(body["project"])
            if not isinstance(body.get("entries"), list):
                raise ValueError("术语文件必须包含 entries 数组")
            if any(not isinstance(e, dict) or not isinstance(e.get("source"), str)
                   or not isinstance(e.get("target"), str)
                   or not e["source"].strip() or not e["target"].strip() for e in body["entries"]):
                raise ValueError("每条术语必须包含非空的 source 和 target")
            previous = load_project_glossary(path)
            from translator.storage.group_backups import checkpoint
            checkpoint(path, force=True, reason="before_glossary_edit")
            entries = import_from_json_list(body["entries"])
            if any(not e.source or not e.target for e in entries):
                raise ValueError("原词和译名不能为空")
            if body.get("merge"):
                entries = merge_glossaries(previous, entries, overwrite=True)
            from translator.ui.web_review import remember_term_changes
            remember_term_changes(path, previous, entries)
            save_project_glossary(path, entries)
            return {"saved": len(entries)}

    def review(self, body):
        from translator.ui.web_review import review_project
        with self.lock:
            if self.restart_requested:
                raise ValueError("服务正在等待更新重启，请稍后检查译文")
            if self.task["running"]:
                raise ValueError("请先停止当前任务再检查译文")
            path = self.project(body["project"])
            result = review_project(path, self.cfg, body.get("mode", "all"))
            result["term_revision"] = self.term_revision(path)
            return result

    def retranslate(self, body):
        from translator.engine import ensure_model, retranslate_project, TranslationCancelled
        selected = body.get("items")
        if not isinstance(selected, list) or not selected or len(selected) > 5000:
            raise ValueError("请选择 1 至 5000 个段落")
        if any(not isinstance(item, dict) or not all(key in item for key in (
                "chapter_index", "paragraph", "original", "translation")) for item in selected):
            raise ValueError("段落信息无效，请重新检查")
        locations = [(item["chapter_index"], item["paragraph"], item["original"], item["translation"])
                     for item in selected]
        with self.lock:
            if self.restart_requested:
                raise ValueError("服务正在等待更新重启，请稍后开始任务")
            if self.task["running"]:
                raise ValueError("已有任务正在运行")
            path = self.project(body["project"])
            if body.get("term_revision") != self.term_revision(path):
                raise ValueError("术语已变化，请重新检查后重译")
            model = str(body.get("model", "")).strip()
            if not model:
                raise ValueError("请选择已安装模型")
            cfg = {**self.cfg, "model": model, "_translation_cancelled": self.cancel.is_set,
                   "_translation_activity": self.model_activity, "_task_stage": self.stage}
            ensure_model(cfg, interactive=False)
            self.cancel.clear()
            self._stage_context.clear()
            self._model_stage = "translation"
            self.task = {"running": True, "percent": 0, "message": "正在重译选中段落…",
                         "project": body["project"], "kind": "review", "output": "",
                         "stages": {"translation": "正在重译选中段落"}}

        def worker():
            try:
                def progress(percent, title):
                    self.stage("translation", f"{title} · 局部重译")
                    self.progress(percent, f"{title} · 已保存")
                output = retranslate_project(path, cfg, [], progress, paragraph_locations=locations)
                with self.lock:
                    self.task.update(percent=100, message="局部重译完成，EPUB 已更新", output=str(output))
            except TranslationCancelled:
                with self.lock:
                    self.task["message"] = "已停止局部重译，已完成章节保留；重新检查后可继续"
            except Exception as exc:
                with self.lock:
                    self.task["message"] = f"局部重译失败：{exc}"
            finally:
                with self.lock:
                    self.task["running"] = False
        threading.Thread(target=worker, daemon=True).start()

    def _release_browser(self, owner):
        with self.lock:
            if owner is not None and self._browser_owner is owner:
                self._browser_owner = None

    def update_status(self):
        result = self.updates.snapshot()
        with self.lock:
            return {**result, "restart_supported": self.restart_callback is not None,
                    "restart_pending": self.restart_requested, "error": self.restart_error,
                    "busy": bool(self.task["running"] or self.acquisition_task["running"] or self._browser_owner)}

    def request_restart(self, mode):
        if mode not in ("now", "wait", "stop"):
            raise ValueError("请选择立即重启、等待任务结束或停止后重启")
        with self.lock:
            if self.restart_callback is None:
                raise ValueError("当前启动方式不支持页面重启，请使用 web_app.py 启动")
            if self.restart_requested:
                raise ValueError("已经安排更新重启")
        self.updates.validate_backend()
        with self.lock:
            if self.restart_requested:
                raise ValueError("已经安排更新重启")
            busy = self.task["running"] or self.acquisition_task["running"] or self._browser_owner
            if mode == "now" and busy:
                raise ValueError("任务仍在运行，请等待结束或选择停止后重启")
            self.restart_requested = True
            self.restart_error = ""
            self._restart_committed = False
            self._restart_generation += 1
            generation = self._restart_generation
            if mode == "stop":
                self.cancel.set()
                self.acquisition_cancel.set()
                for task in (self.task, self.acquisition_task):
                    if task["running"]:
                        task["stopping"] = True

        def worker():
            try:
                while True:
                    threading.Event().wait(.2)
                    with self.lock:
                        if generation != self._restart_generation or not self.restart_requested:
                            return
                        if self.task["running"] or self.acquisition_task["running"] or self._browser_owner:
                            continue
                    # Files may have changed while a long-running task finished.
                    self.updates.validate_backend()
                    with self.lock:
                        if generation != self._restart_generation or not self.restart_requested:
                            return
                        self._restart_committed = True
                    self.restart_callback()
                    return
            except Exception as exc:
                with self.lock:
                    if generation == self._restart_generation:
                        self.restart_requested = False
                        self.restart_error = f"更新重启失败，当前服务保留：{exc}"
        threading.Thread(target=worker, daemon=True).start()

    def cancel_restart(self):
        with self.lock:
            if self._restart_committed:
                raise ValueError("重启已开始，请等待重新连接")
            self._restart_generation += 1
            self.restart_requested = False

    def acquire(self, body, *, _restore_id=None):
        from translator.acquisition.source_epub import normalize_work_url
        from translator.acquisition.network_workflow import acquire_source
        from translator.engine import TranslationCancelled
        url = normalize_work_url(str(body.get("url", "")).strip())
        with self.lock:
            if self.queue_paused:
                if _restore_id is not None or body.get("_queue_id"):
                    return
                raise ValueError("\u961f\u5217\u5df2\u6682\u505c\uff0c\u8bf7\u5148\u70b9\u51fb\u7ee7\u7eed\u961f\u5217")
            if self.closing.is_set() or (_restore_id is None and not self.queue_matches(body)) or _restore_id is not None and not self.recovery.matches("acquisition", _restore_id):
                return
            if self.restart_requested:
                raise ValueError("服务正在等待更新重启，请稍后开始任务")
            if self.acquisition_task["running"] or self._browser_owner is not None:
                raise ValueError("已有作品正在获取，请等待完成或停止获取")
            if self.task["running"] and self.task.get("url") == url:
                raise ValueError("该作品正在翻译，请选择其他作品")
            recovery_body = {"url": url, **{key: body[key] for key in
                ("_queue_id", "updates_only", "known_chapters", "translate_after", "followup_key", "title") if key in body}}
            job_id = self.recovery.begin("acquisition", recovery_body)
            self.consume_queue(body)
            owner = object()
            self._browser_owner = owner
            self.acquisition_cancel.clear()
            self.acquisition_task = {"running": True, "percent": 0, "message": "正在获取目录…", "url": url,
                                     "title": body.get("title") or "等待获取书名", "job_id": job_id}
            cfg = {**self.cfg, "_translation_cancelled": self.acquisition_cancel.is_set,
                   "_automatic_browser_login": True}
            self._discovery_until = 0
        def progress(percent, message):
            with self.lock:
                self.acquisition_task.update(percent=round(percent, 1), message=message)
        def worker():
            try:
                options = {"updates_only": True} if body.get("updates_only") else {}
                source, work = acquire_source(url, cfg, 1, full=True, progress=progress, **options)
                with self.lock:
                    added = max(0, len(work.episodes) - int(body.get("known_chapters", 0))) if body.get("updates_only") else 0
                    if added and body.get("translate_after"):
                        self.enqueue("translation", {**body["translate_after"], "source": str(source),
                            "title": work.title, "dedupe_key": body["followup_key"]}, _internal=True)
                    self.acquisition_task.update(percent=100, message=f"{work.title} · 获取完成，可下载原文",
                                                 source=str(source), title=work.title)
                    if body.get("updates_only"):
                        self.acquisition_task.update(new_chapters=added, message=
                            f"{work.title} · 新增 {added} 章" + ("，已加入翻译队列" if added and body.get("translate_after") else ""))
                    history = load_json(self.history_path, [])
                    item = {"url": url, "source": "", "title": work.title, "language": "zh-Hans"}
                    atomic_json(self.history_path, [item] + [entry for entry in history if entry.get("url") != url][:19])
                    self.finish_queue_task("acquisition", body, work.title)
                    self.recovery.clear("acquisition", job_id)
            except TranslationCancelled:
                progress(self.acquisition_task["percent"], "获取已暂停，已获取章节保留")
            except Exception as exc:
                self.record_task_failure("acquisition", recovery_body, exc)
                if not self.acquisition_cancel.is_set():
                    self.recovery.clear("acquisition", job_id)
            finally:
                with self.lock:
                    self._release_browser(owner)
                    self.acquisition_task["running"] = False
                    self._discovery_until = 0
        threading.Thread(target=worker, daemon=True).start()


def create_server(app, port=0, *, session_token=None):
    token = session_token or secrets.token_urlsafe(24)
    prefix = "/" + token

    class Handler(BaseHTTPRequestHandler):
        def send(self, value, mime="application/json; charset=utf-8", status=200, attachment=None):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlsplit(self.path)
            route = parsed.path.removeprefix(prefix)
            if not parsed.path.startswith(prefix + "/"):
                self.send({"error": "Not found"}, status=404)
                return
            query = parse_qs(parsed.query)
            try:
                if route == "/":
                    page = (WEB_DIR / "web_app.html").read_text(encoding="utf-8")
                    snapshot = app.updates.snapshot(force=True)
                    page = page.replace("{{FRONTEND_REVISION}}", snapshot["frontend_revision"])
                    page = page.replace("{{SERVER_INSTANCE}}", snapshot["instance"])
                    self.send(page.replace("{{APP_VERSION}}", VERSION).encode("utf-8"), "text/html; charset=utf-8")
                elif route == "/app.js":
                    self.send((WEB_DIR / "web_app.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route == "/review.js":
                    self.send((WEB_DIR / "web_review.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route == "/updates.js":
                    self.send((WEB_DIR / "web_updates.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route == "/tasks.js":
                    self.send((WEB_DIR / "web_tasks.js").read_bytes(), "text/javascript; charset=utf-8")
                elif route == "/api/updates":
                    self.send(app.update_status())
                elif route == "/api/status":
                    self.send(app.status())
                elif route == "/api/models":
                    self.send(app.models())
                elif route in ("/api/terms", "/api/export"):
                    path = app.project(query.get("project", [""])[0])
                    revision = app.term_revision(path)
                    result = ({"unchanged": True, "revision": revision}
                              if route == "/api/terms" and query.get("revision", [""])[0] == revision else
                              {"entries": [asdict(e) for e in load_project_glossary(path)], "revision": revision})
                    self.send(result, attachment="glossary.json" if route.endswith("export") else None)
                elif route == "/api/examples":
                    self.send(app.term_examples(app.project(query.get("project", [""])[0]),
                                                query.get("source", [""])[0]))
                elif route == "/api/download":
                    root = Path(app.cfg["output_dir"]).resolve()
                    path = (root / query.get("file", [""])[0]).resolve()
                    if not path.is_relative_to(root) or path.suffix.lower() != ".epub" or not path.is_file():
                        raise ValueError("EPUB 不存在")
                    self.send(path.read_bytes(), "application/epub+zip", attachment=(
                        "original.epub" if path.parent.parent.name == "network-workflows" else "translation.epub"))
                else:
                    self.send({"error": "Not found"}, status=404)
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                self.send({"error": str(exc)}, status=400)

        def do_POST(self):
            route = urlsplit(self.path).path
            if not route.startswith(prefix + "/api/") or self.headers.get("X-Local-App") != token:
                self.send({"error": "Forbidden"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length < 0 or length > 32 * 1024 * 1024:
                    raise ValueError("文件过大（最多 32 MB）")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求必须是对象")
                if route.endswith("/enqueue"):
                    result = app.enqueue(body.get("slot"), body.get("task", {}))
                elif route.endswith("/queue-control"):
                    result = app.control_queue(body.get("paused"))
                elif route.endswith("/cancel-recovery"):
                    app.stop_task(body.get("slot"))
                    result = {"cancelled": True}
                elif route.endswith("/choose-source"):
                    from translator.ui.file_dialogs import choose_web_source
                    result = choose_web_source()
                elif route.endswith("/create-group-backup"):
                    result = app.create_group_backup(body)
                elif route.endswith("/restore-group-backup"):
                    result = app.restore_group_backup(body)
                elif route.endswith("/check-book-updates"):
                    result = app.check_book_updates(body)
                elif route.endswith("/diagnostics"):
                    result = app.diagnostics()
                elif route.endswith("/queue-action"):
                    result = app.change_queue(body)
                elif route.endswith("/retry-recovery"):
                    result = app.retry_recovery(body.get("slot"), body.get("settings"))
                elif route.endswith("/backups"):
                    result = app.backups(body)
                elif route.endswith("/restore-backup"):
                    result = app.restore_backup(body)
                elif route.endswith("/start"):
                    app.start(body)
                    result = {"started": True}
                elif route.endswith("/pause-task"):
                    result = app.pause_task(body.get("slot"), body.get("job_id"))
                elif route.endswith("/stop"):
                    app.stop_task("translation")
                    result = {"stopping": True}
                elif route.endswith("/resume"):
                    project = next((p for p in app.projects() if p["id"] == body.get("project")), None)
                    if not project or not project["resume"]:
                        raise ValueError("无法定位原文，请重新选择原文 EPUB 或小说网址")
                    settings = {**project["resume"], "model": body.get("model", app.cfg["model"])}
                    if project.get("kind") == "source" and body.get("language"):
                        settings["language"] = body["language"]
                    result = app.enqueue("translation", {**settings, "title": project["title"]})
                elif route.endswith("/terms"):
                    result = app.write_terms(body)
                elif route.endswith("/review"):
                    result = app.review(body)
                elif route.endswith("/retranslate"):
                    app.retranslate(body)
                    result = {"started": True}
                elif route.endswith("/restart"):
                    app.request_restart(body.get("mode", "now"))
                    result = {"restart_pending": True}
                elif route.endswith("/cancel-restart"):
                    app.cancel_restart()
                    result = {"restart_pending": False}
                elif route.endswith("/acquire"):
                    app.acquire(body)
                    result = {"started": True}
                elif route.endswith("/stop-acquisition"):
                    app.stop_task("acquisition")
                    result = {"stopping": True}
                elif route.endswith("/read"):
                    from translator.ui.web_reader import ReadingServer
                    with app.lock:
                        if app.reader is None:
                            app.reader = ReadingServer()
                        reader = app.reader
                    result = {"url": reader.open_path(
                        app.project(body["project"]) / "translation-project.json")}
                else:
                    self.send({"error": "Not found"}, status=404)
                    return
                self.send(result)
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                self.send({"error": str(exc)}, status=400)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.url = f"http://127.0.0.1:{server.server_port}{prefix}/"
    return server


def serve(args):
    app = Application()
    server = create_server(app, args.port, session_token=args.session_token)
    restarting = threading.Event()
    def restart():
        restarting.set()
        server.shutdown()
    app.restart_callback = restart
    if args.supervised:
        parent_pipe = sys.stdin.fileno()
        def watch_parent():
            try:
                # Avoid holding stdin's buffered-I/O lock during interpreter exit.
                while os.read(parent_pipe, 1):
                    pass
            finally:
                app.closing.set()
                app.cancel.set()
                app.acquisition_cancel.set()
                server.shutdown()
        threading.Thread(target=watch_parent, daemon=True).start()
    print(f"小说翻译 v{VERSION}", flush=True)
    print("本地翻译页面：" + server.url, flush=True)
    app.restore_tasks()
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        app.closing.set()
        app.cancel.set()
        app.acquisition_cancel.set()
        server.server_close()
        if app.reader:
            app.reader.close()
    return 75 if restarting.is_set() else 0


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--session-token", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return serve(args)
    if not args.port:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            args.port = reservation.getsockname()[1]
    token = secrets.token_urlsafe(24)
    child = None
    try:
        while True:
            command = [sys.executable, "-X", "utf8", "-B", "-X", f"pycache_prefix={APP_DIR / '__pycache__' / secrets.token_hex(12)}",
                       "-m", "translator.ui.web_app", "--worker", "--supervised", "--port", str(args.port), "--session-token", token]
            if args.no_browser:
                command.append("--no-browser")
            child = subprocess.Popen(command, cwd=APP_DIR, stdin=subprocess.PIPE)
            try:
                result = child.wait()
            finally:
                if child.poll() is not None:
                    child.stdin.close()
            if result != 75:
                return result
            args.no_browser = True
            print("正在更新重启，页面地址保持不变…", flush=True)
    except KeyboardInterrupt:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
            child.stdin.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
