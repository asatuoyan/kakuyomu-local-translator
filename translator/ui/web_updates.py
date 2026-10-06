"""Detect local code changes and validate a fresh backend before restarting."""
import hashlib
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import tokenize


class UpdateMonitor:
    def __init__(self, root):
        self.root = Path(root)
        self.instance = secrets.token_urlsafe(12)
        self.lock = threading.Lock()
        self.checked_at = 0
        self.frontend = self._revision(self.frontend_files())
        self.backend = self._revision(self.backend_files())
        self.current = {"frontend_revision": self.frontend, "backend_changed": False}

    def frontend_files(self):
        return [path for path in (self.root / "web").rglob("*")
                if path.is_file() and path.suffix in (".html", ".js", ".css")]

    def backend_files(self):
        return list((self.root / "translator").rglob("*.py")) + [
            self.root / name for name in ("web_app.py", "main.py", "gui.py") if (self.root / name).is_file()]

    def _revision(self, paths):
        digest = hashlib.sha256()
        for path in sorted(paths):
            try:
                stat = path.stat()
                digest.update(f"{path.relative_to(self.root)}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode())
            except FileNotFoundError:
                continue
        return digest.hexdigest()

    def snapshot(self, *, force=False):
        with self.lock:
            if force or time.monotonic() - self.checked_at >= 3:
                self.current = {"frontend_revision": self._revision(self.frontend_files()),
                                "backend_changed": self._revision(self.backend_files()) != self.backend}
                self.checked_at = time.monotonic()
            return {**self.current, "instance": self.instance}

    def validate_backend(self):
        try:
            for path in self.backend_files():
                with tokenize.open(path) as source:
                    compile(source.read(), str(path), "exec")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", "-X", f"pycache_prefix={self.root / '__pycache__' / secrets.token_hex(12)}",
                 "-c", "import translator.ui.web_app; import translator.engine"],
                cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=30)
        except (SyntaxError, UnicodeError, OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"新版本检查失败，保留当前服务：{exc}") from exc
        if result.returncode:
            raise ValueError("新版本无法启动，保留当前服务：" + (result.stderr or result.stdout)[-2000:])
