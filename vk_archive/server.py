"""Read-only loopback HTTP server for the separately maintained web assets."""

import http.server
import json
import re
import secrets
import urllib.parse
from pathlib import Path

WEB = Path(__file__).with_name("web")


def make_handler(store):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def finish(self):
            try:
                super().finish()
            finally:
                store.close()

        def common_headers(self, mime, length, extra=None):
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def send(self, value, mime="application/json; charset=utf-8", code=200, extra=None):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.common_headers(mime, len(data), extra)
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                port = self.server.server_port
                if self.headers.get("Host", "") not in ("127.0.0.1:%s" % port, "localhost:%s" % port):
                    self.send({"error": "Недопустимый Host"}, code=403)
                    return
                parsed = urllib.parse.urlsplit(self.path)
                args = urllib.parse.parse_qs(parsed.query)
                get = lambda key, default="": args.get(key, [default])[0]
                if parsed.path == "/":
                    nonce = secrets.token_urlsafe(20)
                    content = (WEB / "index.html").read_text(encoding="utf-8").replace("__NONCE__", nonce)
                    policy = "default-src 'self'; script-src 'nonce-%s'; style-src 'self'; img-src 'self'; media-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'" % nonce
                    self.send(content.encode("utf-8"), "text/html; charset=utf-8", extra={"Content-Security-Policy": policy})
                elif parsed.path in ("/app.js", "/app.css"):
                    mime = "text/javascript; charset=utf-8" if parsed.path.endswith(".js") else "text/css; charset=utf-8"
                    self.send((WEB / parsed.path[1:]).read_bytes(), mime)
                elif parsed.path == "/api/status":
                    self.send(store.status(brief=get("brief") == "1"))
                elif parsed.path == "/api/profile":
                    rows = store.rows("SELECT data FROM objects WHERE kind='profile' LIMIT 1")
                    self.send(store.bundle([json.loads(row["data"]) for row in rows]))
                elif parsed.path == "/api/objects":
                    self.send(store.objects(get("kind"), parent=get("parent"), offset=int(get("offset", "0")), limit=int(get("limit", "100")), query=get("q"), order=get("order", "desc"), after=int(get("after")) if get("after") else None, before=int(get("before")) if get("before") else None))
                elif parsed.path.startswith("/media/"):
                    self.media(urllib.parse.unquote(parsed.path[7:]))
                else:
                    self.send({"error": "Не найдено"}, code=404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, OverflowError):
                self.send({"error": "Некорректные параметры"}, code=400)
            except Exception:
                self.send({"error": "Ошибка чтения архива"}, code=500)

        def media(self, key):
            row = store.get_media(key)
            if not row or row["status"] not in {"done", "partial"} or not row["path"]:
                self.send({"error": "Файл не сохранён"}, code=404)
                return
            path = (store.root / row["path"]).resolve()
            media_root = (store.root / "media").resolve()
            if media_root not in path.parents or not path.is_file():
                self.send({"error": "Файл отсутствует"}, code=404)
                return
            size = path.stat().st_size
            start, end = 0, size - 1
            ranged = self.headers.get("Range")
            if ranged:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", ranged)
                if not match or not any(match.groups()):
                    self.send(b"", code=416, extra={"Content-Range": "bytes */%s" % size})
                    return
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(end, int(right)) if right else end
                else:
                    start = max(0, size - int(right))
                if start > end or start >= size:
                    self.send(b"", code=416, extra={"Content-Range": "bytes */%s" % size})
                    return
            safe = row["kind"] not in {"doc", "document"} and re.fullmatch(r"(?:image/(?:jpeg|png|gif|webp|avif)|audio/[a-zA-Z0-9.+-]+|video/[a-zA-Z0-9.+-]+)", row["mime"] or "")
            mime = row["mime"] if safe else "application/octet-stream"
            extra = {"Accept-Ranges": "bytes"}
            if not safe:
                extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + urllib.parse.quote(row["name"] or path.name)
            if ranged:
                extra["Content-Range"] = "bytes %s-%s/%s" % (start, end, size)
            self.send_response(206 if ranged else 200)
            self.common_headers(mime, max(0, end - start + 1), extra)
            if self.command != "HEAD":
                with path.open("rb") as source:
                    source.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = source.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
    return Handler


def create_server(store, port=0):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    server.daemon_threads = True
    return server
