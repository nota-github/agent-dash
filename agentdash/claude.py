"""Claude Code transcript 해석.

사용자가 입력한 내용(요청, 답변, 거부 사유)은 저장하지 않는다. user 레코드는 도구 결과의
존재와 오류 여부만 본다. 제목은 Claude가 만든 제목, 요약은 Claude가 쓴 away_summary만 쓴다.

세션 하나는 메인 transcript(<session>.jsonl)와 서브에이전트 transcript
(<session>/subagents/agent-<id>.jsonl, agent-<id>.meta.json)로 이루어진다.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque

from agentdash.plan import Plan
from agentdash.sources import LineReader, LocalFS

USER_INPUT_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
# 사용자가 도구 실행을 거부하며 쓴 말이 tool_result에 들어간다. 이런 결과는 내용을 저장하지 않는다.
# 백그라운드로 시작한 작업. 완료 알림(<task-notification>)이 오면 끝난 것으로 본다.
BG_RESULT_RE = re.compile(r"running in background|moved to the background|launched in (the )?background|async agent", re.I)
NOTIFY_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>.*?<status>([^<]+)</status>", re.S)
USER_REJECT_RE = re.compile(r"user (doesn't|does not) want|user (rejected|denied|declined)|the user said|user's (feedback|response)", re.I)
AGENT_TOOLS = {"Agent", "Task"}
ERROR_KEEP = 5


def short(text, n: int = 160) -> str:
    if not isinstance(text, str):
        if isinstance(text, list):
            text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
        else:
            text = str(text)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _interrupted(content) -> bool:
    """Claude Code가 넣는 중단 표시인지만 확인한다. 사용자 입력은 읽어 두지 않는다."""
    texts = [content] if isinstance(content, str) else [
        b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    return any(t.startswith("[Request interrupted by user") for t in texts if isinstance(t, str))


def tool_summary(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return name
    for k in ("description", "command", "file_path", "pattern", "url", "query", "prompt"):
        if inp.get(k):
            return f"{name}: {short(inp[k], 80)}"
    return name


class Transcript:
    """메인 또는 서브에이전트 transcript 하나의 상태."""

    def __init__(self, fs: LocalFS, path: str):
        self.reader = LineReader(fs, path)
        self.path = path
        self.meta: dict = {}
        self.first_ts = None
        self.last_ts = None
        self.last_kind = None  # user, assistant, tool_result
        self.last_stop = None
        self.usage: dict[str, dict] = {}  # message.id -> usage
        self.usage_ts: dict[str, str] = {}
        self.context = 0
        self.model = None
        self.pending: dict[str, dict] = {}
        self.errors: list[dict] = []
        self.error_streak: tuple[str, int] = ("", 0)
        self.plan = Plan()
        self.last_turn_ts: str | None = None
        self.agent_calls: dict[str, dict] = {}  # tool_use id -> 호출 정보
        self.tool_count = 0
        self.tool_times: deque = deque(maxlen=3000)  # 도구 호출 시각 (활동량 계산용, 내용은 저장하지 않는다)
        self.error_total = 0
        self.background: dict[str, dict] = {}  # tool_use id -> 백그라운드 작업
        self._bg_marked: set[str] = set()
        self.last_tool = None
        self.compactions = 0
        self.cost = None
        self.files_touched: dict[str, str] = {}  # 경로 -> 마지막 수정 시각

    def poll(self, size: int | None = None) -> bool:
        recs = self.reader.poll(size)
        for r in recs:
            self._feed(r)
        return bool(recs)

    def _feed(self, r: dict) -> None:
        t = r.get("type")
        ts = r.get("timestamp")
        if t == "continued-in":
            # 대화가 길어져 앱이 새 세션 ID로 이어 쓴 경우. 이후 기록은 그 세션 파일에 쌓인다.
            self.meta["continued_in"] = r.get("continuedInSessionId")
        elif t == "custom-title":
            self.meta["custom_title"] = r.get("customTitle")
        elif t == "ai-title":
            self.meta["ai_title"] = r.get("aiTitle")
        elif t == "agent-name":
            self.meta["agent_name"] = r.get("agentName")
        elif t == "cost-state":
            self.cost = r.get("totalCostUSD")
        elif t == "system":
            sub = r.get("subtype")
            if sub == "away_summary":
                self.meta["away_summary"] = r.get("content")
                self.meta["away_ts"] = ts
            elif sub == "compact_boundary":
                self.compactions += 1
        elif t in ("queue-operation", "attachment"):
            # CLI는 백그라운드 작업 완료 알림을 이 레코드로 남긴다. 알림의 id와 상태만 꺼낸다.
            raw = r.get("content") if t == "queue-operation" else r.get("attachment")
            if raw and "<task-notification>" in (raw if isinstance(raw, str) else json.dumps(raw)):
                self._notifications(raw if isinstance(raw, str) else json.dumps(raw))
        if t not in ("user", "assistant"):
            return
        for k in ("entrypoint", "cwd", "gitBranch", "version", "sessionId", "agentId"):
            if r.get(k):
                self.meta[k] = r[k]
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        msg = r.get("message") or {}
        content = msg.get("content")
        if t == "assistant":
            self.last_kind = "assistant"
            self.last_stop = msg.get("stop_reason")
            self.model = msg.get("model") or self.model
            mid = msg.get("id") or r.get("requestId") or r.get("uuid")
            u = msg.get("usage")
            if u and mid:
                self.usage[mid] = u
                self.usage_ts[mid] = ts
                if not r.get("isSidechain") or self.meta.get("agentId"):
                    self.context = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                                    + u.get("cache_creation_input_tokens", 0))
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    self._tool_use(b, ts)
        else:
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                self.last_kind = "tool_result"
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self._tool_result(b, ts)
            else:
                self._notifications(content)
                if r.get("isCompactSummary") or r.get("isVisibleInTranscriptOnly"):
                    # 압축 요약은 사용자가 보낸 요청이 아니다. 턴 상태를 바꾸지 않는다.
                    pass
                elif _interrupted(content):
                    # 사용자가 중단한 턴은 응답이 끝난 것으로 본다.
                    self.last_kind, self.last_stop = "assistant", "end_turn"
                    self.pending.clear()
                elif not r.get("isMeta"):
                    self.last_kind = "user"
                    self.last_stop = None
                    self.last_turn_ts = ts  # 새 턴이 시작된 시각 (내용은 저장하지 않는다)

    def _tool_use(self, b: dict, ts: str) -> None:
        name, inp, tid = b.get("name", ""), b.get("input") or {}, b.get("id")
        self.tool_count += 1
        if ts:
            self.tool_times.append(ts)
        self.last_tool = {"name": name, "summary": tool_summary(name, inp), "ts": ts}
        self.pending[tid] = {"name": name, "summary": tool_summary(name, inp), "ts": ts}
        if isinstance(inp, dict) and inp.get("run_in_background") is True:
            self._bg_marked.add(tid)
        self.plan.apply(name, inp, ts)
        if name in AGENT_TOOLS:
            cur = self.plan.current()
            self.agent_calls[tid] = {
                "description": inp.get("description"), "type": inp.get("subagent_type"),
                "ts": ts, "step": cur["content"] if cur else None,
            }
        if name in ("Edit", "Write", "NotebookEdit", "MultiEdit") and inp.get("file_path"):
            self.files_touched[inp["file_path"]] = ts

    def _tool_result(self, b: dict, ts: str) -> None:
        tid = b.get("tool_use_id")
        call = self.pending.pop(tid, None)
        name = call["name"] if call else "?"
        if b.get("is_error"):
            self.error_total += 1
            text = short(b.get("content"), 200)
            if USER_REJECT_RE.search(text):
                text = "사용자가 도구 실행을 거부함"
            self.errors.append({"ts": ts, "tool": name, "text": text})
            self.errors = self.errors[-ERROR_KEEP:]
            streak_tool, n = self.error_streak
            self.error_streak = (name, n + 1 if streak_tool == name else 1)
        else:
            self.error_streak = ("", 0)
            if call and (tid in self._bg_marked or BG_RESULT_RE.search(short(b.get("content"), 300))):
                self.background[tid] = {"name": name, "summary": call["summary"], "ts": call["ts"]}
        self._bg_marked.discard(tid)

    def _notifications(self, content) -> None:
        """완료 알림에서 tool_use id와 상태만 꺼낸다. 알림 본문은 저장하지 않는다."""
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        for text in texts:
            if "<task-notification>" not in text:
                continue
            for tid, status in NOTIFY_RE.findall(text):
                if status.strip().lower() != "running":
                    self.background.pop(tid.strip(), None)

    def tokens(self) -> dict:
        out = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        for u in self.usage.values():
            out["input"] += u.get("input_tokens", 0)
            out["output"] += u.get("output_tokens", 0)
            out["cache_read"] += u.get("cache_read_input_tokens", 0)
            out["cache_write"] += u.get("cache_creation_input_tokens", 0)
        out["total"] = sum(out.values())
        # 캐시 읽기는 같은 문맥을 다시 읽은 양이라 크게 부풀려진다. 화면의 대표 수치는 이 값을 뺀다.
        out["io"] = out["total"] - out["cache_read"]
        return out

    def tokens_since(self, iso_prefix: str) -> int:
        total = 0
        for mid, u in self.usage.items():
            if (self.usage_ts.get(mid) or "") >= iso_prefix:
                total += u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        return total


class ClaudeSession:
    """메인 transcript와 서브에이전트 묶음."""

    agent = "claude"

    def __init__(self, fs: LocalFS, path: str):
        self.fs = fs
        self.path = path
        self.id = os.path.basename(path)[:-len(".jsonl")]
        self.main = Transcript(fs, path)
        self.subdir = path[:-len(".jsonl")] + "/subagents"
        self.subs: dict[str, Transcript] = {}
        self.sub_meta: dict[str, dict] = {}
        self.mtime = 0.0
        self.sub_mtime = 0.0

    def poll(self) -> bool:
        changed = False
        files = self.fs.glob(self.path)
        if files:
            self.mtime = files[0].mtime
            changed |= self.main.poll(files[0].size)
        for f in self.fs.glob(self.subdir + "/agent-*.jsonl"):
            aid = os.path.basename(f.path)[len("agent-"):-len(".jsonl")]
            if aid not in self.subs:
                self.subs[aid] = Transcript(self.fs, f.path)
            self.sub_mtime = max(self.sub_mtime, f.mtime)
            if self.subs[aid].poll(f.size):
                changed = True
                self.sub_meta[aid] = self.fs.read_json(f.path[:-len(".jsonl")] + ".meta.json") or self.sub_meta.get(aid, {})
            elif aid not in self.sub_meta or not self.sub_meta[aid].get("finished"):
                m = self.fs.read_json(f.path[:-len(".jsonl")] + ".meta.json")
                if m and m != self.sub_meta.get(aid):
                    self.sub_meta[aid] = m
                    changed = True
        return changed

    @property
    def headless(self) -> bool:
        """사람이 대화하지 않는 세션(claude -p 등). 승인 대기가 생기지 않는다.

        데스크톱 세션이 실행한 claude -p는 entrypoint를 claude-desktop으로 물려받는다.
        앱이 붙이는 제목(custom-title, agent-name)이 없으면 헤드리스로 본다.
        """
        m = self.main.meta
        if m.get("entrypoint") == "sdk-cli":
            return True
        return m.get("entrypoint") == "claude-desktop" and not (m.get("custom_title") or m.get("agent_name"))

    def title(self) -> str:
        m = self.main.meta
        return m.get("custom_title") or m.get("ai_title") or m.get("agent_name") or "제목 없음"
