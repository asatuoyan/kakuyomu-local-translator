"""Durable, independent FIFO queues for acquisition and translation."""
from copy import deepcopy
from pathlib import Path
import secrets
import threading

from translator.storage.project_storage import atomic_json, load_json
from translator.ui.task_errors import task_error


class QueueMixin:
    def init_queue(self):
        self.queue_path = Path(self.cfg["output_dir"]) / "web-task-queue.json"
        self.queue_control_path = self.queue_path.with_name("web-queue-control.json")
        try:
            self.queue_paused = load_json(self.queue_control_path, {}).get("paused", False) is True
        except (OSError, ValueError, AttributeError):
            self.queue_paused = True  # An unreadable control must not unexpectedly launch work.
        self._queue_thread = None
        self._queue_launching = set()
        self._queue_launch_threads = []
        self.queue_error = ""
        self._queue_keys = []
        self.completed_tasks = []
        try:
            saved = load_json(self.queue_path, [])
            if isinstance(saved, dict) and saved.get("version") == 2:
                self._queue_keys = saved.get("dedupe_keys", [])
                completed = saved.get("completed", [])
                if not isinstance(completed, list) or any(not isinstance(item, dict) for item in completed):
                    raise ValueError("任务完成记录无效")
                self.completed_tasks = completed[-5:]
                if not isinstance(self._queue_keys, list) or any(not isinstance(key, str) for key in self._queue_keys):
                    raise ValueError("任务队列去重记录无效")
                saved = saved.get("tasks")
            if not isinstance(saved, list) or any(
                not isinstance(item, dict) or item.get("slot") not in ("translation", "acquisition")
                or not isinstance(item.get("body"), dict) or not isinstance(item.get("id"), str)
                or item.get("status") not in ("waiting", "failed", "paused")
                for item in saved
            ):
                raise ValueError("任务队列记录无效")
            active = {entry["body"].get("_queue_id") for entry in self.recovery.snapshot().values()}
            self.queued_tasks = [item for item in saved if item["id"] not in active or item["status"] != "waiting"]
            # A held row is committed before its active journal is removed.
            # Finish that transfer if the process exited between the two writes.
            for slot, entry in self.recovery.snapshot().items():
                if any(item["id"] == entry["body"].get("_queue_id") and item["status"] != "waiting" for item in self.queued_tasks):
                    self.recovery.clear(slot, entry["id"])
        except (OSError, ValueError) as exc:
            self.queued_tasks = []
            self._queue_keys = []
            self.queue_error = str(exc)

    def _save_queue(self, tasks, *, keys=None):
        keys = self._queue_keys if keys is None else keys
        atomic_json(self.queue_path, {"version": 2, "tasks": tasks, "dedupe_keys": keys[-1000:], "completed": self.completed_tasks[-5:]})
        self.queued_tasks = tasks
        self._queue_keys = keys[-1000:]

    def enqueue(self, slot, body, *, _internal=False):
        from translator.acquisition.source_epub import normalize_work_url
        from translator.languages import language_code
        if slot not in ("translation", "acquisition"):
            raise ValueError("无效的任务类型")
        if not isinstance(body, dict):
            raise ValueError("任务参数必须是对象")
        body = deepcopy(body)
        dedupe_key = body.pop("dedupe_key", None)
        if dedupe_key is not None and (not isinstance(dedupe_key, str) or not dedupe_key or len(dedupe_key) > 100):
            raise ValueError("无效的任务去重编号")
        if body.get("url"):
            body["url"] = normalize_work_url(str(body["url"]).strip())
        elif slot == "acquisition" or not body.get("source"):
            raise ValueError("请输入小说网址或本地 EPUB 路径")
        if slot == "translation":
            body["language"] = language_code(body.get("language", "zh-Hans"))
            body["model"] = str(body.get("model", self.cfg["model"])).strip()
            if not body["model"]:
                raise ValueError("请选择模型")
        with self.lock:
            if self.restart_requested and not _internal or self.closing.is_set():
                raise ValueError("服务正在重启，请稍后添加任务")
            if dedupe_key in self._queue_keys:
                return {"queued": True, "duplicate": True}
            def existing_result(**result):
                # A follow-up is acknowledged even when another request already
                # queued the book. Persist its key before the acquisition clears.
                if dedupe_key:
                    self._save_queue(self.queued_tasks, keys=[*self._queue_keys, dedupe_key])
                return {"duplicate": True, **result}
            identity = self.queue_identity(slot, body)
            if slot == "acquisition":
                translating = self.recovery.snapshot().get("translation")
                if translating and translating["body"].get("url") == body.get("url") and self._browser_owner is not None:
                    return existing_result(queued=False, slot="translation")
            for item in self.queued_tasks:
                if item["slot"] == slot and self.queue_identity(slot, item["body"]) == identity:
                    return existing_result(queued=True, id=item["id"])
            saved = self.recovery.snapshot().get(slot)
            if saved and self.queue_identity(slot, saved["body"]) == identity:
                return existing_result(queued=False, slot=slot)
            item = {"id": secrets.token_hex(12), "slot": slot, "body": body,
                    "title": body.get("title") or Path(body.get("source", "")).stem or
                             "等待获取书名 · " + body.get("url", "").rsplit("/", 1)[-1],
                    "status": "waiting"}
            self._save_queue([*self.queued_tasks, item], keys=[*self._queue_keys, dedupe_key] if dedupe_key else self._queue_keys)
            self.start_queue()
            return {"queued": True, "id": item["id"]}

    def change_queue(self, body):
        with self.lock:
            tasks = deepcopy(self.queued_tasks)
            item = next((entry for entry in tasks if entry["id"] == body.get("id")), None)
            if item is None:
                raise ValueError("排队任务不存在或已开始")
            if body.get("action") in ("retry", "resume"):
                settings = body.get("settings", {})
                if settings:
                    item["body"] = self.recovery_settings(item["slot"], item["body"], settings)
                item["status"] = "waiting"
                item.pop("error", None)
            elif body.get("action") == "cancel":
                tasks.remove(item)
            elif body.get("action") == "pause":
                item["status"] = "paused"
            elif body.get("action") == "priority":
                if item["status"] != "waiting":
                    raise ValueError("请先继续或重试该任务")
                tasks.remove(item)
                tasks.insert(0, item)
            else:
                raise ValueError("无效的队列操作")
            self._save_queue(tasks)
            self.start_queue()
        return {"updated": True}

    def queue_identity(self, slot, body):
        import os
        from translator.languages import language_code
        identity = body.get("_book_identity") or body.get("url")
        if not identity:
            source = Path(str(body.get("source", ""))).resolve()
            identity = os.path.normcase(str(source))
            network_root = (Path(self.cfg["output_dir"]) / "network-workflows").resolve()
            if source.is_relative_to(network_root):
                for folder in (source.parent, source.parent.parent):
                    saved = self._cached_json(folder / "network.json", {})
                    if saved.get("url"):
                        identity = saved["url"]
                        break
        return identity, language_code(body.get("language", "zh-Hans")) if slot == "translation" else "original"

    def hold_task(self, slot, status="paused", error=None):
        """Durably transfer active work into a non-dispatchable queue row."""
        with self.lock:
            saved = self.recovery.snapshot().get(slot)
            if not saved:
                return
            body = deepcopy(saved["body"])
            identity = body.pop("_queue_id", None) or secrets.token_hex(12)
            task = self.task if slot == "translation" else self.acquisition_task
            item = {"id": identity, "slot": slot, "body": body, "status": status,
                    "title": task.get("title") or body.get("title") or Path(body.get("source", "")).stem or "等待获取书名"}
            if error:
                item["error"] = error
            self._save_queue([entry for entry in self.queued_tasks if entry["id"] != identity] + [item])
            self.recovery.clear(slot, saved["id"])
            task["recovering"] = False
            return identity

    def pause_task(self, slot, job_id=None):
        if slot not in ("translation", "acquisition"):
            raise ValueError("无效的任务类型")
        with self.lock:
            task = self.task if slot == "translation" else self.acquisition_task
            if job_id is not None and not self.recovery.matches(slot, job_id):
                raise ValueError("任务已变化，请刷新后操作")
            if task.get("kind") == "review":
                self.stop_task(slot)
                return {"paused": True}
            identity = self.hold_task(slot)
            cancel = self.cancel if slot == "translation" else self.acquisition_cancel
            cancel.set()
            task.update(stopping=bool(task.get("running")), message="正在暂停，已保存进度；后续小说继续处理")
            self.start_queue()
            return {"paused": True, "id": identity}

    def finish_queue_task(self, slot, body, title, project=""):
        import time
        with self.lock:
            self.completed_tasks = [*self.completed_tasks, {"slot": slot, "title": title,
                "project": project, "finished_at": time.time()}][-5:]
            self._save_queue([item for item in self.queued_tasks if item["id"] != body.get("_queue_id")])
            self._idle_completed = True
            self._idle_since = None

    def control_queue(self, paused):
        if not isinstance(paused, bool):
            raise ValueError("暂停状态必须是布尔值")
        with self.lock:
            if self.restart_requested:
                raise ValueError("服务正在重启，请稍后操作")
            atomic_json(self.queue_control_path, {"paused": paused})
            self.queue_paused = paused
            if paused:
                # Retain active journals so continuation resumes saved work first.
                for slot, task, cancel in (("translation", self.task, self.cancel),
                                           ("acquisition", self.acquisition_task, self.acquisition_cancel)):
                    if task.get("running"):
                        cancel.set()
                        task.update(stopping=True, message="正在暂停，保存进度后等待继续队列…")
            else:
                self._recovery_started = False
                self.restore_tasks()
                self.start_queue()
        return {"queue_paused": paused}

    def start_queue(self):
        with self.lock:
            if self._queue_thread is not None and self._queue_thread.is_alive():
                return
            self._queue_thread = threading.Thread(target=self._queue_worker, daemon=True)
            self._queue_thread.start()

    def _queue_worker(self):
        while not self.closing.is_set():
            with self.lock:
                if not self.restart_requested and not self.queue_paused:
                    for slot in ("translation", "acquisition"):
                        task = self.task if slot == "translation" else self.acquisition_task
                        if slot in self.recovery.snapshot() and not any(task.get(key) for key in ("running", "recovering", "retry_available")):
                            self._recovery_started = False
                            self.restore_tasks()
                        if task.get("running") or task.get("recovering") or slot in self.recovery.snapshot():
                            continue
                        # A failed head remains visible; later books may continue.
                        item = next((entry for entry in self.queued_tasks
                                     if entry["slot"] == slot and entry["status"] == "waiting"), None)
                        if item is None:
                            continue
                        if self._browser_owner is not None and (slot == "acquisition" or item["body"].get("url")):
                            continue
                        if slot in self._queue_launching:
                            continue
                        self._queue_launching.add(slot)
                        thread = threading.Thread(target=self._dispatch_queue, args=(slot, deepcopy(item)), daemon=True)
                        self._queue_launch_threads = [entry for entry in self._queue_launch_threads if entry.is_alive()]
                        self._queue_launch_threads.append(thread)
                        thread.start()
            self.closing.wait(.3)

    def queue_matches(self, body):
        identity = body.get("_queue_id")
        return not identity or not self.queue_paused and any(entry["id"] == identity and entry["status"] == "waiting" for entry in self.queued_tasks)

    def consume_queue(self, body):
        identity = body.get("_queue_id")
        if identity:
            self._save_queue([entry for entry in self.queued_tasks if entry["id"] != identity])

    def _dispatch_queue(self, slot, item):
        try:
            operation = self.start if slot == "translation" else self.acquire
            operation({**item["body"], "_queue_id": item["id"]})
        except Exception as exc:
            with self.lock:
                tasks = deepcopy(self.queued_tasks)
                saved = next((entry for entry in tasks if entry["id"] == item["id"]), None)
                if saved is not None and not self.closing.is_set():
                    task = self.task if slot == "translation" else self.acquisition_task
                    if isinstance(exc, ValueError) and (task.get("running") or self.restart_requested or self._browser_owner is not None):
                        return  # Another request acquired the slot while preflight ran.
                    saved.update(status="failed", error=task_error(exc))
                    self.diagnostic_event(slot, saved["error"])
                    self._save_queue(tasks)
        finally:
            with self.lock:
                self._queue_launching.discard(slot)
