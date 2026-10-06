"""Task recovery lifecycle and cancellation."""
import threading
from translator.ui.task_errors import task_error


class RecoveryMixin:
    def restore_tasks(self):
        """Start saved jobs once; defer browser acquisition until its lease is free."""
        with self.lock:
            if self._recovery_started:
                return
            if self.queue_paused:
                self.start_queue()
                return
            self._recovery_started = True
            pending = {slot: saved for slot, saved in self.recovery.snapshot().items()
                       if not (self.task if slot == "translation" else self.acquisition_task).get("running")
                       and not (self.task if slot == "translation" else self.acquisition_task).get("recovering")}
            self.start_queue()
            for slot in pending:
                task = self.task if slot == "translation" else self.acquisition_task
                body = pending[slot]["body"]
                task.update(recovering=True, job_id=pending[slot]["id"], url=body.get("url", ""), source=body.get("source", ""),
                            model=body.get("model", ""), language=body.get("language", ""), message="正在自动恢复上次任务…")
            if self.recovery.error:
                self.task["message"] = "无法读取自动恢复记录，请手动继续翻译：" + self.recovery.error

        def worker():
            while pending and not self.closing.is_set():
                for slot, saved in list(pending.items()):
                    with self.lock:
                        task = self.task if slot == "translation" else self.acquisition_task
                        if not self.recovery.matches(slot, saved["id"]):
                            task.pop("recovering", None)
                            pending.pop(slot)
                            continue
                        if self.restart_requested or self.queue_paused:
                            continue
                        if slot == "acquisition" and self._browser_owner is not None:
                            task["message"] = "等待浏览器空闲后自动恢复获取…"
                            continue
                    try:
                        operation = self.start if slot == "translation" else self.acquire
                        operation(saved["body"], _restore_id=saved["id"])
                        with self.lock:
                            if self.queue_paused and self.recovery.matches(slot, saved["id"]):
                                continue
                    except Exception as exc:
                        with self.lock:
                            task = self.task if slot == "translation" else self.acquisition_task
                            if self.recovery.matches(slot, saved["id"]):
                                error = task_error(exc)
                                self.diagnostic_event(slot, error)
                                task.update(recovery_error=error, retry_available=True)
                                if isinstance(exc, ValueError) and self._browser_owner is not None:
                                    task["message"] = "等待浏览器空闲后自动恢复任务…"
                                    continue
                                identity = self.hold_task(slot, "failed", error)
                                task["retry_body"] = {**saved["body"], "_queue_id": identity}
                                task.update(recovering=False, message=f"自动恢复失败：{error['advice']} {exc}")
                    pending.pop(slot)
                self.closing.wait(.2)
        self._recovery_thread = threading.Thread(target=worker, daemon=True)
        self._recovery_thread.start()

    def stop_task(self, slot):
        if slot not in ("translation", "acquisition"):
            raise ValueError("无效的任务类型")
        with self.lock:
            had_recovery = slot in self.recovery.snapshot()
            self.recovery.clear(slot)
            task = self.task if slot == "translation" else self.acquisition_task
            identity = task.get("retry_body", {}).get("_queue_id")
            if identity:
                self._save_queue([item for item in self.queued_tasks if item["id"] != identity])
            task.pop("retry_available", None)
            task.pop("recovery_error", None)
            task.pop("retry_body", None)
            cancel = self.cancel if slot == "translation" else self.acquisition_cancel
            task = self.task if slot == "translation" else self.acquisition_task
            cancel.set()
            if task["running"]:
                task["stopping"] = True
            elif task.pop("recovering", False) or had_recovery:
                task["message"] = "已取消自动恢复，保存进度保留"

    def recovery_settings(self, slot, original, settings):
        from pathlib import Path
        from translator.languages import language_code
        from translator.acquisition.source_epub import normalize_work_url
        if not isinstance(settings, dict):
            raise ValueError("恢复参数必须是对象")
        body = {**original}
        if slot == "translation":
            if "source" in settings:
                source = Path(str(settings["source"]).strip()).resolve()
                if not source.is_file() or source.suffix.lower() != ".epub":
                    raise ValueError("请选择有效的本地 EPUB 文件")
                body.update(source=str(source), url="")
                if original.get("source") and not body.get("_resume_project"):
                    for project in self.projects():
                        if project.get("kind") == "source":
                            continue
                        path = self.project(project["id"]) / "translation-project.json"
                        state = self._cached_json(path, {})
                        if state.get("source_path") == original["source"] and language_code(state.get("language", "")) == language_code(original.get("language", "zh-Hans")):
                            body["_resume_project"] = project["id"]
                            break
            if "model" in settings:
                if not isinstance(settings["model"], str) or not settings["model"].strip():
                    raise ValueError("请选择模型")
                body["model"] = settings["model"].strip()
            if "language" in settings:
                body["language"] = language_code(settings["language"])
        elif "url" in settings:
            body["url"] = normalize_work_url(str(settings["url"]).strip())
        return body

    def retry_recovery(self, slot, settings=None):
        if slot not in ("translation", "acquisition"):
            raise ValueError("无效的任务类型")
        with self.lock:
            task = self.task if slot == "translation" else self.acquisition_task
            if task.get("running") or task.get("recovering"):
                raise ValueError("任务正在运行或等待自动恢复")
            saved = self.recovery.snapshot().get(slot)
            body = saved["body"] if saved else task.get("retry_body")
            if not body:
                raise ValueError("没有可重试的任务记录")
            body = self.recovery_settings(slot, body, settings or {})
            queued = next((item for item in self.queued_tasks if item["id"] == body.get("_queue_id")), None)
            if queued:
                self.change_queue({"id": queued["id"], "action": "retry", "settings": settings or {}})
                task.pop("retry_available", None)
                return {"queued": True}
            if not saved or settings:
                self.recovery.begin(slot, body)
            task.pop("retry_available", None)
            task.pop("recovery_error", None)
            task["recovering"] = False
            self._recovery_started = False
            self.restore_tasks()
            if self.queue_paused:
                task["message"] = "恢复设置已保存，点击继续队列后执行"
        return {"recovering": True}

    def record_task_failure(self, slot, body, exc):
        with self.lock:
            task = self.task if slot == "translation" else self.acquisition_task
            if (self.cancel if slot == "translation" else self.acquisition_cancel).is_set():
                task.update(retry_available=False, message="已保存进度，等待继续队列或自动恢复")
                return
            error = task_error(exc)
            self.diagnostic_event(slot, error)
            task.update(recovery_error=error, retry_available=True, retry_body=body,
                        message=f"任务失败：{error['advice']} {error['detail']}")
            if body.get("_queue_id") and not (self.cancel if slot == "translation" else self.acquisition_cancel).is_set():
                identity = body["_queue_id"]
                if not any(entry["id"] == identity for entry in self.queued_tasks):
                    from pathlib import Path
                    settings = {key: value for key, value in body.items() if key != "_queue_id"}
                    self._save_queue([*self.queued_tasks, {"id": identity, "slot": slot, "body": settings,
                        "title": settings.get("title") or Path(settings.get("source", "")).stem or "等待获取书名",
                        "status": "failed", "error": error}])
                task["retry_available"] = False  # This job retries through its durable queue row.
