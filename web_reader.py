"""Serve live reading snapshots to local and LAN browsers."""
import json
import re
import secrets
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs


def load_saved_reading(path):
    """Compatibility adapter for callers requesting all saved text."""
    from saved_reading import SavedReading
    book = SavedReading(path)
    payloads = []
    for index, entry in enumerate(book.catalog, 1):
        record = book.chapter(entry["id"])
        payloads.append((book.language, index, record["title"],
                         record["originals"], record["translations"]))
    return book.identity, book.title, payloads


def physical_lan_addresses():
    """Prefer active physical adapters on Windows; None permits fallback."""
    if sys.platform != "win32":
        return None
    script = (
        "$ErrorActionPreference='Stop'; "
        "$adapters=Get-NetAdapter -Physical | Where-Object Status -eq 'Up'; "
        "@($adapters | ForEach-Object { Get-NetIPAddress -InterfaceIndex $_.ifIndex "
        "-AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Where-Object AddressState -eq 'Preferred' | Select-Object -ExpandProperty IPAddress }) "
        "| ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        values = json.loads(result.stdout or "[]")
        if isinstance(values, str):
            values = [values]
        return {ip for ip in values if isinstance(ip, str)
                and not ip.startswith(("127.", "169.254.", "0."))}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


class ReadingServer:
    def __init__(self):
        self.lock = threading.Lock()
        self.book = ""
        self.saved = None
        self.chapters = {}
        self.revision = 0
        self.token = secrets.token_urlsafe(24)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path)
                prefix = "/" + owner.token
                if path.path == prefix + "/":
                    data = Path(__file__).with_name("reader.html").read_bytes()
                    mime = "text/html; charset=utf-8"
                elif path.path in (prefix + "/catalog", prefix + "/chapter"):
                    with owner.lock:
                        saved = owner.saved
                        if path.path.endswith("catalog"):
                            entries = []
                            volumes = {}
                            for key, value in sorted(owner.chapters.items(), key=lambda item: (item[1]["language"], item[1]["index"])):
                                language = value["language"]
                                match = re.search(r"第\s*[0-9０-９一二三四五六七八九十百千零〇两壱弐参]+\s*[卷巻部]", value["chapter_title"])
                                if match:
                                    volumes[language] = match.group(0)
                                entries.append({"id": key, "title": value["title"],
                                                "revision": value["revision"], "language": language,
                                                "volume": volumes.get(language, "未分卷")})
                            result = {"book": owner.book, "chapters": entries}
                        else:
                            key = parse_qs(path.query).get("id", [""])[0]
                            result = owner.chapters.get(key)
                    if saved is not None:
                        try:
                            result = ({"book": saved.identity, "title": saved.title, "chapters": saved.catalog}
                                      if path.path.endswith("catalog") else saved.chapter(key))
                            if result is not None and path.path.endswith("chapter"):
                                result = {**result, "revision": saved.revision}
                        except (OSError, ValueError, KeyError) as exc:
                            self.send_error(500, "Chapter could not be loaded")
                            return
                    data = json.dumps(result, ensure_ascii=False).encode("utf-8")
                    mime = "application/json; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.http = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def url(self, host="127.0.0.1"):
        return f"http://{host}:{self.http.server_port}/{self.token}/"

    def lan_urls(self):
        physical = physical_lan_addresses()
        if physical is not None:
            return [self.url(ip) for ip in sorted(physical)]
        addresses = set()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                connection.connect(("192.0.2.1", 80))
                addresses.add(connection.getsockname()[0])
        except OSError:
            pass
        try:
            addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
        return [self.url(ip) for ip in sorted(addresses) if not ip.startswith("127.")]

    def reset(self, book):
        with self.lock:
            self.book = book
            self.saved = None
            self.chapters.clear()

    def open_saved(self, saved):
        with self.lock:
            self.revision += 1
            saved.revision = self.revision
            for entry in getattr(saved, "catalog", []):
                entry["revision"] = saved.revision
            self.book = saved.identity
            self.saved = saved
            self.chapters.clear()

    def update(self, payload):
        language, index, title, originals, translations = payload
        with self.lock:
            self.revision += 1
            self.chapters[f"{language}:{index}"] = {
                "language": language, "index": index, "chapter_title": title,
                "title": f"{language} · {index} · {title}", "revision": self.revision,
                "originals": list(originals), "translations": list(translations)}

    def close(self):
        self.http.shutdown()
        self.http.server_close()
