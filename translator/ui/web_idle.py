"""Close the web application after completed work and an hour without interaction."""
import math
import time


class IdleCloseMixin:
    def init_idle_close(self):
        self.idle_close_enabled = True
        self._idle_completed = False
        self._idle_since = None

    def note_activity(self):
        with self.lock:
            if self._idle_since is not None:
                self._idle_since = time.monotonic()

    def control_idle_close(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("自动关闭状态必须是布尔值")
        with self.lock:
            self.idle_close_enabled = enabled
            self._idle_since = None
        return self.idle_close_status()

    def idle_close_status(self, *, close_if_due=False):
        with self.lock:
            ready = (self.idle_close_enabled and self._idle_completed
                     and not self.closing.is_set() and not self.restart_requested
                     and not self.queue_error and not self.queued_tasks
                     and not self._queue_launching and self._browser_owner is None
                     and not self.recovery.snapshot()
                     and not any(task.get(key) for task in (self.task, self.acquisition_task)
                                 for key in ("running", "recovering", "retry_available")))
            now = time.monotonic()
            if not ready:
                self._idle_since = None
            elif self._idle_since is None:
                self._idle_since = now
            remaining = None if self._idle_since is None else max(0, math.ceil(3600 - (now - self._idle_since)))
            if close_if_due and remaining == 0:
                self.closing.set()
            return {"enabled": self.idle_close_enabled, "remaining_seconds": remaining,
                    "closing": self.closing.is_set()}
