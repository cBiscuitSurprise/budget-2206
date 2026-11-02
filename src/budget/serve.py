import hashlib
import html
import json
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

POLL = (
    "let v=null;setInterval(async()=>{try{const t=await(await fetch('/version',{cache:'no-store'})).text();"
    "if(v&&t!==v)location.reload();v=t}catch{}},2000)"
)


def fingerprint(paths: list[Path]) -> str:
    h = hashlib.sha1()
    for p in paths:
        for f in sorted(p.rglob("*")) if p.is_dir() else [p]:
            if f.is_file():
                h.update(f"{f}:{f.stat().st_mtime_ns}".encode())
    return h.hexdigest()[:12]


def serve(
    render: Callable[[], str], watch: list[Path], host: str, port: int, saves: dict[str, Callable[[dict], str]] | None = None,
    allow_hosts: tuple[str, ...] = (),
) -> None:
    allowed_hosts = {f"[{h}]:{port}" if ":" in h else f"{h}:{port}" for h in ("127.0.0.1", "localhost", *allow_hosts)}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            ok = saves and self.path in saves and self.headers.get("Host") in allowed_hosts
            ok = ok and self.headers.get("X-Budget") == "1" and self.headers.get("Content-Type", "").startswith("application/json")
            if not ok:
                self._send(403, "text/plain", "forbidden")
                return
            try:
                self._send(200, "text/plain", saves[self.path](json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))))
            except Exception as e:
                self._send(400, "text/plain", str(e))

        def do_GET(self) -> None:
            if self.path == "/version":
                self._send(200, "text/plain", fingerprint(watch))
            elif self.path in ("/", "/index.html"):
                try:
                    self._send(200, "text/html", render())
                except Exception:
                    body = f"<pre style='white-space:pre-wrap'>{html.escape(traceback.format_exc())}</pre><script>{POLL}</script>"
                    self._send(500, "text/html", body)
            else:
                self._send(404, "text/plain", "not found")

        def _send(self, status: int, ctype: str, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    ThreadingHTTPServer((host, port), Handler).serve_forever()


_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".pdf": "application/pdf",
          ".tif": "image/tiff", ".tiff": "image/tiff"}


def serve_labeler(
    render: Callable[[], str], save: Callable[[dict], None], images: Path, watch: list[Path], host: str, port: int
) -> None:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    root = images.resolve()

    class Handler(BaseHTTPRequestHandler):
        def _guard(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._send(403, "text/plain", b"bad host")
                return False
            return True

        def do_GET(self) -> None:
            if not self._guard():
                return
            if self.path == "/version":
                self._send(200, "text/plain", fingerprint(watch).encode())
            elif self.path in ("/", "/index.html"):
                try:
                    self._send(200, "text/html", render().encode())
                except Exception:
                    self._send(500, "text/plain", traceback.format_exc().encode())
            elif self.path.startswith("/img/"):
                f = (root / self.path.removeprefix("/img/")).resolve()
                if f.is_relative_to(root) and f.suffix.lower() in _TYPES and f.is_file():
                    self._send(200, _TYPES[f.suffix.lower()], f.read_bytes(), cache=True)
                else:
                    self._send(404, "text/plain", b"not found")
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self) -> None:
            if not self._guard():
                return
            ok = self.path == "/label" and self.headers.get("X-Budget") == "1"
            ok = ok and self.headers.get("Content-Type", "").startswith("application/json")
            if not ok:
                self._send(403, "text/plain", b"forbidden")
                return
            try:
                save(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                self._send(200, "application/json", b'{"ok":true}')
            except Exception as e:
                self._send(400, "text/plain", str(e).encode())

        def _send(self, status: int, ctype: str, data: bytes, cache: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype if ctype.startswith(("image/", "application/pdf")) else f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    ThreadingHTTPServer((host, port), Handler).serve_forever()
