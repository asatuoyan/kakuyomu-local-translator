"""Queue incremental source checks and durable follow-up translation."""
import secrets

from translator.acquisition.network_workflow import network_path
from translator.storage.project_storage import load_json


class BookUpdatesMixin:
    def check_book_updates(self, body):
        with self.lock:
            book = next((item for item in self.projects() if item["id"] == body.get("project")), None)
            if not book or not book.get("update_url") or not book.get("source_download"):
                raise ValueError("该作品没有已获取完成的网络原文，无法检查原站更新")
            url = book["update_url"]
            for item in self.queued_tasks:
                if item["slot"] == "acquisition" and item["body"].get("url") == url and item["body"].get("updates_only"):
                    raise ValueError("该作品已在更新检查队列中")
            if self.acquisition_task.get("running") and self.acquisition_task.get("url") == url:
                raise ValueError("该作品正在获取或检查更新")
            saved = load_json(network_path(url, self.cfg), {})
            settings = {"url": url, "title": book["title"], "updates_only": True,
                        "known_chapters": saved.get("acquired_chapters", 0),
                        "followup_key": secrets.token_hex(16)}
            if body.get("translate", True):
                from translator.languages import language_code
                model = str(body.get("model", self.cfg["model"])).strip()
                if not model:
                    raise ValueError("请选择新增章节的翻译模型")
                settings["translate_after"] = {"model": model,
                    "language": language_code(body.get("language", book.get("language") if book.get("kind") != "source" else "zh-Hans") or "zh-Hans")}
            return self.enqueue("acquisition", settings)
