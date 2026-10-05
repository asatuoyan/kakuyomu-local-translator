"""Two-page local web UI sharing the existing acquisition and translation engine."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from app_config import load_config, update_config
from glossary_manager import (import_from_json_list,
                              load_project_glossary, merge_glossaries, save_project_glossary)
from project_storage import atomic_json, load_json


class Application:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.task = {"running": False, "percent": 0, "message": "准备就绪"}
        self.reader = None
        self.history_path = Path(self.cfg["output_dir"]) / "web-tasks.json"

    def projects(self):
        from main import project_exists
        result = []
        root = Path(self.cfg["output_dir"])
        for path in sorted(root.rglob("translation-project.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
            folder = path.parent
            if project_exists(folder):
                try:
                    project = load_json(path, {})
                    result.append({"id": str(folder.relative_to(root)),
                                   "title": project.get("metadata", {}).get("title", folder.name),
                                   "language": project.get("language", ""),
                                   "term_count": len(load_project_glossary(folder)),
                                   "chapters": project.get("chapter_count", len(project.get("chapters", [])))})
                except (OSError, ValueError, KeyError):
                    continue
        return result

    def project(self, identity):
        root = Path(self.cfg["output_dir"]).resolve()
        path = (root / identity).resolve()
        if not path.is_relative_to(root) or not (path / "translation-project.json").is_file():
            raise ValueError("作品不存在")
        return path

    def status(self):
        with self.lock:
            return {"task": dict(self.task), "projects": self.projects(),
                    "history": load_json(self.history_path, []),
                    "model": self.cfg["model"], "ollama_url": self.cfg["ollama_url"]}

    def models(self):
        from main import installed_models, model_is_installed
        names = sorted(name for name in installed_models(self.cfg) if name)
        preferred = self.cfg["model"]
        selected = next((name for name in names if name == preferred), "")
        if not selected and model_is_installed(preferred, set(names)):
            selected = next(name for name in names if name.split(":", 1)[0] == preferred)
        return {"models": names, "selected": selected or (names[0] if names else ""),
                "ollama_url": self.cfg["ollama_url"]}

    def start(self, body):
        from source_epub import normalize_work_url
        from languages import language_code
        with self.lock:
            if self.task["running"]:
                raise ValueError("已有任务正在运行")
        url = str(body.get("url", "")).strip()
        source = str(body.get("source", "")).strip()
        if url:
            url = normalize_work_url(url)
        elif not source or not Path(source).is_file() or Path(source).suffix.lower() != ".epub":
            raise ValueError("请输入小说网址或有效的本地 EPUB 路径")
        language = language_code(body.get("language", "zh-Hans"))
        model = str(body.get("model", self.cfg["model"])).strip()
        if not model:
            raise ValueError("请选择或填写模型")
        from main import ensure_model
        ensure_model({**self.cfg, "model": model}, interactive=False)
        with self.lock:
            if self.task["running"]:
                raise ValueError("已有任务正在运行")
            update_config({"model": model})
            self.cfg["model"] = model
            self.cancel.clear()
            self.task = {"running": True, "percent": 0, "message": "正在启动…", "output": "",
                         "url": url, "source": source, "language": language}
            history = load_json(self.history_path, [])
            item = {"url": url, "source": source, "language": language}
            atomic_json(self.history_path, [item] + [x for x in history if x != item][:19])
            cfg = dict(self.cfg)
        cfg.update(_translation_cancelled=self.cancel.is_set, _capture_first_terms=True,
                   _translation_activity=lambda msg: self.progress(None, msg),
                   _translation_project_ready=self.project_ready)
        def worker():
            from main import translate_epub_language, translation_work_dir, TranslationCancelled
            try:
                actual = Path(source)
                if url:
                    from streaming_workflow import run_streaming_workflow
                    actual, output, work_dir = run_streaming_workflow(url, cfg, language,
                        progress=self.progress, counts=self.pipeline_counts)
                else:
                    work_dir = translation_work_dir(actual, cfg, language)
                    self.progress(0, "开始翻译，默认保存首次译名…")
                    output = translate_epub_language(actual, cfg, language, self.progress)
                with self.lock:
                    self.task.update(percent=100, message="翻译完成", output=str(output),
                                     project=str(work_dir.relative_to(Path(cfg["output_dir"]))))
            except TranslationCancelled:
                self.progress(None, "已停止，已保存章节可续译")
            except Exception as exc:
                self.progress(None, f"任务失败：{exc}")
            finally:
                with self.lock:
                    self.task["running"] = False
        threading.Thread(target=worker, daemon=True).start()

    def progress(self, percent, message):
        with self.lock:
            self.task["message"] = message
            if percent is not None:
                self.task["percent"] = round(percent, 1)

    def pipeline_counts(self, counts):
        with self.lock:
            self.task["counts"] = counts

    def project_ready(self, path):
        with self.lock:
            self.task["project"] = str(path.relative_to(Path(self.cfg["output_dir"])))

    def write_terms(self, body):
        with self.lock:
            if self.task["running"]:
                raise ValueError("请停止翻译后修改术语，重新开始会使用更新的译名")
            path = self.project(body["project"])
            if not isinstance(body.get("entries"), list):
                raise ValueError("术语文件必须包含 entries 数组")
            if any(not isinstance(e, dict) or not isinstance(e.get("source"), str)
                   or not isinstance(e.get("target"), str)
                   or not e["source"].strip() or not e["target"].strip() for e in body["entries"]):
                raise ValueError("每条术语必须包含非空的 source 和 target")
            entries = import_from_json_list(body["entries"])
            if any(not e.source or not e.target for e in entries):
                raise ValueError("原词和译名不能为空")
            if body.get("merge"):
                entries = merge_glossaries(load_project_glossary(path), entries, overwrite=True)
            save_project_glossary(path, entries)
            return {"saved": len(entries)}


def create_server(app, port=0):
    token = secrets.token_urlsafe(24)
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
                    self.send(Path(__file__).with_name("web_app.html").read_bytes(), "text/html; charset=utf-8")
                elif route == "/api/status":
                    self.send(app.status())
                elif route == "/api/models":
                    self.send(app.models())
                elif route in ("/api/terms", "/api/export"):
                    path = app.project(query.get("project", [""])[0])
                    result = {"entries": [asdict(e) for e in load_project_glossary(path)]}
                    self.send(result, attachment="glossary.json" if route.endswith("export") else None)
                elif route == "/api/download":
                    root = Path(app.cfg["output_dir"]).resolve()
                    path = (root / query.get("file", [""])[0]).resolve()
                    if not path.is_relative_to(root) or path.suffix.lower() != ".epub" or not path.is_file():
                        raise ValueError("EPUB 不存在")
                    self.send(path.read_bytes(), "application/epub+zip", attachment="translation.epub")
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
                if route.endswith("/start"):
                    app.start(body)
                    result = {"started": True}
                elif route.endswith("/stop"):
                    app.cancel.set()
                    result = {"stopping": True}
                elif route.endswith("/terms"):
                    result = app.write_terms(body)
                elif route.endswith("/read"):
                    from saved_reading import SavedReading
                    from web_reader import ReadingServer
                    with app.lock:
                        if app.reader is None:
                            app.reader = ReadingServer()
                        app.reader.open_saved(SavedReading(app.project(body["project"]) / "translation-project.json"))
                        result = {"url": app.reader.url()}
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    app = Application()
    server = create_server(app, args.port)
    print("本地翻译页面：" + server.url, flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.cancel.set()
        server.server_close()
        if app.reader:
            app.reader.close()


if __name__ == "__main__":
    main()
