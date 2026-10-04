"""Serve live reading snapshots to local and LAN browsers."""
import json
import secrets
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs


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
                        if path.path.endswith("catalog"):
                            result = {"book": owner.book, "chapters": [
                                {"id": key, "title": value["title"], "revision": value["revision"]}
                                for key, value in owner.chapters.items()]}
                        else:
                            key = parse_qs(path.query).get("id", [""])[0]
                            result = owner.chapters.get(key)
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
            self.chapters.clear()

    def update(self, payload):
        language, index, title, originals, translations = payload
        with self.lock:
            self.revision += 1
            self.chapters[f"{language}:{index}"] = {
                "title": f"{language} · {index} · {title}", "revision": self.revision,
                "originals": list(originals), "translations": list(translations)}

    def close(self):
        self.http.shutdown()
        self.http.server_close()
