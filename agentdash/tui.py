"""터미널 화면(TUI). 웹 화면과 같은 카드 구성을 curses로 그린다.

서버 없이 이 프로세스가 직접 기록을 읽는다. 등록 목록(~/.agentdash/tracked.json)은 웹 화면과 함께 쓴다.
정렬, 보기, 직접 배치 순서는 ~/.agentdash/tui.json에 저장한다.
"""

from __future__ import annotations

import base64
import curses
import json
import os
import sys
import threading
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdash.model import STATE_DIR, Registry  # noqa: E402

REFRESH_SEC = 2.0
PREFS = STATE_DIR / "tui.json"
CARD_MIN = 44
LEFT_W = 42  # 가로형에서 왼쪽 칸 너비
SORTS = [("recent", "최근 실행순"), ("name", "이름순"), ("agent", "에이전트별"), ("custom", "직접 배치")]
AGENTS = [("all", "전체"), ("claude", "Claude"), ("codex", "Codex")]
LAYOUTS = [("grid", "세로형"), ("list", "가로형")]
FILTERS = [("all", "전체"), ("wait", "대기"), ("running", "실행 중"), ("idle", "입력 차례"), ("done", "완료")]
SPIN = "◐◓◑◒"
# 화면 아래 키 막대. 자주 쓰는 순서로 둔다.
KEY_BAR = [("a", "세션 관리"), ("x", "해제"), ("d", "완료 표시"), ("Enter", "펼치기"), ("c", "재개 명령 복사"), ("r", "새로고침"),
           ("s", "정렬"), ("[ ]", "옮기기"), ("v", "세로/가로"), ("f", "상태 필터"), ("g", "Claude/Codex"),
           ("←↑→↓", "이동"), ("?", "도움말"), ("q", "종료")]
# ? 도움말 창
HELP = [
    ("←↑→↓, h j k l", "카드 선택 이동"),
    ("Enter, Space", "선택한 카드 펼치기, 접기 (계획 단계, 요약, 경로, 토큰, 이력)"),
    ("a", "세션 관리: 세션 등록과 해제 (Enter 등록/해제, Esc 닫기)"),
    ("x", "선택한 세션 트래킹 해제 (확인 창에서 y 확인, n 취소)"),
    ("d", "선택한 세션을 완료로 표시, 다시 누르면 다시 열기 (새 활동이 생기면 자동으로 다시 열림)"),
    ("c", "선택한 세션의 이어서 작업하기 명령 복사"),
    ("r", "지금 바로 다시 읽기 (평소에는 2초마다 자동으로 갱신)"),
    ("s", "정렬 바꾸기: 최근 실행순, 이름순, 에이전트별, 직접 배치"),
    ("[ ]", "선택한 카드를 앞, 뒤로 옮기기 (직접 배치로 바뀜)"),
    ("v", "세로형, 가로형 보기 바꾸기"),
    ("f", "상태 필터: 전체, 대기, 실행 중, 입력 차례, 완료"),
    ("g", "에이전트 필터: 전체, Claude, Codex"),
    ("?", "이 도움말"),
    ("q", "종료"),
]


# ---------- 글자 폭 ----------

def cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(s: str) -> int:
    return sum(cw(c) for c in s)


def cut(s: str, w: int) -> str:
    """표시 폭 w에 맞게 자른다. 잘리면 끝에 '…'을 붙인다."""
    if width(s) <= w:
        return s
    out, n = [], 0
    for c in s:
        if n + cw(c) > w - 1:
            break
        out.append(c)
        n += cw(c)
    return "".join(out) + "…"


# ---------- 포맷 ----------

def fmt_tok(n) -> str:
    n = n or 0
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{round(n / 1e3)}k"
    return str(n)


def fmt_dur(sec) -> str:
    if sec is None:
        return ""
    sec = max(0, round(sec))
    if sec < 60:
        return f"{sec}초"
    m = round(sec / 60)
    if m < 60:
        return f"{m}분"
    h, r = divmod(m, 60)
    if h < 24:
        return f"{h}시간 {r}분" if r else f"{h}시간"
    return f"{h // 24}일 {h % 24}시간"


def fmt_date(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def display_name(ws: str) -> str:
    import re
    return re.sub(r"^\d{6}[-_]", "", ws or "") or ws or ""


# ---------- 색 ----------

C = {}


def init_colors() -> None:
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    rich = curses.COLORS >= 256
    spec = {
        "blue": (75 if rich else curses.COLOR_BLUE, bg),
        "amber": (179 if rich else curses.COLOR_YELLOW, bg),
        "red": (203 if rich else curses.COLOR_RED, bg),
        "teal": (37 if rich else curses.COLOR_CYAN, bg),
        "green": (71 if rich else curses.COLOR_GREEN, bg),
        "gray": (245 if rich else curses.COLOR_WHITE, bg),
        "violet": (141 if rich else curses.COLOR_MAGENTA, bg),
        "coral": (209 if rich else curses.COLOR_RED, bg),
        "tab_claude": (231 if rich else curses.COLOR_WHITE, 173 if rich else curses.COLOR_RED),
        "tab_codex": (231 if rich else curses.COLOR_WHITE, 30 if rich else curses.COLOR_CYAN),
        "resume": (114 if rich else curses.COLOR_GREEN, 236 if rich else curses.COLOR_BLACK),
        "title": (99 if rich else curses.COLOR_MAGENTA, bg),
        "idle_a": (37 if rich else curses.COLOR_CYAN, bg),
        "idle_b": (179 if rich else curses.COLOR_YELLOW, bg),
        "modal": (231 if rich else curses.COLOR_WHITE, 238 if rich else curses.COLOR_BLUE),
        "headline": (231 if rich else curses.COLOR_WHITE, 61 if rich else curses.COLOR_BLUE),
        "keycap": (17 if rich else curses.COLOR_BLACK, 153 if rich else curses.COLOR_CYAN),
    }
    for i, (name, (fg, b)) in enumerate(spec.items(), start=1):
        try:
            curses.init_pair(i, fg, b)
            C[name] = curses.color_pair(i)
        except curses.error:
            C[name] = 0


def col(name: str) -> int:
    return C.get(name, 0)


STATUS_COLOR = {"running": "blue", "needs_input": "amber", "maybe_waiting": "amber", "stalled": "red",
                "idle": "teal", "done": "green", "unreadable": "gray"}


def prog_color(p) -> str:
    if p is None:
        return "gray"
    if p >= 1:
        return "green"
    if p >= .75:
        return "teal"
    if p >= .5:
        return "blue"
    if p >= .25:
        return "amber"
    return "coral"


def light(status: str, tick: int) -> tuple[str, int]:
    color = col(STATUS_COLOR.get(status, "gray"))
    if status == "running":
        return SPIN[tick % len(SPIN)], color | curses.A_BOLD
    if status == "needs_input":
        return "?", color | (curses.A_BOLD if tick % 2 else curses.A_DIM)
    if status == "maybe_waiting":
        return "…", color
    if status == "stalled":
        return "!", color | curses.A_BOLD
    if status == "done":
        return "✓", color
    return "●", color


# ---------- 카드 내용 ----------
# 한 줄은 [(글자, 속성), ...] 목록이다.

def bar(p, w: int) -> list:
    w = max(4, w)
    n = round(min(1, max(0, p or 0)) * w)
    return [("█" * n, col(prog_color(p))), ("░" * (w - n), col("gray") | curses.A_DIM)]


def mux_text(proc) -> str:
    if not proc or proc.get("desktop"):
        return ""
    if not proc.get("mux"):
        return "멀티플렉서 없음"
    return proc["mux"] + (f": {proc['mux_session']}" if proc.get("mux_session") else "")


def left_lines(s: dict, w: int, tick: int, host: dict | None = None) -> list:
    dim = col("gray")
    sym, sattr = light(s["status"], tick)
    # 맨 위: 세션 제목을 바탕색으로 강조한다. 제목이 없으면 작업 폴더 이름을 쓴다.
    title = cut(s.get("title") or display_name(s.get("workspace")) or "제목 없음", w - 2)
    out = [[(" " + title + " " * max(0, w - 1 - width(title)), col("headline") | curses.A_BOLD)]]
    # 바로 아래: 진행률
    if s["status"] == "done" and s.get("completed_at"):
        out.append([(f"✓ {fmt_date(s['completed_at'])}에 완료로 표시함", col("green") | curses.A_BOLD)])
    elif s.get("progress") is not None:
        pct = f" {round(s['progress'] * 100)}%"
        out.append(bar(s["progress"], w - len(pct)) + [(pct, curses.A_BOLD)])
    else:
        # 계획이 없으면 퍼센트 대신 활동량을 보여준다
        a = s.get("activity") or {}
        bits = []
        if a.get("turn_started"):
            end = time.time() if s["status"] == "running" else (s.get("last_activity") or time.time())
            bits.append(("이번 턴 " if s["status"] == "running" else "지난 턴 ") + fmt_dur(end - a["turn_started"]) + ("째" if s["status"] == "running" else ""))
        if a.get("turn_tools"):
            bits.append(f"도구 {a['turn_tools']}회")
        if s["status"] == "running" and a.get("recent_tools") is not None:
            bits.append(f"최근 10분 {a['recent_tools']}회")
        out.append([(cut(", ".join(bits) if bits else "계획 단계 없음", w), dim)])
    ss = s.get("session_steps") or [0, 0]
    shown = len((s.get("nodes") or [{}])[0].get("steps") or [])
    if ss[1] and ss[1] != shown:
        out.append([(cut(f"세션 누적 {ss[0]}/{ss[1]} 단계", w), dim)])
    if host:
        out.append([("서버 ", dim), (cut(host.get("ip") or host.get("name") or "", w - 5), curses.A_BOLD),
                    (cut(f"  {host['name']}" if host.get("ip") and host.get("name") else "", max(0, w - 21)), dim)])
    out += [
        [(f"세션 시작 : {fmt_date(s.get('started'))}", dim)],
        [(f"마지막 실행 : {fmt_date(s.get('last_activity'))}", dim)],
        [(sym + " ", sattr), (cut(display_name(s.get("workspace")), w - 2), curses.A_BOLD)],
    ]
    st = [(cut(s.get("status_label") or "", w), col(STATUS_COLOR.get(s["status"], "gray")) | curses.A_BOLD)]
    out.append(st)
    extra = []
    if s.get("eta"):
        e = s["eta"]
        extra.append("곧 완료" if e[1] <= 0 else f"남은 {fmt_dur(e[0])}~{fmt_dur(e[1])}")
    if s.get("user") == "root":
        extra.append("root")
    mt = mux_text(s.get("process"))
    if mt:
        extra.append(mt)
    if extra:
        out.append([(cut("  ".join(extra), w), col("violet"))])
    t = s.get("tokens") or {}
    nodes = s.get("nodes") or []
    kids = [n for n in nodes if n["id"] != "main"]
    stats = f"토큰 {fmt_tok(t.get('io', t.get('total')))}"
    if kids:
        stats += f"   하위 {sum(1 for n in kids if n['status'] == 'done')}/{len(kids)}"
    for a in s.get("alerts") or []:
        stats += f"   {a['text']}"
    out.append([(cut(stats, w), dim)])
    return out


def resume_lines(s: dict, w: int, full: bool) -> list:
    """이어서 작업하기 명령. full이면 줄을 나눠 전체를, 아니면 한 줄로 줄여 보여준다."""
    cmd = s.get("resume") or ""
    if not cmd:
        return []
    out = [[("이어서 작업하기", curses.A_BOLD), ("  c 복사" if full else "  선택하면 전체 표시, c 복사", col("gray"))]]
    if not full:
        piece = cut(cmd, w - 2)
        return out + [[(" " + piece + " " * max(0, w - 1 - width(piece)), col("resume"))]]
    while cmd:
        piece, n = "", 0
        for ch in cmd:
            if n + cw(ch) > w - 2:
                break
            piece += ch
            n += cw(ch)
        cmd = cmd[len(piece):]
        out.append([(" " + piece + " " * (w - 1 - width(piece)), col("resume"))])
    if s.get("resume_note"):
        out.append([(cut(s["resume_note"], w), col("amber"))])
    return out


def right_lines(s: dict, w: int, tick: int, expanded: bool, max_kids: int = 8, selected: bool = False) -> list:
    dim = col("gray")
    nodes = s.get("nodes") or []
    main = next((n for n in nodes if n["id"] == "main"), nodes[0] if nodes else {})
    out = []
    sym, sattr = light(main.get("status", "idle"), tick)
    head = [(sym + " ", sattr), (" 메인 에이전트 ", col("blue") | curses.A_REVERSE | curses.A_BOLD)]
    if main.get("model"):
        head.append((cut(f"  {main['model']}", max(0, w - 18)), dim))
    out.append(head)
    steps = main.get("steps") or []
    if steps:
        done = sum(1 for x in steps if x["status"] == "completed")
        line = []
        for x in steps:
            ch = "■" if x["status"] == "completed" else ("▶" if x["status"] == "in_progress" else "□")
            c = "green" if x["status"] == "completed" else ("blue" if x["status"] == "in_progress" else "gray")
            line.append((ch, col(c)))
        line = line[: max(1, w - 8)]
        line.append((f" {done}/{len(steps)}", dim))
        out.append(line)
    cur = next((x for x in steps if x["status"] == "in_progress"), None)
    now_text = cur["content"] if cur else main.get("current")
    if now_text:
        out.append([("▸ ", col("blue")), (cut(now_text, w - 2), 0)])
    ctx = main.get("context_pct")
    out.append([(cut(f"컨텍스트 {ctx if ctx is not None else 0}%   도구 {main.get('tools') or 0}"
                     + (f"   오류 {main['error_count']}" if main.get("error_count") else ""), w), dim)])
    by_parent: dict = {}
    for n in nodes:
        if n["id"] != "main":
            by_parent.setdefault(n.get("parent") or "main", []).append(n)
    rows = []

    def walk(pid, depth):
        for n in by_parent.get(pid, []):
            rows.append((n, depth))
            walk(n["id"], depth + 1)
    walk("main", 0)
    for n, depth in rows[:max_kids]:
        ksym, kattr = light(n["status"], tick)
        role = " 하위 세션 " if n.get("kind") == "session" else " 서브 에이전트 "
        rcol = col("teal") if n.get("kind") == "session" else col("violet")
        mt = mux_text(n.get("process"))
        label = n.get("type") or ""
        desc = n.get("description") or ""
        tail = f" {fmt_tok(n.get('tokens'))}"
        room = w - 2 * depth - 2 - width(role) - 1 - width(tail)
        body = cut(f"{label} {desc}".strip() + (f" [{mt}]" if mt else ""), max(4, room))
        out.append([("  " * depth, 0), (ksym + " ", kattr), (role, rcol | curses.A_REVERSE), (" " + body, 0),
                    (" " * max(0, room - width(body)), 0), (tail, dim)])
    if len(rows) > max_kids:
        out.append([(f"  하위 {len(rows) - max_kids}개 더 (Enter로 펼치기)", dim)])
    out.append([("", 0)])
    out += resume_lines(s, w, full=selected or expanded)
    if expanded:
        out.append([("", 0)])
        if steps:
            out.append([("계획 단계", curses.A_BOLD)])
            for i, x in enumerate(steps, 1):
                c = "green" if x["status"] == "completed" else ("blue" if x["status"] == "in_progress" else "gray")
                out.append([(cut(f" {i}. {x['content']}", w), col(c))])
        if s.get("summary"):
            out.append([("요약", curses.A_BOLD)])
            out.append([(cut(s["summary"], w), dim)])
        out.append([("경로", curses.A_BOLD)])
        out.append([(cut(s.get("cwd") or "", w), dim)])
        t = s.get("tokens") or {}
        out.append([(cut(f"토큰 입력 {fmt_tok(t.get('input'))} 출력 {fmt_tok(t.get('output'))} "
                         f"캐시 읽기 {fmt_tok(t.get('cache_read'))} 캐시 쓰기 {fmt_tok(t.get('cache_write'))}", w), dim)])
        for x in (s.get("timeline") or [])[-5:]:
            c = "red" if x["kind"] == "error" else "violet"
            out.append([(cut(f"{time.strftime('%H:%M', time.localtime(x['ts'] or 0))} {x['text']}", w), col(c))])
    return out


# ---------- 앱 ----------

class App:
    def __init__(self, scr):
        self.scr = scr
        self.reg = Registry()
        self.snap: dict = {"sessions": [], "summary": {}}
        self.lock = threading.Lock()
        self.sel = 0
        self.scroll = 0
        self.expanded: set[str] = set()
        self.msg = ""
        self.msg_until = 0.0
        p = self._load_prefs()
        self.sort = p.get("sort", "recent")
        self.layout = p.get("layout", "grid")
        self.order = p.get("order", [])
        self.filter = "all"
        self.agent = p.get("agent", "all")
        self.boxes: list = []
        self.wake = threading.Event()  # r 키로 바로 다시 읽을 때 쓴다
        self.app: dict = {}
        threading.Thread(target=self._check_updates, daemon=True).start()
        self.dump_path = os.environ.get("AGENTDASH_TUI_DUMP")  # 화면 확인용. 화면 글자를 이 파일에 쓴다.
        self.shadow: list = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _load_prefs(self) -> dict:
        try:
            return json.loads(PREFS.read_text())
        except (OSError, ValueError):
            return {}

    def _save_prefs(self) -> None:
        try:
            tmp = PREFS.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sort": self.sort, "layout": self.layout, "order": self.order, "agent": self.agent}, ensure_ascii=False))
            os.replace(tmp, PREFS)
        except OSError:
            pass

    def _loop(self) -> None:
        while True:
            try:
                self.reg.refresh()
                snap = self.reg.snapshot()
                with self.lock:
                    self.snap = snap
            except Exception as e:  # 한 번 실패해도 화면은 계속 돈다
                self.flash(f"갱신 오류: {type(e).__name__}: {e}")
            self.wake.wait(REFRESH_SEC)
            self.wake.clear()

    def _check_updates(self) -> None:
        try:
            from agentdash import updates
            self.app = updates.info()
        except Exception:
            self.app = {}

    def flash(self, text: str, sec: float = 4) -> None:
        self.msg, self.msg_until = text, time.time() + sec

    # ----- 목록 -----
    def sessions(self) -> list:
        with self.lock:
            items = list(self.snap.get("sessions") or [])
        if self.agent != "all":
            items = [s for s in items if s.get("agent") == self.agent]
        f = self.filter
        if f == "wait":
            items = [s for s in items if s["status"] in ("needs_input", "maybe_waiting")]
        elif f == "running":
            items = [s for s in items if s["status"] in ("running", "stalled")]
        elif f == "idle":
            items = [s for s in items if s["status"] == "idle"]
        elif f == "done":
            items = [s for s in items if s["status"] == "done"]
        recent = lambda s: -(s.get("last_activity") or 0)  # noqa: E731
        if self.sort == "name":
            items.sort(key=lambda s: (display_name(s.get("workspace")).lower(), s.get("title") or "", recent(s)))
        elif self.sort == "agent":
            items.sort(key=lambda s: (s.get("agent") == "codex", recent(s)))
        elif self.sort == "custom":
            idx = {k: i for i, k in enumerate(self.order)}
            items.sort(key=lambda s: (idx.get(s["key"], -1), recent(s)))
        else:
            items.sort(key=recent)
        # 완료로 표시한 세션은 맨 뒤 (정렬은 그 안에서 유지)
        return [s for s in items if s["status"] != "done"] + [s for s in items if s["status"] == "done"]

    # ----- 그리기 -----
    def put(self, y: int, x: int, segs: list, maxw: int, last_row_ok: bool = False) -> None:
        h, W = self.scr.getmaxyx()
        if y < 0 or y >= (h if last_row_ok else h - 1) or x >= W:
            return
        if last_row_ok and y == h - 1:
            maxw = min(maxw, W - 1 - x)  # 맨 아래 줄 마지막 칸에 쓰면 curses가 오류를 낸다
        used = 0
        for text, attr in segs:
            if used >= maxw:
                break
            text = cut(text, maxw - used) if width(text) > maxw - used else text
            try:
                self.scr.addstr(y, x + used, text, attr)
            except curses.error:
                pass
            if self.dump_path:
                cx = x + used
                for c in text:
                    if cx < W:
                        self.shadow[y][cx] = c
                    if cw(c) == 2 and cx + 1 < W:
                        self.shadow[y][cx + 1] = ""
                    cx += cw(c)
            used += width(text)

    def card(self, y: int, x: int, w: int, s: dict, selected: bool, tick: int) -> int:
        """카드 하나를 그리고 높이를 돌려준다. y가 화면 밖이어도 높이 계산은 한다."""
        inner = w - 4
        expanded = s["key"] in self.expanded
        if self.layout == "list" and inner >= LEFT_W + 30:
            lw = LEFT_W
            rw = inner - lw - 3
            L = left_lines(s, lw, tick, self.snap.get("host"))
            R = right_lines(s, rw, tick, expanded, max_kids=99 if expanded else 6, selected=selected)
            n = max(len(L), len(R))
            body = []
            for i in range(n):
                left = L[i] if i < len(L) else []
                lwid = sum(width(t) for t, _ in left)
                body.append(left + [(" " * max(0, lw - lwid), 0), (" │ ", col("gray") | curses.A_DIM)] + (R[i] if i < len(R) else []))
        else:
            body = left_lines(s, inner, tick, self.snap.get("host")) + [[("", 0)]] + right_lines(s, inner, tick, expanded, max_kids=99 if expanded else 6, selected=selected)
        h = len(body) + 2
        # 테두리 색. 선택한 카드는 굵게, 사용자 차례인 카드는 두 색을 천천히 오간다.
        if s["status"] == "idle":
            battr = col("idle_a" if (tick // 3) % 2 == 0 else "idle_b") | curses.A_BOLD
        elif s["status"] in ("needs_input", "maybe_waiting"):
            battr = col("amber")
        elif s["status"] == "stalled":
            battr = col("red")
        else:
            battr = col("gray")
        if selected:
            battr = (battr if s["status"] in ("idle", "needs_input", "maybe_waiting", "stalled") else col("blue")) | curses.A_BOLD
        agent = s.get("agent")
        tab = f" {'Codex' if agent == 'codex' else 'Claude'} {s.get('launch') or ''} "
        top = [("╭─", battr), (tab, col("tab_codex" if agent == "codex" else "tab_claude") | curses.A_BOLD)]
        fill = w - 7 - width(tab)
        top += [("─" * max(0, fill), battr), (" x ", col("red") if selected else col("gray")), ("─╮", battr)]
        self.put(y, x, top, w)
        for i, line in enumerate(body, 1):
            self.put(y + i, x, [("│ ", battr)], 2)
            self.put(y + i, x + 2, line, inner)
            self.put(y + i, x + w - 2, [(" │", battr)], 2)
        self.put(y + h - 1, x, [("╰" + "─" * (w - 2) + "╯", battr)], w)
        return h

    def layout_boxes(self, items: list, W: int) -> list:
        """(세션, 열, 행 시작 y, 너비, x) 목록을 만든다. 높이는 그릴 때 정한다."""
        if self.layout == "list":
            return [(s, 0, W - 2, 1) for s in items]
        cols = max(1, (W - 1) // CARD_MIN)
        w = (W - 1) // cols
        return [(s, i % cols, w - 1, 1 + (i % cols) * w) for i, s in enumerate(items)]

    def draw(self, tick: int) -> None:
        scr = self.scr
        scr.erase()
        H, W = scr.getmaxyx()
        if self.dump_path:
            self.shadow = [[" "] * W for _ in range(H)]
        title = "Agent Dash"
        self.put(0, max(0, (W - width(title)) // 2), [(title, col("title") | curses.A_BOLD)], W)
        sub = "내 에이전트 진행 현황" + (f"  v{self.app['current']}" if self.app.get("current") else "")
        self.put(1, max(0, (W - width(sub)) // 2), [(sub, col("gray"))], W)
        sm = self.snap.get("summary") or {}
        info = (f"사용자 대기 {sm.get('waiting', 0)}   실행 중 {sm.get('running', 0)}   입력 차례 {sm.get('idle', 0)}   "
                f"오늘 토큰 {fmt_tok(sm.get('tokens_today'))}")
        self.put(2, max(0, (W - width(info)) // 2), [(info, curses.A_BOLD)], W)
        opts = (f"에이전트 {dict(AGENTS)[self.agent]}   필터 {dict(FILTERS)[self.filter]}   정렬 {dict(SORTS)[self.sort]}   보기 {dict(LAYOUTS)[self.layout]}")
        self.put(3, max(0, (W - width(opts)) // 2), [(opts, col("gray"))], W)

        items = self.sessions()
        self.sel = min(self.sel, max(0, len(items) - 1))
        top = 5
        bar = self.key_bar(W)
        view_h = H - top - len(bar) - 1
        # 행 단위로 배치한다. 가로형은 한 행에 카드 하나다.
        boxes = self.layout_boxes(items, W)
        cols = 1 if self.layout == "list" else max(1, (W - 1) // CARD_MIN)
        # 높이를 먼저 잰다 (화면 밖 좌표에 그려 높이만 얻는다)
        heights = []
        for s, c, w, x in boxes:
            heights.append(self._measure(s, w, tick, len(heights) == self.sel))
        rows_y, y = [], 0
        for r in range(0, len(boxes), cols):
            rows_y.append(y)
            y += max(heights[r:r + cols]) + 1
        if items:
            sel_row = self.sel // cols
            sy, sh = rows_y[sel_row], max(heights[sel_row * cols:sel_row * cols + cols])
            if sy < self.scroll:
                self.scroll = sy
            elif sy + sh > self.scroll + view_h:
                self.scroll = sy + sh - view_h
        else:
            self.scroll = 0
            empty = "등록한 세션이 없습니다. a를 눌러 세션 관리에서 등록하세요." if not (self.snap.get("sessions")) else "이 필터에 해당하는 세션이 없습니다."
            self.put(top + 2, max(0, (W - width(empty)) // 2), [(empty, col("gray"))], W)
        self.boxes = boxes
        for i, (s, c, w, x) in enumerate(boxes):
            ry = rows_y[i // cols] - self.scroll
            if ry + heights[i] < 0 or ry >= view_h:
                continue
            self._draw_clipped(top + ry, x, w, s, i == self.sel, tick, top, top + view_h)

        # 아래쪽: 알림 한 줄(메시지 또는 새 버전 안내)과 키 막대
        note_y = H - len(bar) - 1
        if self.msg and time.time() < self.msg_until:
            self.put(note_y, 1, [(cut(self.msg, W - 2), col("amber") | curses.A_BOLD)], W - 2)
        elif self.app.get("update"):
            up = f"새 버전(v{self.app['latest']})이 나왔습니다. q로 나간 뒤 agent-dash update"
            self.put(note_y, max(0, (W - width(up)) // 2), [(up, col("blue") | curses.A_BOLD)], W)
        for i, line in enumerate(bar):
            self.put(H - len(bar) + i, 1, line, W - 2, last_row_ok=True)
        scr.refresh()
        if self.dump_path:
            Path(self.dump_path).write_text("\n".join("".join(r).rstrip() for r in self.shadow) + "\n")

    def key_bar(self, W: int) -> list:
        """키 막대 줄 목록. 키는 색 상자로, 설명은 옆에 둔다. 너비가 모자라면 다음 줄로 넘긴다."""
        lines, line, used = [], [], 0
        for key, label in KEY_BAR:
            item = [(f" {key} ", col("keycap") | curses.A_BOLD), (f" {label}", 0), ("   ", 0)]
            n = sum(width(t) for t, _ in item)
            if line and used + n > W - 2:
                lines.append(line)
                line, used = [], 0
            line += item
            used += n
        if line:
            lines.append(line)
        return lines[:3]

    def help_window(self) -> None:
        H, W = self.scr.getmaxyx()
        kw = max(width(k) for k, _ in HELP) + 2
        w = min(W - 2, kw + max(width(d) for _, d in HELP) + 8)
        h = min(H - 2, len(HELP) + 5)
        win = curses.newwin(h, w, max(0, (H - h) // 2), max(0, (W - w) // 2))
        win.bkgd(" ", col("modal"))
        win.box()
        try:
            win.addstr(1, 3, "키 도움말", col("modal") | curses.A_BOLD)
            for i, (k, d) in enumerate(HELP[: h - 5]):
                win.addstr(3 + i, 3, k, col("modal") | curses.A_BOLD)
                win.addstr(3 + i, 3 + kw, cut(d, w - kw - 6), col("modal"))
            win.addstr(h - 2, max(1, w - 22), "아무 키나 누르면 닫기", col("modal") | curses.A_DIM)
        except curses.error:
            pass
        win.refresh()
        self.scr.timeout(-1)
        try:
            self.scr.getch()
        finally:
            self.scr.timeout(500)

    def _measure(self, s, w, tick, selected: bool = False) -> int:
        saved = self.put
        self.put = lambda *a, **k: None
        try:
            return self.card(0, 0, w, s, selected, tick)
        finally:
            self.put = saved

    def _draw_clipped(self, y, x, w, s, selected, tick, ymin, ymax) -> None:
        saved = self.put

        def clipped(yy, xx, segs, maxw):
            if ymin <= yy < ymax:
                saved(yy, xx, segs, maxw)
        self.put = clipped
        try:
            self.card(y, x, w, s, selected, tick)
        finally:
            self.put = saved

    # ----- 대화 상자 -----
    def confirm(self, lines: list[str]) -> bool:
        H, W = self.scr.getmaxyx()
        w = min(W - 4, max(width(t) for t in lines) + 6)
        h = len(lines) + 4
        y0, x0 = max(0, (H - h) // 2), max(0, (W - w) // 2)
        win = curses.newwin(h, w, y0, x0)
        win.bkgd(" ", col("modal"))
        win.box()
        for i, t in enumerate(lines):
            try:
                win.addstr(1 + i, 3, cut(t, w - 6), col("modal") | (curses.A_BOLD if i == 0 else 0))
            except curses.error:
                pass
        btn = "[취소: n, Esc]   [확인: y, Enter]"
        try:
            win.addstr(h - 2, max(1, w - width(btn) - 3), btn, col("modal") | curses.A_BOLD)
        except curses.error:
            pass
        win.refresh()
        self.scr.timeout(-1)
        try:
            while True:
                k = self.scr.getch()
                if k in (ord("y"), ord("Y"), 10, 13, curses.KEY_ENTER):
                    return True
                if k in (ord("n"), ord("N"), 27, ord("q")):
                    return False
        finally:
            self.scr.timeout(500)

    def add_panel(self) -> None:
        """세션 관리 목록. 최근 해제한 세션을 맨 위에 둔다."""
        self.flash("세션 목록을 읽는 중", 1)
        with self.reg.lock:
            items = self.reg.discover(days=7)
        items = [c for c in items if not c.get("parent")]  # 하위 세션은 부모 줄에 개수로만 보여준다
        back = sorted([c for c in items if c.get("untracked_rank") is not None and not c["tracked"]], key=lambda c: c["untracked_rank"])
        rest = [c for c in items if c not in back and c["mtime"] >= time.time() - 7 * 86400]
        # 부모가 없는 자동 실행 세션(claude -p, codex exec)은 접어 두고, 펼쳤을 때만 보여준다. 등록한 것은 늘 보인다.
        auto = [c for c in rest if c.get("kind") and not c["tracked"]]
        rest = [c for c in rest if c not in auto]
        show_auto = False

        def build():
            r = [("grp", "최근 해제한 세션")] * bool(back) + [("c", c) for c in back] \
                + [("grp", "최근 7일 세션")] * bool(rest) + [("c", c) for c in rest]
            if auto:
                r.append(("more", f"{'▾' if show_auto else '▸'} 자동 실행 세션 {len(auto)}개 (claude -p, codex exec)  Enter로 {'접기' if show_auto else '펼치기'}"))
                if show_auto:
                    r += [("c", c) for c in auto]
            return r
        rows = build()
        sel = next((i for i, r in enumerate(rows) if r[0] in ("c", "more")), 0)
        self.scr.timeout(-1)
        try:
            while True:
                H, W = self.scr.getmaxyx()
                self.scr.erase()
                self.put(0, 2, [("세션 관리", curses.A_BOLD), ("   ↑↓ 이동  Enter 등록/해제  Esc 닫기", col("gray"))], W - 2)
                view = H - 3
                start = max(0, sel - view + 1)
                for i, (kind, c) in enumerate(rows[start:start + view]):
                    y = 2 + i
                    if kind == "grp":
                        self.put(y, 2, [(c, col("gray") | curses.A_BOLD)], W - 4)
                        continue
                    if kind == "more":
                        self.put(y, 2, [(cut(c, W - 4), (curses.A_REVERSE if start + i == sel else 0) | col("gray"))], W - 4)
                        continue
                    on = start + i == sel
                    mark = "[등록됨]" if c["tracked"] else "[ 등록 ]"
                    agent = "Codex " if c["agent"] == "codex" else "Claude"
                    kind = f"[{c['kind']}] " if c.get("kind") else ""
                    kids = f", 하위 세션 {c['children']}개" if c.get("children") else ""
                    text = f" {mark} {agent} {kind}{display_name(c['workspace'])}  {c.get('title') or '제목 없음'}  ({c.get('launch') or ''}, {c['user']}{kids})"
                    attr = curses.A_REVERSE if on else 0
                    self.put(y, 2, [(cut(text, W - 4), attr | (col("blue") if c["tracked"] else 0))], W - 4)
                self.scr.refresh()
                k = self.scr.getch()
                if k in (27, ord("q"), ord("a")):
                    return
                if k in (curses.KEY_UP, ord("k")):
                    sel = next((j for j in range(sel - 1, -1, -1) if rows[j][0] in ("c", "more")), sel)
                elif k in (curses.KEY_DOWN, ord("j")):
                    sel = next((j for j in range(sel + 1, len(rows)) if rows[j][0] in ("c", "more")), sel)
                elif k in (10, 13, curses.KEY_ENTER, ord(" ")) and rows and rows[sel][0] == "more":
                    show_auto = not show_auto
                    rows = build()
                elif k in (10, 13, curses.KEY_ENTER, ord(" ")) and rows and rows[sel][0] == "c":
                    c = rows[sel][1]
                    try:
                        if c["tracked"]:
                            self.reg.tracked.remove(c["key"])
                        else:
                            self.reg.tracked.add(c["key"])
                        c["tracked"] = not c["tracked"]
                    except RuntimeError as e:
                        self.flash(str(e))
        finally:
            self.scr.timeout(500)

    # ----- 키 -----
    def move_card(self, items: list, d: int) -> None:
        if not items:
            return
        keys = [s["key"] for s in items]
        i = self.sel
        j = max(0, min(len(keys) - 1, i + d))
        if i == j:
            return
        keys[i], keys[j] = keys[j], keys[i]
        shown = set(keys)
        self.order = keys + [k for k in self.order if k not in shown]
        self.sort = "custom"
        self.sel = j
        self._save_prefs()

    def copy(self, text: str) -> None:
        # OSC 52: 터미널이 지원하면 로컬 PC 클립보드로 복사된다. SSH 너머에서도 동작한다.
        seq = "\033]52;c;" + base64.b64encode(text.encode()).decode() + "\a"
        if os.environ.get("TMUX"):
            seq = "\033Ptmux;\033" + seq.replace("\033", "\033\033") + "\033\\"
        try:
            os.write(sys.stdout.fileno(), seq.encode())
        except OSError:
            pass
        self.flash(f"복사함 (터미널이 지원하는 경우): {text}", 8)

    def handle(self, k: int) -> bool:
        items = self.sessions()
        cols = 1 if self.layout == "list" else max(1, (self.scr.getmaxyx()[1] - 1) // CARD_MIN)
        cur = items[self.sel] if items and self.sel < len(items) else None
        if k in (ord("q"), ord("Q")):
            return False
        if k == ord("r"):
            self.wake.set()
            self.flash("다시 읽는 중", 2)
        elif k == ord("?"):
            self.help_window()
        elif k in (curses.KEY_RIGHT, ord("l")):
            self.sel = min(len(items) - 1, self.sel + 1) if items else 0
        elif k in (curses.KEY_LEFT, ord("h")):
            self.sel = max(0, self.sel - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.sel = min(len(items) - 1, self.sel + cols) if items else 0
        elif k in (curses.KEY_UP, ord("k")):
            self.sel = max(0, self.sel - cols)
        elif k in (10, 13, curses.KEY_ENTER, ord(" ")) and cur:
            self.expanded.symmetric_difference_update({cur["key"]})
        elif k in (ord("x"), curses.KEY_DC) and cur:
            if self.confirm(["이 세션 트래킹을 해제하시나요?", "나중에 다시 추가할 수 있습니다.", "",
                             f"{display_name(cur.get('workspace'))}  {cur.get('title') or ''}"]):
                try:
                    self.reg.tracked.remove(cur["key"])
                    self.flash("해제했습니다. a를 누르면 세션 관리 목록에서 다시 추가할 수 있습니다")
                except RuntimeError as e:
                    self.flash(str(e))
        elif k == ord("d") and cur:
            done = not (cur["status"] == "done" and cur.get("completed_at"))
            try:
                self.reg.tracked.set_completed(cur["key"], done)
                self.wake.set()
                self.flash("완료로 표시했습니다. 새 활동이 생기면 다시 열립니다" if done else "다시 열었습니다")
            except RuntimeError as e:
                self.flash(str(e))
        elif k == ord("a"):
            self.add_panel()
        elif k == ord("s"):
            keys = [x for x, _ in SORTS]
            self.sort = keys[(keys.index(self.sort) + 1) % len(keys)]
            if self.sort == "custom" and not self.order:
                self.order = [s["key"] for s in items]
            self._save_prefs()
        elif k == ord("v"):
            self.layout = "list" if self.layout == "grid" else "grid"
            self._save_prefs()
        elif k == ord("g"):
            keys = [x for x, _ in AGENTS]
            self.agent = keys[(keys.index(self.agent) + 1) % len(keys)]
            self.sel = 0
            self._save_prefs()
        elif k == ord("f"):
            keys = [x for x, _ in FILTERS]
            self.filter = keys[(keys.index(self.filter) + 1) % len(keys)]
            self.sel = 0
        elif k in (ord("["), ord("<"), curses.KEY_SLEFT, curses.KEY_SR):
            self.move_card(items, -1)
        elif k in (ord("]"), ord(">"), curses.KEY_SRIGHT, curses.KEY_SF):
            self.move_card(items, 1)
        elif k == ord("c") and cur:
            self.copy(cur.get("resume") or "")
        return True

    def run(self) -> None:
        curses.curs_set(0)
        init_colors()
        self.scr.timeout(500)
        t0 = time.time()
        while True:
            tick = int((time.time() - t0) / 0.5)
            self.draw(tick)
            k = self.scr.getch()
            if k == curses.KEY_RESIZE:
                continue
            if k != -1 and not self.handle(k):
                return


def main() -> int:
    if not STATE_DIR.exists():
        print(f"{STATE_DIR}가 없음. 먼저 설치해야 함", file=sys.stderr)
        return 1
    os.environ.setdefault("ESCDELAY", "50")
    curses.wrapper(lambda scr: App(scr).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
