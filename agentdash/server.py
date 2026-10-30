"""대시보드 웹 서버. 127.0.0.1에만 연다."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agentdash.model import Registry

HOST = "127.0.0.1"
PORT_MIN, PORT_MAX = 24000, 25000


def default_port() -> int:
    """설정 파일이 없을 때 쓰는 포트. 설치 스크립트가 빈 포트를 찾기 시작하는 위치와 같다."""
    return PORT_MIN + (os.getuid() * 37) % (PORT_MAX - PORT_MIN + 1)


WEB = Path(__file__).resolve().parent / "web" / "index.html"
REFRESH_SEC = 2.0


class State:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.snapshot: dict = {}
        self.body = b"{}"
        self.digest = ""
        self.cond = threading.Condition()
        self.error = None
        self.app = {}
        threading.Thread(target=self._check_updates, daemon=True).start()

    def _check_updates(self) -> None:
        """새 버전 확인은 네트워크를 쓰므로 화면 갱신과 따로 돈다. 10분마다 보고, 실제로 받아 오는 것은 한 시간에 한 번이다."""
        from agentdash import updates
        while True:
            try:
                self.app = updates.info()
            except Exception:
                pass
            time.sleep(600)

    def loop(self) -> None:
        while True:
            try:
                self.registry.refresh()
                snap = self.registry.snapshot()
                self.error = None
            except Exception as e:  # 한 번 실패해도 서버는 계속 돈다
                self.error = f"{type(e).__name__}: {e}"
                time.sleep(REFRESH_SEC)
                continue
            snap["server_error"] = self.error
            snap["app"] = self.app
            stable = dict(snap)
            stable.pop("generated_at", None)
            digest = hashlib.sha1(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
            with self.cond:
                self.snapshot = snap
                self.body = json.dumps(snap, ensure_ascii=False, default=str).encode()
                if digest != self.digest:
                    self.digest = digest
                    self.cond.notify_all()
            time.sleep(REFRESH_SEC)


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agentdash"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value) -> None:
            self._send(code, json.dumps(value, ensure_ascii=False, default=str).encode())

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, WEB.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                with state.cond:
                    body = state.body
                self._send(200, body)
            elif url.path == "/api/candidates":
                days = float(parse_qs(url.query).get("days", ["7"])[0])
                with state.registry.lock:
                    items = state.registry.discover(days=days)
                for d in items:
                    d.pop("path", None)
                self._json(200, items)
            elif url.path == "/api/events":
                self._events()
            else:
                self._json(404, {"error": "not found"})

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = None
            try:
                while True:
                    with state.cond:
                        if state.digest == last:
                            state.cond.wait(timeout=15)
                        digest, body = state.digest, state.body
                    if digest != last:
                        self.wfile.write(b"event: state\ndata: " + body + b"\n\n")
                        last = digest
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self):
            url = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "잘못된 JSON"})
            tr = state.registry.tracked
            try:
                if url.path == "/api/track":
                    tr.add_dir(data["dir"]) if data.get("dir") else tr.add(data["key"])
                elif url.path == "/api/untrack":
                    tr.remove_dir(data["dir"]) if data.get("dir") else tr.remove(data["key"])
                elif url.path == "/api/complete":
                    tr.set_completed(data["key"], bool(data.get("done", True)))
                else:
                    return self._json(404, {"error": "not found"})
            except (KeyError, RuntimeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(200, {"ok": True})

    return Handler


def port_open(host: str = HOST, port: int | None = None) -> bool:
    port = port or default_port()
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def serve(port: int | None = None, root: bool | None = None) -> None:
    port = port or default_port()
    state = State(Registry(root=root))
    threading.Thread(target=state.loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, port), make_handler(state))
    httpd.daemon_threads = True
    print(f"agentdash: http://{HOST}:{port} (pid {os.getpid()})", flush=True)
    httpd.serve_forever()
