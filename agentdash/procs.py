"""실행 중인 프로세스를 세션과 연결하고, 멀티플렉서 안에서 실행 중인지 확인한다.

Linux의 /proc만 읽는다. 다른 OS에서는 빈 결과를 돌려준다(macOS는 v2에서 ps로 처리할 예정).
환경 변수와 작업 폴더는 현재 사용자 프로세스만 읽을 수 있다. 부모 프로세스 이름은 모든 프로세스에서 읽는다.

세션 연결 순서:
1. ~/.claude/sessions/<pid>.json (데스크톱 앱 세션이 남기는 파일)
2. 명령줄의 `--resume <id>`, `--session-id <id>`, `codex resume <id>`
3. 열려 있는 Codex rollout 파일
4. 같은 작업 폴더에서 세션 첫 기록 시각과 2분 안에 시작한 claude, codex 프로세스
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

PROC = Path("/proc")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# 부모 프로세스 이름으로 찾는 멀티플렉서. 앞쪽이 우선한다.
MUX_COMM = (("tmux", "tmux"), ("zellij", "zellij"), ("screen", "screen"), ("herdr", "herdr"), ("cmux", "cmux"))
START_MATCH_SEC = 120
CODEX_NATIVE = re.compile(r"codex-(x86_64|aarch64|arm64)-")  # npm 패키지가 실행하는 실제 실행 파일


class Proc:
    __slots__ = ("pid", "ppid", "comm", "argv", "start", "mine")

    def __init__(self, pid, ppid, comm, argv, start, mine):
        self.pid, self.ppid, self.comm, self.argv, self.start, self.mine = pid, ppid, comm, argv, start, mine

    @property
    def agent(self) -> str | None:
        """claude, codex 실행 파일이면 그 이름. 앱 서버와 도우미 프로세스는 제외한다."""
        if not self.argv:
            return None
        exe = os.path.basename(self.argv[0])
        rest = self.argv[1:]
        if exe == "claude" or "/ccd-cli/" in self.argv[0] or any("@anthropic-ai/claude-code" in a for a in self.argv[:2]):
            return "claude"
        if (exe == "codex" or CODEX_NATIVE.match(exe) or any("@openai/codex" in a for a in self.argv[:2])) \
                and not (rest and rest[0] in ("app-server", "mcp-server")):
            return "codex"
        return None


def supported() -> bool:
    return sys.platform.startswith("linux") and PROC.is_dir()


def _boot_time() -> float:
    try:
        for line in (PROC / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def scan() -> dict[int, Proc]:
    if not supported():
        return {}
    uid = os.getuid()
    hz = os.sysconf("SC_CLK_TCK") or 100
    boot = _boot_time()
    out = {}
    for d in os.listdir(PROC):
        if not d.isdigit():
            continue
        pid = int(d)
        try:
            stat = (PROC / d / "stat").read_text()
            argv = (PROC / d / "cmdline").read_bytes().split(b"\0")
            owner = os.stat(PROC / d).st_uid
        except OSError:
            continue
        # comm에 공백이나 괄호가 들어갈 수 있어 마지막 ')' 뒤를 나눈다
        comm = stat[stat.find("(") + 1:stat.rfind(")")]
        fields = stat[stat.rfind(")") + 2:].split()
        try:
            ppid, start = int(fields[1]), boot + int(fields[19]) / hz
        except (IndexError, ValueError):
            continue
        out[pid] = Proc(pid, ppid, comm, [a.decode(errors="replace") for a in argv if a], start, owner == uid)
    return out


def _environ(pid: int) -> dict[str, str]:
    try:
        raw = (PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        k, _, v = item.partition(b"=")
        if k:
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def _cwd(pid: int) -> str | None:
    try:
        return os.readlink(PROC / str(pid) / "cwd")
    except OSError:
        return None


def mux_of(pid: int, procs: dict[int, Proc]) -> tuple[str | None, str | None]:
    """(멀티플렉서 이름, 세션 이름). 부모 프로세스를 먼저 보고, 없으면 환경 변수를 본다."""
    seen, p = set(), procs.get(pid)
    while p and p.pid not in seen and p.pid > 1:
        seen.add(p.pid)
        name = os.path.basename(p.argv[0]) if p.argv else ""
        for key, label in MUX_COMM:
            if p.comm.startswith(key) or name.startswith(key):
                return label, _mux_session(label, p, pid)
        p = procs.get(p.ppid)
    env = _environ(pid)
    if env.get("ZELLIJ_SESSION_NAME") or "ZELLIJ" in env:
        return "zellij", env.get("ZELLIJ_SESSION_NAME")
    if env.get("TMUX"):
        return "tmux", None
    if env.get("STY"):
        return "screen", env["STY"].split(".", 1)[-1]
    for k in env:
        if k.startswith("CMUX_"):
            return "cmux", None
        if k.startswith("HERDR_"):
            return "herdr", None
    return None, None


def _mux_session(label: str, server: Proc, pid: int) -> str | None:
    if label == "zellij":
        # zellij 서버는 `zellij --server <소켓 폴더>/<세션 이름>`으로 실행된다
        if "--server" in server.argv:
            i = server.argv.index("--server")
            if i + 1 < len(server.argv):
                return os.path.basename(server.argv[i + 1])
        return _environ(pid).get("ZELLIJ_SESSION_NAME")
    if label == "screen":
        sty = _environ(pid).get("STY")
        return sty.split(".", 1)[-1] if sty else None
    return None


def _arg_session(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a in ("--resume", "-r", "--session-id", "resume") and i + 1 < len(argv) and UUID.fullmatch(argv[i + 1]):
            return argv[i + 1]
        if a.startswith(("--resume=", "--session-id=")) and UUID.fullmatch(a.split("=", 1)[1]):
            return a.split("=", 1)[1]
    return None


def _open_rollouts(pid: int) -> list[str]:
    out = []
    for fd in glob.glob(f"/proc/{pid}/fd/*"):
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if "/.codex/sessions/" in target and target.endswith(".jsonl"):
            m = UUID.search(os.path.basename(target))
            if m:
                out.append(m.group(0))
    return out


def session_processes(sessions: list[dict], home: Path | None = None) -> dict[str, dict]:
    """sessions: key, agent, cwd, first_ts(초)를 가진 목록. 찾은 세션만 {key: 정보}로 돌려준다."""
    procs = scan()
    if not procs:
        return {}
    home = home or Path.home()
    by_id = {}
    for s in sessions:
        by_id.setdefault((s["agent"], s["key"].split(":", 1)[1]), s["key"])
    found: dict[str, int] = {}

    for f in glob.glob(str(home / ".claude/sessions/*.json")):
        try:
            pid = int(Path(f).stem)
            sid = json.loads(Path(f).read_text()).get("sessionId")
        except (ValueError, OSError):
            continue
        key = by_id.get(("claude", sid))
        if key and pid in procs:
            found.setdefault(key, pid)

    agents = [p for p in procs.values() if p.agent]
    for p in agents:
        sid = _arg_session(p.argv)
        key = by_id.get((p.agent, sid)) if sid else None
        if key:
            found.setdefault(key, p.pid)
        elif p.agent == "codex" and p.mine:
            for sid in _open_rollouts(p.pid):
                key = by_id.get(("codex", sid))
                if key:
                    found.setdefault(key, p.pid)

    used = set(found.values())
    for s in sessions:
        if s["key"] in found or not s.get("cwd") or not s.get("first_ts"):
            continue
        best = None
        for p in agents:
            if p.pid in used or p.agent != s["agent"] or not p.mine:
                continue
            gap = s["first_ts"] - p.start
            if -5 <= gap <= START_MATCH_SEC and _cwd(p.pid) == s["cwd"] and (best is None or gap < best[0]):
                best = (gap, p.pid)
        if best:
            found[s["key"]] = best[1]
            used.add(best[1])

    out = {}
    for key, pid in found.items():
        mux, name = mux_of(pid, procs)
        # 데스크톱 앱이 실행한 프로세스는 터미널과 무관하므로 화면에 멀티플렉서를 표시하지 않는다
        desktop = "/ccd-cli/" in (procs[pid].argv[0] if procs[pid].argv else "")
        out[key] = {"pid": pid, "alive": True, "desktop": desktop, "mux": mux, "mux_session": name}
    return out
