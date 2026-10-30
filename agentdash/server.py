"""대시보드 웹 서버. 127.0.0.1에만 연다."""

from __future__ import annotations

import base64
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
# 브라우저 탭 아이콘(64x64 PNG). SVG 아이콘을 쓰지 않는 브라우저(Safari 등)와 /favicon.ico 요청용이다.
# 그라데이션 사각형에 진행률 호를 그린 이미지로, 이 저장소에서 직접 만들었다.
FAVICON_PNG = base64.b64decode(
    "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")


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
            if url.path in ("/favicon.ico", "/favicon.png"):
                self._send(200, FAVICON_PNG, "image/png")
            elif url.path in ("/", "/index.html"):
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
                elif url.path == "/api/ignore":
                    tr.ignore(data["key"])
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
