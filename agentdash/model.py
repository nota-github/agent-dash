"""등록 목록, 세션 상태, 진행률, 예상시간, 화면용 JSON."""

from __future__ import annotations

import json
import os
import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from agentdash.claude import USER_INPUT_TOOLS, AGENT_TOOLS, ClaudeSession, short
from agentdash.codex import CodexSession, rollout_id
from agentdash import procs
from agentdash.plan import ACTIVE, DONE, points, step_points
from agentdash.sources import LocalFS, SudoFS

HOME = Path.home()
STATE_DIR = HOME / ".agentdash"
WAIT_GUESS_SEC = 60
STALL_SEC = 600
RUNNING_RECENT_SEC = 600
PRIOR_SEC_PER_POINT = 240
COMPLETE_GRACE_SEC = 30  # 완료로 표시한 직후 기록되는 마무리 활동은 다시 열지 않는다
PROC_SCAN_SEC = 10  # 프로세스 목록은 이 간격으로만 다시 읽는다
BACKGROUND_MAX_SEC = 6 * 3600  # 완료 알림 없이 이보다 오래된 백그라운드 작업은 끝난 것으로 본다
CONTEXT_WINDOW = {"claude-opus": 1_000_000, "claude-sonnet": 1_000_000, "claude-haiku": 200_000}
STATUS_LABEL = {
    "needs_input": "선택/승인 필요",
    "maybe_waiting": "대기 추정",
    "running": "실행 중",
    "stalled": "정체",
    "idle": "사용자가 입력(승인)할 차례입니다",
    "done": "완료",
    "unreadable": "읽을 수 없음",
}
UNTRACKED_KEEP = 50  # 대시보드에서 해제한 세션을 세션 추가 목록 위쪽에 보여주는 개수
STATUS_ORDER = ["needs_input", "maybe_waiting", "stalled", "running", "idle", "done", "unreadable"]


def parse_ts(ts) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def today_prefix() -> str:
    # transcript 시각은 UTC다. 로컬 날짜의 0시를 UTC 문자열로 바꿔 비교한다.
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def workspace_name(cwd: str | None) -> str:
    if not cwd:
        return "?"
    parts = cwd.rstrip("/").split("/")
    if "ws" in parts and parts.index("ws") + 1 < len(parts):
        return parts[parts.index("ws") + 1]
    return parts[-1] or cwd


_HOST: dict | None = None


def host_info() -> dict:
    """이 도구가 실행 중인 서버의 이름과 IP. 세션 기록을 읽는 서버가 곧 에이전트가 실행되는 서버다."""
    global _HOST
    if _HOST is None:
        import socket
        name = socket.gethostname()
        ip = None
        try:
            # 패킷은 보내지 않는다. 바깥으로 나갈 때 쓰는 주소만 확인한다.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))
                ip = s.getsockname()[0]
        except OSError:
            pass
        if not ip or ip.startswith("127."):
            try:
                ip = socket.gethostbyname(name)
            except OSError:
                ip = None
        _HOST = {"name": name, "ip": ip}
    return _HOST


def launch_of(agent: str, entry: str | None, source=None, headless: bool = False) -> str:
    """화면에 보여줄 실행 방식. remote(데스크톱 앱이나 IDE가 SSH로 연결해 실행) 또는 cli(서버 터미널에서 실행).

    Claude는 entrypoint, Codex는 session_meta의 source로 판단한다. Codex 앱과 IDE 확장은 originator가
    codex-tui여도 source가 vscode라서 source를 먼저 본다. claude -p, codex exec는 cli다.
    """
    if agent == "claude":
        return "remote" if entry == "claude-desktop" and not headless else "cli"
    if isinstance(source, str):
        return "cli" if source in ("cli", "exec") else "remote"
    if isinstance(source, dict):  # 하위 에이전트 등 구조화된 source는 터미널 실행에서만 생긴다
        return "cli"
    return "cli" if entry in ("codex-tui", "codex_exec", "codex_cli_rs") else "remote"


def ssh_target() -> tuple[str | None, int | None]:
    """로컬 PC에서 이 서버로 SSH 접속할 때 쓰는 주소와 포트.

    SSH로 접속한 터미널이면 SSH_CONNECTION(클라이언트 IP, 포트, 서버 IP, 포트)에서 실제로 접속한 주소를 쓴다.
    아니면 서버 IP를 쓰고 포트는 모른다(None).
    """
    parts = os.environ.get("SSH_CONNECTION", "").split()
    if len(parts) == 4 and parts[3].isdigit():
        return parts[2], int(parts[3])
    h = host_info()
    return h["ip"] or h["name"], None


def tunnel_command(port: int, user: str) -> str:
    host, sshp = ssh_target()
    opt = f"-p {sshp} " if sshp and sshp != 22 else ""
    return f"ssh -f -N {opt}-L {port}:127.0.0.1:{port} {user}@{host}"


def load_config() -> dict:
    """설치 스크립트가 만든 설정. 없으면 빈 값이다 (개발용 실행)."""
    try:
        return json.loads((STATE_DIR / "config.json").read_text())
    except (OSError, ValueError):
        return {}


def project_root(cwd: str) -> str:
    """작업공간 폴더(ws/<이름>)까지 줄인 경로. ws 구조가 아니면 그대로 둔다."""
    parts = cwd.rstrip("/").split("/")
    if "ws" in parts and parts.index("ws") + 1 < len(parts):
        return "/".join(parts[:parts.index("ws") + 2])
    return cwd.rstrip("/")


MAIN_FOLDER_SHARE = 0.2  # 시작 폴더 아래 폴더가 기록의 이 비율 이상이면 그 폴더를 세션 폴더로 본다
WS_FOLDER_SHARE = 0.1  # ws/<이름> 작업공간은 이 비율만 넘어도 우선한다 (repos 같은 참고용 폴더보다 작업 폴더가 맞다)


def main_folder(first: str | None, counts: dict[str, int]) -> str | None:
    """카드에 보여줄 세션 폴더.

    데스크톱 앱 세션은 상위 폴더에서 시작해 작업공간으로 옮겨 다니는 경우가 많다. 시작 폴더 아래에서
    가장 많이 쓴 작업공간(ws/<이름>, 아니면 시작 폴더 바로 아래 폴더)이 전체 기록의 20% 이상이면 그 폴더를,
    아니면 시작 폴더를 쓴다.
    """
    if not first:
        return None
    base = first.rstrip("/")
    total = sum(counts.values()) or 1
    groups: dict[str, int] = {}
    ws_groups: set[str] = set()
    for cwd, n in counts.items():
        cwd = cwd.rstrip("/")
        if not cwd.startswith(base + "/"):
            continue
        rel = cwd[len(base) + 1:].split("/")
        if "ws" in rel[:-1]:
            root = project_root(cwd)  # 시작 폴더 아래 ws/<이름>
            ws_groups.add(root)
        else:
            root = base + "/" + rel[0]  # 시작 폴더 바로 아래 폴더
        groups[root] = groups.get(root, 0) + n
    ws = [g for g in ws_groups if groups[g] / total >= WS_FOLDER_SHARE]
    if ws:
        return max(ws, key=groups.get)
    if groups:
        best = max(groups, key=groups.get)
        if groups[best] / total >= MAIN_FOLDER_SHARE:
            return best
    return first


def context_window(model: str | None) -> int:
    for prefix, n in CONTEXT_WINDOW.items():
        if model and model.startswith(prefix):
            return n
    return 200_000


# ---------- 등록 목록 ----------

class Tracked:
    def __init__(self, path: Path = STATE_DIR / "tracked.json"):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"sessions": [], "dirs": [], "untracked": [], "completed": {}}
        self.load()

    def load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {"sessions": [], "dirs": [], "untracked": [], "completed": {}}
        self.data.setdefault("completed", {})
        self.data.setdefault("sessions", [])
        self.data.setdefault("dirs", [])
        self.data.setdefault("untracked", [])

    def save(self) -> None:
        if not self.path.parent.exists():
            raise RuntimeError(f"{self.path.parent}가 없음. 먼저 install.sh base를 실행해야 함")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def add(self, key: str) -> None:
        with self.lock:
            self.load()
            if key not in self.data["sessions"] or key in self.data["untracked"]:
                if key not in self.data["sessions"]:
                    self.data["sessions"].append(key)
                self.data["untracked"] = [k for k in self.data["untracked"] if k != key]
                self.save()

    def remove(self, key: str) -> None:
        """해제한 세션은 untracked 맨 앞에 둔다. 폴더 등록에 해당하는 세션도 이 목록이 우선한다."""
        with self.lock:
            self.load()
            self.data["sessions"] = [k for k in self.data["sessions"] if k != key]
            self.data["untracked"] = ([key] + [k for k in self.data["untracked"] if k != key])[:UNTRACKED_KEEP]
            self.save()

    def add_dir(self, d: str) -> None:
        with self.lock:
            self.load()
            d = os.path.abspath(d)
            if d not in self.data["dirs"]:
                self.data["dirs"].append(d)
                self.save()

    def remove_dir(self, d: str) -> None:
        with self.lock:
            self.load()
            self.data["dirs"] = [x for x in self.data["dirs"] if x != os.path.abspath(d)]
            self.save()

    def replace(self, old: str, new: str) -> None:
        """세션이 새 ID로 이어졌을 때 등록을 옮긴다. 사용자가 해제한 것이 아니므로 해제 목록에는 넣지 않는다."""
        with self.lock:
            self.load()
            if old in self.data["sessions"]:
                self.data["sessions"] = [new if k == old else k for k in self.data["sessions"]]
                if new in self.data["sessions"][self.data["sessions"].index(new) + 1:]:
                    self.data["sessions"].remove(new)
            elif new not in self.data["sessions"]:
                self.data["sessions"].append(new)
            done = self.data["completed"].pop(old, None)
            if done:
                self.data["completed"][new] = done
            self.save()

    def set_completed(self, key: str, done: bool) -> None:
        """사용자가 직접 완료로 표시한 세션. 표시한 뒤 새 활동이 생기면 snapshot에서 자동으로 푼다."""
        with self.lock:
            self.load()
            if done:
                self.data["completed"][key] = time.time()
            else:
                self.data["completed"].pop(key, None)
            self.save()

    def matches(self, key: str, cwd: str | None) -> bool:
        if key in self.data["sessions"]:
            return True
        if key in self.data["untracked"]:
            return False
        return bool(cwd) and any(cwd == d or cwd.startswith(d.rstrip("/") + "/") for d in self.data["dirs"])


# ---------- 세션 찾기 ----------

class Sources:
    def __init__(self, root: bool = True):
        self.local = LocalFS(os.environ.get("USER") or HOME.name)
        self.root = SudoFS() if root and os.geteuid() != 0 else None
        self._head_cache: dict[str, tuple[float, dict]] = {}
        self._codex_titles: dict[str, str] = {}
        self._codex_titles_mtime = 0.0

    def claude_files(self):
        yield "claude", self.local, self.local.glob(str(HOME / ".claude/projects/*/*.jsonl"))
        if self.root and self.root.ok:
            yield "claude-root", self.root, self.root.glob("/root/.claude/projects/*/*.jsonl")

    def codex_files(self):
        return self.local.glob(str(HOME / ".codex/sessions/*/*/*/rollout-*.jsonl"))

    def codex_titles(self) -> dict[str, str]:
        p = HOME / ".codex/session_index.jsonl"
        try:
            m = p.stat().st_mtime
        except OSError:
            return self._codex_titles
        if m != self._codex_titles_mtime:
            self._codex_titles_mtime = m
            for line in p.read_text(errors="replace").splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("id") and d.get("thread_name"):
                    self._codex_titles[d["id"]] = d["thread_name"]
        return self._codex_titles

    def head(self, kind: str, fs: LocalFS, f) -> dict:
        """최근 세션 목록용 요약. 파일 끝 일부만 읽는다."""
        cached = self._head_cache.get(f.path)
        if cached and cached[0] == f.mtime:
            return cached[1]
        info = {"title": None, "cwd": None, "entry": None}
        counts: dict[str, int] = {}
        if kind.startswith("claude"):
            tail = fs.read_tail(f.path, 256 * 1024).decode(errors="replace").splitlines()
            for line in reversed(tail):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("type")
                if t in ("custom-title", "agent-name"):
                    info["named"] = True
                if t == "custom-title" and not info["title"]:
                    info["title"] = d.get("customTitle")
                elif t == "ai-title" and not info.get("ai"):
                    info["ai"] = d.get("aiTitle")
                if d.get("cwd"):
                    counts[d["cwd"]] = counts.get(d["cwd"], 0) + 1
                    if not info["entry"]:
                        info["entry"] = d.get("entrypoint")
            info["title"] = info["title"] or info.pop("ai", None)
            info.pop("ai", None)
            info["headless"] = info["entry"] == "sdk-cli" or (info["entry"] == "claude-desktop" and not info.pop("named", False))
            info.pop("named", None)
            info["launch"] = launch_of("claude", info["entry"], headless=info["headless"])
            first_cwd = None
            for line in fs.read_head(f.path, 64 * 1024).split(b"\n")[:200]:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("timestamp") and not info.get("first_ts"):
                    info["first_ts"] = d["timestamp"]
                if d.get("cwd") and not first_cwd:
                    first_cwd = d["cwd"]
                if info.get("first_ts") and first_cwd:
                    break
            first_cwd = first_cwd or (max(counts, key=counts.get) if counts else None)
            info["cwd_first"] = first_cwd
            info["cwd"] = main_folder(first_cwd, counts)
        else:
            first = fs.read_head(f.path, 1024 * 1024).split(b"\n", 1)[0]
            try:
                p = json.loads(first).get("payload") or {}
                info["cwd"], info["entry"] = p.get("cwd"), p.get("originator")
                info["launch"] = launch_of("codex", p.get("originator"), p.get("source"))
            except ValueError:
                pass
            info["title"] = self.codex_titles().get(rollout_id(f.path) or "")
            info["first_ts"] = _rollout_ts(f.path)
        self._head_cache[f.path] = (f.mtime, info)
        return info


# ---------- 상태와 계산 ----------

def app_states(home: Path = HOME) -> dict[str, dict]:
    """Claude 앱(데스크톱, 대화형 CLI, 백그라운드 에이전트)이 남기는 세션 상태. {세션 ID: {status, since}}.

    ~/.claude/sessions/<pid>.json의 status는 idle, busy, waiting이다. waiting은 객관식 질문이나 승인 창이
    떠 있는 상태인데, 이때 질문 기록은 답을 고른 뒤에야 기록 파일에 쓰이므로 기록만으로는 알 수 없다.
    프로세스가 살아 있는 파일만 쓴다.
    """
    out: dict[str, dict] = {}
    for f in (home / ".claude" / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid = int(d.get("pid") or f.stem)
            os.kill(pid, 0)
        except (OSError, ValueError, TypeError):
            continue
        sid, st = d.get("sessionId"), d.get("status")
        if sid and st in ("idle", "busy", "waiting"):
            ts = d.get("statusUpdatedAt") or d.get("updatedAt")
            out[sid] = {"status": st, "since": ts / 1000 if isinstance(ts, (int, float)) else None}
    return out


def apply_app_state(status: str, since, detail, app: dict | None):
    """기록으로 판단한 상태를 앱 상태로 바로잡는다. 앱 상태가 없으면 그대로 둔다."""
    if not app:
        return status, since, detail
    st = app["status"]
    if st == "waiting":
        return "needs_input", app.get("since") or since, detail or "앱에서 선택이나 승인을 기다리는 중"
    if st == "busy" and status in ("idle", "maybe_waiting"):
        return "running", since, detail
    if st == "idle" and status in ("running", "maybe_waiting", "stalled"):
        return "idle", app.get("since") or since, None
    return status, since, detail


def claude_status(t, now: float, other_activity: float = 0.0, headless: bool = False) -> tuple[str, float | None, str | None]:
    """(상태, 그 상태가 시작된 시각, 설명). 헤드리스 세션은 승인 대기가 없으므로 결과를 기다리는 도구를 실행 중으로 본다."""
    last = parse_ts(t.last_ts) or 0
    activity = max(last, other_activity)
    if t.pending:
        oldest = min((parse_ts(p["ts"]) or now for p in t.pending.values()), default=now)
        names = [p["name"] for p in t.pending.values()]
        if any(n in USER_INPUT_TOOLS for n in names):
            return "needs_input", oldest, next(p["summary"] for p in t.pending.values() if p["name"] in USER_INPUT_TOOLS)
        if not headless and now - oldest > WAIT_GUESS_SEC and now - activity > WAIT_GUESS_SEC:
            return "maybe_waiting", oldest, next(iter(t.pending.values()))["summary"]
        return "running", oldest, next(iter(t.pending.values()))["summary"]
    if t.last_kind == "assistant" and t.last_stop == "end_turn":
        return "idle", last, None
    if now - activity > STALL_SEC:
        return "stalled", activity, None
    return "running", last, None


ACTIVE_MIN, ACTIVE_MAX = 0.05, 0.9  # 진행 중 단계는 이 범위 안에서만 채운다 (끝났다고 보지 않는다)
STEP_MIN_SEC = 30  # 이보다 짧게 끝난 단계는 한꺼번에 완료 처리된 것으로 보고 속도 계산에서 뺀다


def active_fraction(step: dict, steps: list[dict], now: float, prior: float = PRIOR_SEC_PER_POINT) -> float:
    """진행 중 단계의 진척. 이 세션에서 끝난 단계들의 점수당 시간과 도구 호출 수에 견줘 계산한다.

    끝난 단계가 없으면 점수당 기본 시간(prior)만 쓴다.
    """
    a = parse_ts(step.get("started"))
    spent = max(0.0, now - a) if a else 0.0
    sec_per_pt, tools_per_pt = [], []
    for s in steps:
        if s["status"] != DONE:
            continue
        sa, sb = parse_ts(s.get("started")), parse_ts(s.get("ended"))
        if sa and sb and sb - sa >= STEP_MIN_SEC:
            sec_per_pt.append((sb - sa) / step_points(s))
            if s.get("tools"):
                tools_per_pt.append(s["tools"] / step_points(s))
    pts = step_points(step)
    fracs = [spent / (pts * (sum(sec_per_pt) / len(sec_per_pt) if sec_per_pt else prior))]
    if tools_per_pt and step.get("tools") is not None:
        fracs.append(step.get("tools", 0) / (pts * sum(tools_per_pt) / len(tools_per_pt)))
    frac = sum(fracs) / len(fracs)
    return max(ACTIVE_MIN, min(ACTIVE_MAX, frac))


RECENT_SEC = 600  # 최근 활동량을 세는 구간


def activity(tool_times, turn_ts: str | None, now: float) -> dict:
    """이번 턴의 경과 시간과 도구 호출 수, 최근 10분 도구 호출 수. 계획이 없는 세션의 진행 표시로 쓴다."""
    turn_tools = sum(1 for t in tool_times if turn_ts and t >= turn_ts) if turn_ts else 0
    recent = 0
    for t in reversed(tool_times):
        v = parse_ts(t)
        if not v or now - v > RECENT_SEC:
            break
        recent += 1
    a = parse_ts(turn_ts)
    return {"turn_started": a, "turn_tools": turn_tools, "recent_tools": recent}


def progress(steps: list[dict], child_progress: dict[str, float] | None = None, now: float | None = None) -> float | None:
    """완료 단계 점수 + 진행 중 단계의 진척을 전체 점수로 나눈다.

    진행 중 단계는 그 단계에서 띄운 서브 에이전트의 진행률을 먼저 쓰고, 없으면 활동량(시간, 도구 호출)으로 채운다.
    """
    if not steps:
        return None
    total = points(steps)
    done = 0.0
    for s in steps:
        if s["status"] == DONE:
            done += step_points(s)
        elif s["status"] == ACTIVE:
            frac = (child_progress or {}).get(s["content"])
            if frac is None:
                frac = active_fraction(s, steps, now) if now else 0.5
            done += step_points(s) * frac
    return done / total if total else None


def eta(steps: list[dict], now: float, prior: float = PRIOR_SEC_PER_POINT) -> list[int] | None:
    """남은 시간 범위(초). 완료한 단계의 실제 속도와 사전값을 섞는다."""
    if not steps:
        return None
    remaining = 0.0
    done_pts, done_sec = 0, 0.0
    for s in steps:
        p = step_points(s)
        if s["status"] == DONE:
            a, b = parse_ts(s.get("started")), parse_ts(s.get("ended"))
            if a and b and b >= a:
                done_pts += p
                done_sec += b - a
        elif s["status"] == ACTIVE:
            a = parse_ts(s.get("started"))
            spent = now - a if a else 0
            remaining += max(p * 0.25, p - spent / prior)
        else:
            remaining += p
    if remaining <= 0:
        return [0, 0]
    observed = done_sec / done_pts if done_pts else prior
    w = done_pts / (done_pts + 5)
    rate = w * observed + (1 - w) * prior
    mid = remaining * rate
    return [int(mid * 0.7), int(mid * 1.5)]


def resume_command(agent: str, source: str, sid: str, cwd: str | None, entry: str | None, status: str) -> tuple[str, str | None]:
    cd = f"cd {shlex.quote(cwd)} && " if cwd else ""
    note = None
    if agent == "codex":
        return f"{cd}codex resume {sid}", None
    cmd = f"{cd}claude --resume {sid}"
    if source == "claude-root":
        cmd = f"sudo -i bash -c {shlex.quote(cmd)}"
    if entry == "claude-desktop" and status in ("running", "needs_input", "maybe_waiting"):
        note = "데스크톱 앱에서 실행 중인 세션이다. CLI로 열면 같은 세션을 두 곳에서 쓰게 된다"
    return cmd, note


# ---------- 레지스트리 ----------

class Registry:
    _proc_info: dict = {}  # 테스트처럼 __init__ 없이 만든 경우의 기본값. 값은 통째로 바꾸기만 한다.
    _proc_at = 0.0
    _show_last_message = True
    _app: dict = {}

    def __init__(self, tracked: Tracked | None = None, root: bool | None = None):
        if root is None:
            root = load_config().get("read_root", True)
        self.tracked = tracked or Tracked()
        self.src = Sources(root=root)
        self.sessions: dict[str, ClaudeSession | CodexSession] = {}
        self.source_of: dict[str, str] = {}
        self.lock = threading.RLock()
        self.version = 0
        self.last_refresh = 0.0
        self._codex_index: list[tuple[str, object]] = []
        self._proc_info: dict[str, dict] = {}
        self._proc_at = 0.0

    def discover(self, days: float = 7) -> list[dict]:
        """최근 세션 목록 (등록 후보)."""
        cutoff = time.time() - days * 86400
        untracked = self.tracked.data["untracked"]
        keep = set(untracked)
        out = []
        for kind, fs, files in self.src.claude_files():
            for f in files:
                sid = os.path.basename(f.path)[:-len(".jsonl")]
                if f.mtime < cutoff and f"{kind}:{sid}" not in keep:
                    continue
                h = self.src.head(kind, fs, f)
                out.append({"key": f"{kind}:{sid}", "agent": "claude", "source": kind, "id": sid, "path": f.path,
                            "mtime": f.mtime, "user": fs.user, **h})
        for f in self.src.codex_files():
            sid = rollout_id(f.path)
            if f.mtime < cutoff and f"codex:{sid}" not in keep:
                continue
            h = self.src.head("codex", self.src.local, f)
            out.append({"key": f"codex:{sid}", "agent": "codex", "source": "codex", "id": sid, "path": f.path,
                        "mtime": f.mtime, "user": self.src.local.user, **h})
        out.sort(key=lambda d: -d["mtime"])
        parents = getattr(self, "child_parent", {})
        kids: dict[str, int] = {}
        for d in out:
            d["tracked"] = self.tracked.matches(d["key"], d.get("cwd"))
            d["workspace"] = workspace_name(d.get("cwd"))
            # 하위 세션(스크립트가 실행한 claude -p, codex exec)은 부모 세션에 묶어 목록에서 따로 늘어놓지 않는다
            if d.get("entry") == "codex_exec":
                d["kind"] = "codex exec"
            elif d.get("headless"):
                d["kind"] = "claude -p"
            if d["key"] in parents:
                d["parent"] = parents[d["key"]]
                kids[d["parent"]] = kids.get(d["parent"], 0) + 1
            if d["key"] in keep:
                d["untracked_rank"] = untracked.index(d["key"])
        for d in out:
            if kids.get(d["key"]):
                d["children"] = kids[d["key"]]
        return out

    def refresh(self) -> bool:
        with self.lock:
            self.tracked.load()
            changed = False
            cands = self.discover(days=30)
            self._cands = cands
            want = {c["key"]: c for c in cands if c["tracked"]}
            for key, c in want.items():
                if key not in self.sessions:
                    fs = self.src.root if c["source"] == "claude-root" else self.src.local
                    self.sessions[key] = (CodexSession if c["agent"] == "codex" else ClaudeSession)(fs, c["path"])
                    self.source_of[key] = c["source"]
                    changed = True
            # 등록한 Claude 세션이 실행한 하위 세션(codex exec, claude -p)도 포함한다.
            child = self._child_candidates()
            for key in list(self.sessions):
                if key not in want and key not in child:
                    del self.sessions[key]
                    changed = True
            for key, c in child.items():
                if key not in self.sessions:
                    fs = self.src.root if c["source"] == "claude-root" else self.src.local
                    self.sessions[key] = (CodexSession if c["agent"] == "codex" else ClaudeSession)(fs, c["path"])
                    self.source_of[key] = c["source"]
                    changed = True
            titles = self.src.codex_titles()
            for key, s in self.sessions.items():
                if isinstance(s, CodexSession):
                    s.thread_name = titles.get(s.id)
                changed |= s.poll()
            self._follow_continuations(cands)
            if changed:
                self.version += 1
            self.last_refresh = time.time()
            return changed

    def _follow_continuations(self, cands: list[dict]) -> None:
        """등록한 세션이 새 ID로 이어졌으면 등록을 새 세션으로 옮긴다. 다음 갱신부터 새 파일을 읽는다."""
        known = {c["key"] for c in cands}
        for key, s in list(self.sessions.items()):
            nxt = isinstance(s, ClaudeSession) and s.main.meta.get("continued_in")
            if not nxt or key not in self.tracked.data["sessions"]:
                continue
            new_key = key.split(":", 1)[0] + ":" + nxt
            if new_key in known:
                try:
                    self.tracked.replace(key, new_key)
                except RuntimeError:
                    pass

    def _child_candidates(self) -> dict[str, dict]:
        """등록한 대화형 Claude 세션이 실행한 하위 세션.

        `codex exec`(originator codex_exec)와 헤드리스 Claude 세션(claude -p) 중에서
        부모와 같은 폴더(또는 그 아래)에서, 부모가 활동하던 동안 시작된 것을 하위 세션으로 본다.
        하위 세션이 따로 등록돼 있어도 부모 보드 안에 표시한다.
        """
        out = {}
        windows = []
        for key, s in self.sessions.items():
            if isinstance(s, ClaudeSession) and not s.headless:
                cwds = [c.rstrip("/") for c in (s.main.cwd_counts or {}) if c] or [(s.main.meta.get("cwd") or "").rstrip("/")]
                a, b = parse_ts(s.main.first_ts), parse_ts(s.main.last_ts)
                if any(cwds) and a and b:
                    windows.append((key, cwds, a, max(b, s.mtime or 0) + 120))
        parents = {w[0] for w in windows}
        cands = [c for c in getattr(self, "_cands", [])
                 if c["key"] not in parents and (c.get("entry") == "codex_exec" or c.get("headless"))]
        cands.sort(key=lambda c: c.get("first_ts") or "")
        # 시작 순서대로 연결한다. 하위 세션이 활동하는 동안은 부모의 활동 시간도 늘어난 것으로 본다.
        # 밤새 스크립트가 claude -p를 차례로 새로 실행해도 같은 부모에 연결된다.
        ends = {key: b for key, _, _, b in windows}
        for c in cands:
            started, cwd = parse_ts(c.get("first_ts")), (c.get("cwd") or "").rstrip("/")
            if not started or not cwd:
                continue
            # 폴더가 가장 가까운 부모를 고르고, 같으면 가장 최근에 시작한 부모를 고른다.
            # 상위 폴더에서 실행한 세션이 다른 작업의 하위 세션을 가져가지 않게 한다.
            best = None
            for key, pcwds, a, _ in windows:
                near = max((len(p) for p in pcwds if p and (cwd == p or cwd.startswith(p + "/"))), default=-1)
                if near >= 0 and a <= started <= ends[key]:
                    rank = (near, a)
                    if best is None or rank > best[1]:
                        best = (key, rank)
            if best:
                out[c["key"]] = {**c, "parent": best[0]}
                ends[best[0]] = max(ends[best[0]], (c.get("mtime") or started) + 120)
        self.child_parent = {k: v["parent"] for k, v in out.items()}
        return out

    # ---------- 화면용 JSON ----------

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            today = today_prefix()
            sessions = []
            child_parent = getattr(self, "child_parent", {})
            children: dict[str, list] = {}
            for key, s in self.sessions.items():
                if key in child_parent:
                    children.setdefault(child_parent[key], []).append((key, s))
            self._scan_processes(now)
            self._show_last_message = load_config().get("show_last_message", True) is not False
            try:
                self._app = app_states()
            except OSError:
                self._app = {}
            for key, s in self.sessions.items():
                if key in child_parent:
                    continue
                if isinstance(s, ClaudeSession):
                    sessions.append(self._claude_view(key, s, now, today, children.get(key, [])))
                else:
                    sessions.append(self._codex_view(key, s, now, today))
            self._apply_completed(sessions)
            sessions.sort(key=lambda d: (STATUS_ORDER.index(d["status"]), -(d["last_activity"] or 0)))
            summary = {
                "done": sum(1 for d in sessions if d["status"] == "done"),
                "waiting": sum(1 for d in sessions if d["status"] in ("needs_input", "maybe_waiting")),
                "running": sum(1 for d in sessions if d["status"] == "running"),
                "idle": sum(1 for d in sessions if d["status"] == "idle"),
                "tracked": len(sessions),
                "tokens_today": sum(d["tokens_today"] for d in sessions),
            }
            root = self.src.root
            return {
                "generated_at": now,
                "host": host_info(),
                "version": self.version,
                "summary": summary,
                "sessions": sessions,
                "root_status": None if root is None else {"ok": root.ok, "error": root.error},
                "tracked_dirs": self.tracked.data["dirs"],
            }

    def _last_message(self, status: str, text: str | None, ts, cut: bool) -> dict | None:
        """입력 차례일 때 에이전트가 쓴 마지막 응답. 사용자 입력은 넣지 않는다. 설정 show_last_message로 끈다."""
        if not text or status not in ("idle", "done") or not self._show_last_message:
            return None
        return {"text": text, "ts": parse_ts(ts), "truncated": cut}

    def _apply_completed(self, sessions: list[dict]) -> None:
        """사용자가 완료로 표시한 세션은 상태를 완료로 바꾼다. 표시 뒤 새 활동이 있으면 다시 연다."""
        done = self.tracked.data.get("completed") or {}
        for d in sessions:
            ts = done.get(d["key"])
            if not ts:
                continue
            if (d.get("last_activity") or 0) > ts + COMPLETE_GRACE_SEC:
                try:
                    self.tracked.set_completed(d["key"], False)
                except RuntimeError:
                    pass
                d["reopened_at"] = ts
                continue
            d.update(status="done", status_label="완료 표시됨", eta=None, completed_at=ts, since=ts)

    def _scan_processes(self, now: float) -> None:
        """세션을 실행 중인 프로세스와 멀티플렉서. 데스크톱 앱에서 연 Codex 세션은 앱 서버가 실행하므로 찾지 않는다."""
        if now - self._proc_at < PROC_SCAN_SEC:
            return
        self._proc_at = now
        items = []
        for key, s in self.sessions.items():
            if isinstance(s, ClaudeSession):
                items.append({"key": key, "agent": "claude", "cwd": s.main.cwd_first or s.main.meta.get("cwd"), "first_ts": parse_ts(s.main.first_ts)})
            elif launch_of("codex", s.meta.get("originator"), s.meta.get("source")) == "cli":
                items.append({"key": key, "agent": "codex", "cwd": s.meta.get("cwd"), "first_ts": parse_ts(s.first_ts)})
        try:
            self._proc_info = procs.session_processes(items)
        except Exception:  # 프로세스 정보는 부가 정보라 실패해도 화면은 계속 갱신한다
            self._proc_info = {}

    def _claude_view(self, key, s: ClaudeSession, now, today, children) -> dict:
        m = s.main.meta
        source = self.source_of.get(key, "claude")
        # 기록 시각은 응답 생성 시작 시각이라 늦게 쓰일 수 있다. 파일 수정 시각도 활동으로 본다.
        sub_activity = max(s.sub_mtime or 0, s.mtime or 0)
        status, since, detail = claude_status(s.main, now, sub_activity, s.headless)
        if source == "claude":
            status, since, detail = apply_app_state(status, since, detail, self._app.get(s.id))
        nodes = []
        child_prog: dict[str, list[float]] = {}
        main_tokens = s.main.tokens()
        total_tokens = dict(main_tokens)
        tokens_today = s.main.tokens_since(today)
        # 서브에이전트 노드
        call_index = s.main.agent_calls
        for aid, t in sorted(s.subs.items(), key=lambda kv: kv[1].first_ts or ""):
            meta = s.sub_meta.get(aid, {})
            parent = "main"
            call = call_index.get(meta.get("toolUseId"))
            if not call and meta.get("parentAgentId"):
                parent = "sub:" + meta["parentAgentId"]
                pt = s.subs.get(meta["parentAgentId"])
                call = pt.agent_calls.get(meta.get("toolUseId")) if pt else None
            finished = bool(meta.get("finished"))
            if finished:
                st = "done"
            else:
                st, _, _ = claude_status(t, now)
                if st == "idle":
                    st = "done"
            prog = progress(t.plan.steps, now=now)
            if call and call.get("step"):
                child_prog.setdefault(call["step"], []).append(1.0 if st == "done" else (prog if prog is not None else 0.5))
            tok = t.tokens()
            for k in total_tokens:
                total_tokens[k] += tok.get(k, 0)
            tokens_today += t.tokens_since(today)
            nodes.append({
                "id": "sub:" + aid, "parent": parent, "depth": int(meta.get("spawnDepth") or 1),
                "kind": "subagent", "type": meta.get("agentType") or (call or {}).get("type") or "agent",
                "description": meta.get("description") or (call or {}).get("description") or "",
                "step": (call or {}).get("step"),
                "status": st, "status_label": STATUS_LABEL[st],
                "started": parse_ts(t.first_ts), "last": parse_ts(t.last_ts),
                "tokens": tok["io"], "context_pct": _pct(t.context, context_window(t.model)),
                "tools": t.tool_count, "error_count": t.error_total,
                "steps": _steps_view(t.plan.steps), "progress": prog,
                "current": (t.last_tool or {}).get("summary") if st != "done" else None,
                "errors": t.errors[-3:], "model": t.model,
            })
        for ckey, c in children:
            if isinstance(c, CodexSession):
                cst = _codex_status(c, now)
                ctok, ctx = c.tokens(), _pct(c.context(), c.context_window or 0)
                ctype, ctools, cerr, cplan = "codex exec", c.tool_count, len(c.errors), c.plan
                cfirst, clast, cerrors = c.first_ts, c.last_ts, c.errors
                tokens_today += ctok["io"] if (c.last_ts or "") >= today else 0
            else:
                cst, _, _ = claude_status(c.main, now, max(c.mtime or 0, c.sub_mtime or 0), c.headless)
                ctok, ctx = c.main.tokens(), _pct(c.main.context, context_window(c.main.model))
                ctype, ctools, cerr, cplan = "claude -p", c.main.tool_count, c.main.error_total, c.main.plan
                cfirst, clast, cerrors = c.main.first_ts, c.main.last_ts, c.main.errors
                tokens_today += c.main.tokens_since(today)
            if cst == "idle":
                cst = "done"
            for k in total_tokens:
                total_tokens[k] += ctok.get(k, 0)
            last_tool = c.last_tool if isinstance(c, CodexSession) else c.main.last_tool
            nodes.append({
                "id": ckey, "parent": "main", "depth": 1, "kind": "session",
                "type": ctype, "description": c.title(),
                "step": None, "status": cst, "status_label": STATUS_LABEL[cst],
                "started": parse_ts(cfirst), "last": parse_ts(clast),
                "tokens": ctok["io"], "context_pct": ctx, "tools": ctools, "error_count": cerr,
                "steps": _steps_view(cplan.steps), "progress": progress(cplan.steps, now=now),
                "current": (last_tool or {}).get("summary") if cst not in ("done",) else None,
                "errors": cerrors[-3:], "model": c.model if isinstance(c, CodexSession) else c.main.model,
                "process": self._proc_info.get(ckey),
            })
        cp = {k: sum(v) / len(v) for k, v in child_prog.items()}
        steps = s.main.plan.steps
        # 지난 턴의 계획이 끝났고 지금 턴에서는 계획을 세우지 않았다면 지난 계획은 보여주지 않는다.
        # 실행 중인 세션이 100%로 보여 끝난 것처럼 읽히지 않게 한다.
        if s.main.plan.finished_before(s.main.last_turn_ts):
            steps = []
        prog = progress(steps, cp, now=now)
        active = status in ("running", "needs_input", "maybe_waiting", "stalled")
        eta_range = eta(steps, now) if active and prog is not None and prog < 1 else None
        streak_tool, streak = s.main.error_streak
        alerts = []
        if streak >= 3:
            alerts.append({"kind": "error", "text": f"{streak_tool} 연속 오류 {streak}회"})
        cpct = _pct(s.main.context, context_window(s.main.model))
        if cpct is not None and cpct >= 85:
            alerts.append({"kind": "context", "text": f"컨텍스트 {cpct}%"})
        timeline = [{"ts": parse_ts(r["ts"]), "kind": "plan",
                     "text": _revision_text(r), "delta_sec": (r["points_after"] - r["points_before"]) * PRIOR_SEC_PER_POINT}
                    for r in s.main.plan.revisions]
        timeline += [{"ts": parse_ts(e["ts"]), "kind": "error", "text": f"{e['tool']}: {e['text']}"} for e in s.main.errors]
        timeline.sort(key=lambda x: x["ts"] or 0)
        main_status, main_label = status, STATUS_LABEL[status]
        board_label = main_label
        bg = [b for b in s.main.background.values() if now - (parse_ts(b["ts"]) or 0) < BACKGROUND_MAX_SEC]
        kid_states = [n["status"] for n in nodes if n["status"] in ("needs_input", "maybe_waiting", "running")
                      and now - (n["last"] or 0) < BACKGROUND_MAX_SEC]
        if status == "idle" and (kid_states or bg):
            # 메인 에이전트는 응답을 마쳤지만 백그라운드 작업이나 하위 작업이 남아 있다.
            status = next((x for x in ("needs_input", "maybe_waiting", "running") if x in kid_states), "running")
            board_label = STATUS_LABEL[status] + " (백그라운드)"
            since = now
            n_bg = len(bg) + len(kid_states)
            first = bg[0]["summary"] if bg else ""
            detail = f"백그라운드 작업 {n_bg}개 진행 중" + (f". {first}" if first else "")
        entry = m.get("entrypoint")
        # 재개는 세션을 시작한 폴더에서 해야 이어진다. 카드에는 많이 쓴 작업공간을 보여준다.
        folder = main_folder(s.main.cwd_first, s.main.cwd_counts) or m.get("cwd")
        cmd, note = resume_command("claude", source, s.id, s.main.cwd_first or m.get("cwd"), entry, status)
        nodes.insert(0, {
            "id": "main", "parent": None, "depth": 0, "kind": "main", "type": "메인 에이전트",
            "description": "", "step": None, "status": main_status, "status_label": main_label,
            "started": parse_ts(s.main.first_ts), "last": parse_ts(s.main.last_ts),
            "tokens": main_tokens["io"], "context_pct": cpct, "steps": _steps_view(steps), "progress": prog,
            "tools": s.main.tool_count, "error_count": s.main.error_total,
            "current": detail or ((s.main.last_tool or {}).get("summary") if status == "running" else None),
            "errors": s.main.errors[-3:], "model": s.main.model, "compactions": s.main.compactions,
        })
        return {
            "key": key, "agent": "claude", "source": source, "id": s.id,
            "user": "root" if source == "claude-root" else self.src.local.user,
            "entry": entry, "launch": launch_of("claude", entry, headless=s.headless),
            "title": s.title(), "workspace": workspace_name(folder),
            "cwd": folder, "start_cwd": s.main.cwd_first, "branch": m.get("gitBranch"), "version": m.get("version"),
            "status": status, "status_label": board_label, "since": since, "detail": detail,
            "started": parse_ts(s.main.first_ts), "last_activity": max(parse_ts(s.main.last_ts) or 0, sub_activity) or None,
            "summary": m.get("away_summary"),
            "tokens": total_tokens, "tokens_today": tokens_today, "cost": _float(s.main.cost),
            "progress": prog, "eta": eta_range, "alerts": alerts, "timeline": timeline[-20:],
            "resume": cmd, "resume_note": note, "nodes": nodes, "files": sorted(s.main.files_touched, key=s.main.files_touched.get)[-30:],
            "model": s.main.model, "process": self._proc_info.get(key),
            "session_steps": list(s.main.plan.session_counts()),
            "activity": activity(s.main.tool_times, s.main.last_turn_ts, now),
            "last_message": self._last_message(status, s.main.last_text, s.main.last_text_ts, s.main.last_text_cut),
        }

    def _codex_view(self, key, s: CodexSession, now, today) -> dict:
        status = _codex_status(s, now)
        tok = s.tokens()
        steps = s.plan.steps
        prog = progress(steps, now=now)
        cmd, note = resume_command("codex", "codex", s.id, s.meta.get("cwd"), s.meta.get("originator"), status)
        cpct = _pct(s.context(), s.context_window or 0)
        timeline = [{"ts": parse_ts(r["ts"]), "kind": "plan", "text": _revision_text(r),
                     "delta_sec": (r["points_after"] - r["points_before"]) * PRIOR_SEC_PER_POINT} for r in s.plan.revisions]
        timeline += [{"ts": parse_ts(e["ts"]), "kind": "error", "text": f"{e['tool']}: {e['text']}"} for e in s.errors]
        timeline.sort(key=lambda x: x["ts"] or 0)
        node = {
            "id": "main", "parent": None, "depth": 0, "kind": "main", "type": "메인 에이전트", "description": "",
            "step": None, "status": status, "status_label": STATUS_LABEL[status],
            "started": parse_ts(s.first_ts), "last": parse_ts(s.last_ts), "tokens": tok["io"],
            "tools": s.tool_count, "error_count": len(s.errors),
            "context_pct": cpct, "steps": _steps_view(steps), "progress": prog,
            "current": (s.last_tool or {}).get("summary") if status == "running" else None,
            "errors": s.errors[-3:], "model": s.model,
        }
        return {
            "key": key, "agent": "codex", "source": "codex", "id": s.id, "user": self.src.local.user,
            "entry": s.meta.get("originator"), "launch": launch_of("codex", s.meta.get("originator"), s.meta.get("source")),
            "title": s.title(), "workspace": workspace_name(s.meta.get("cwd")),
            "cwd": s.meta.get("cwd"), "branch": None, "version": s.meta.get("cli_version"),
            "status": status, "status_label": STATUS_LABEL[status],
            "since": parse_ts(s.turn_started) if status == "running" else parse_ts(s.last_ts), "detail": None,
            "started": parse_ts(s.first_ts), "last_activity": parse_ts(s.last_ts),
            "summary": short(s.last_message or "", 400) or None,
            "tokens": tok, "tokens_today": tok["io"] if (s.last_ts or "") >= today else 0, "cost": None,
            "progress": prog, "eta": eta(steps, now) if steps and status in ("running", "maybe_waiting", "stalled") else None,
            "alerts": [{"kind": "context", "text": f"컨텍스트 {cpct}%"}] if cpct and cpct >= 85 else [],
            "timeline": timeline[-20:], "resume": cmd, "resume_note": note, "nodes": [node], "files": [],
            "model": s.model, "process": self._proc_info.get(key),
            "session_steps": list(s.plan.session_counts()),
            "activity": activity(s.tool_times, s.turn_started, now),
            "last_message": self._last_message(status, s.last_message, s.last_ts, False),
        }


def _codex_status(s: CodexSession, now: float) -> str:
    last = parse_ts(s.last_ts) or 0
    if s.turn_open:
        if now - last > STALL_SEC:
            return "stalled"
        if s.pending and now - last > WAIT_GUESS_SEC:
            return "maybe_waiting"
        return "running"
    return "idle"


def _rollout_ts(path: str) -> str | None:
    base = os.path.basename(path)
    # rollout-2026-10-29T10-40-30-<uuid>.jsonl, 시각은 로컬 시간 기준이 아니라 UTC로 기록된다.
    try:
        stamp = base[len("rollout-"):len("rollout-") + 19]
        d, t = stamp.split("T")
        return f"{d}T{t.replace('-', ':')}Z"
    except ValueError:
        return None


def _pct(used: int, window: int) -> int | None:
    if not used or not window:
        return None
    return min(100, round(used * 100 / window))


def _float(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _steps_view(steps: list[dict]) -> list[dict]:
    return [{"content": s["content"], "size": s.get("size"), "status": s["status"],
             "started": parse_ts(s.get("started")), "ended": parse_ts(s.get("ended"))} for s in steps]


def _revision_text(r: dict) -> str:
    parts = []
    if r["added"]:
        parts.append("추가: " + ", ".join(short(x, 40) for x in r["added"][:3]))
    if r["removed"]:
        parts.append("제거: " + ", ".join(short(x, 40) for x in r["removed"][:3]))
    return "계획 변경. " + "; ".join(parts) if parts else "계획 변경"
