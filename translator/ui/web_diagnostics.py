"""Allowlisted diagnostics: never export inputs, arbitrary errors, logs or config."""
from collections import deque
from datetime import datetime, timezone
import platform
import sys

from translator.version import VERSION

ERROR_CODES = {"model_offline", "model_missing", "source_missing", "login_required", "task_failed"}


def _code(value):
    return value if value in ERROR_CODES else "task_failed"


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


class DiagnosticsMixin:
    def init_diagnostics(self):
        self._diagnostic_events = deque(maxlen=30)

    def diagnostic_event(self, slot, error):
        self._diagnostic_events.append({"at": datetime.now(timezone.utc).isoformat(),
            "slot": slot if slot in {"translation", "acquisition"} else "system",
            "code": _code(error.get("code"))})

    def diagnostics(self):
        with self.lock:
            tasks = {}
            for slot, task in (("translation", self.task), ("acquisition", self.acquisition_task)):
                counts = task.get("counts") or {}
                stage = getattr(self, "_model_stage", "translation") if slot == "translation" else "acquisition"
                tasks[slot] = {"running": bool(task.get("running")), "stopping": bool(task.get("stopping")),
                    "recovering": bool(task.get("recovering")), "percent": _number(task.get("percent")),
                    "stage": stage if stage in {"translation", "glossary", "acquisition"} else "translation",
                    "error_code": _code(task["recovery_error"].get("code")) if task.get("recovery_error") else None,
                    "counts": {name: _number(counts.get(name)) for name in ("acquired", "translated", "total")}}
            return {"schema_version": 1, "exported_at": datetime.now(timezone.utc).isoformat(),
                "application_version": VERSION, "python_version": sys.version.split()[0],
                "system": {"name": platform.system(), "release": platform.release(), "architecture": platform.machine()},
                "queue_paused": self.queue_paused, "tasks": tasks,
                "queue": [{"slot": item["slot"], "status": item["status"],
                    "error_code": _code(item["error"].get("code")) if item.get("error") else None}
                    for item in self.queued_tasks],
                "record_errors": {"queue": bool(self.queue_error), "recovery": bool(self.recovery.error)},
                "errors": list(self._diagnostic_events)}
