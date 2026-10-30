"""기록 파일 읽기. 쓰기는 하지 않는다.

LocalFS는 현재 사용자 파일을 직접 읽는다. SudoFS는 root 기록을 `sudo -n`으로 이 파일의
읽기 도우미를 실행해 읽는다. 도우미는 /root/.claude/projects 밖의 경로를 거부한다.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import subprocess
import sys
from dataclasses import dataclass

ROOT_ALLOWED = "/root/.claude/projects/"
MAX_READ = 64 * 1024 * 1024


@dataclass
class FileInfo:
    path: str
    size: int
    mtime: float


class LocalFS:
    user = "user"
    ok = True
    error = ""

    def __init__(self, user: str | None = None):
        if user:
            self.user = user

    def glob(self, pattern: str) -> list[FileInfo]:
        out = []
        for p in glob.glob(pattern, recursive=True):
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append(FileInfo(p, st.st_size, st.st_mtime))
        return out

    def read_from(self, path: str, offset: int, limit: int = MAX_READ) -> bytes:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                return f.read(limit)
        except OSError:
            return b""

    def read_head(self, path: str, n: int) -> bytes:
        return self.read_from(path, 0, n)

    def read_tail(self, path: str, n: int) -> bytes:
        try:
            size = os.path.getsize(path)
        except OSError:
            return b""
        return self.read_from(path, max(0, size - n), n)

    def read_json(self, path: str):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None


class SudoFS(LocalFS):
    """root 기록 읽기. 실패하면 ok=False로 두고 빈 결과를 돌려준다."""

    user = "root"

    def __init__(self):
        self.ok = True
        self.error = ""

    def _call(self, *args: str) -> bytes | None:
        if not self.ok:
            return None
        try:
            r = subprocess.run(
                ["sudo", "-n", sys.executable, "-I", os.path.abspath(__file__), *args],
                capture_output=True, timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self.ok, self.error = False, str(e)
            return None
        if r.returncode != 0:
            self.ok, self.error = False, r.stderr.decode(errors="replace").strip()[:200]
            return None
        return r.stdout

    def glob(self, pattern: str) -> list[FileInfo]:
        out = self._call("glob", pattern)
        if not out:
            return []
        return [FileInfo(**d) for d in json.loads(out)]

    def read_from(self, path: str, offset: int, limit: int = MAX_READ) -> bytes:
        out = self._call("read", path, str(offset), str(limit))
        return base64.b64decode(out) if out else b""

    def read_tail(self, path: str, n: int) -> bytes:
        out = self._call("tail", path, str(n))
        return base64.b64decode(out) if out else b""

    def read_json(self, path: str):
        data = self.read_from(path, 0, 1024 * 1024)
        try:
            return json.loads(data) if data else None
        except ValueError:
            return None


def _helper(argv: list[str]) -> int:
    """sudo로 실행되는 읽기 도우미."""
    cmd, path = argv[0], argv[1]
    if ".." in path.split("/"):
        print("허용되지 않은 경로", file=sys.stderr)
        return 2
    real = os.path.realpath(path.split("*")[0]) + ("/" if path.endswith("/") else "")
    if not (real + "/").startswith(ROOT_ALLOWED) and not real.startswith(ROOT_ALLOWED):
        print("허용되지 않은 경로", file=sys.stderr)
        return 2
    fs = LocalFS()
    if cmd == "glob":
        sys.stdout.write(json.dumps([f.__dict__ for f in fs.glob(path)]))
    elif cmd == "read":
        sys.stdout.write(base64.b64encode(fs.read_from(path, int(argv[2]), min(int(argv[3]), MAX_READ))).decode())
    elif cmd == "tail":
        sys.stdout.write(base64.b64encode(fs.read_tail(path, int(argv[2]))).decode())
    else:
        return 2
    return 0


class LineReader:
    """파일에 추가된 줄만 읽는다. 마지막 줄이 아직 덜 써졌으면 다음에 다시 읽는다."""

    def __init__(self, fs: LocalFS, path: str):
        self.fs = fs
        self.path = path
        self.offset = 0
        self.size = -1

    def poll(self, size: int | None = None) -> list[dict]:
        if size is not None and size == self.size:
            return []
        data = self.fs.read_from(self.path, self.offset)
        if not data:
            self.size = size if size is not None else self.size
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        chunk = data[: end + 1]
        self.offset += len(chunk)
        self.size = self.offset if size is None else size
        out = []
        for line in chunk.splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


if __name__ == "__main__":
    sys.exit(_helper(sys.argv[1:]))
