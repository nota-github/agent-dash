"""Codex rollout 해석 (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).

사용자가 입력한 내용은 저장하지 않는다. 요약은 에이전트의 마지막 응답만 쓴다.
"""

from __future__ import annotations

import json
import os
import re

from agentdash.claude import short
from agentdash.plan import Plan
from agentdash.sources import LineReader, LocalFS

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
EXEC_PLAN_RE = re.compile(r"update_plan\((\{.*?\})\)", re.S)


def rollout_id(path: str) -> str | None:
    m = UUID_RE.search(path)
    return m.group(1) if m else None


class CodexSession:
    agent = "codex"

    def __init__(self, fs: LocalFS, path: str):
        self.fs = fs
        self.path = path
        self.id = rollout_id(path) or os.path.basename(path)
        self.reader = LineReader(fs, path)
        self.meta: dict = {}
        self.first_ts = None
        self.last_ts = None
        self.turn_open = False
        self.turn_started = None
        self.turns = 0
        self.total = {}
        self.last_usage = {}
        self.context_window = None
        self.model = None
        self.plan = Plan()
        self.pending: dict[str, dict] = {}
        self.errors: list[dict] = []
        self.tool_count = 0
        self.last_message = None
        self.last_tool = None
        self.mtime = 0.0
        self.thread_name = None

    def poll(self) -> bool:
        files = self.fs.glob(self.path)
        if not files:
            return False
        self.mtime = files[0].mtime
        recs = self.reader.poll(files[0].size)
        for r in recs:
            self._feed(r)
        return bool(recs)

    def _feed(self, r: dict) -> None:
        t = r.get("type")
        ts = r.get("timestamp")
        p = r.get("payload") or {}
        if ts:
            self.first_ts = self.first_ts or ts
            self.last_ts = ts
        if t == "session_meta":
            for k in ("cwd", "originator", "source", "cli_version"):
                if isinstance(p.get(k), str):
                    self.meta[k] = p[k]
            return
        if t == "turn_context":
            self.model = p.get("model") or self.model
            if p.get("cwd"):
                self.meta["cwd"] = p["cwd"]
            return
        if t == "event_msg":
            et = p.get("type")
            if et == "task_started":
                self.turn_open, self.turn_started = True, ts
                self.turns += 1
                self.context_window = p.get("model_context_window") or self.context_window
            elif et == "task_complete":
                self.turn_open = False
                self.pending.clear()
                if p.get("last_agent_message"):
                    self.last_message = p["last_agent_message"]
            elif et == "token_count":
                info = p.get("info") or {}
                self.total = info.get("total_token_usage") or self.total
                self.last_usage = info.get("last_token_usage") or self.last_usage
                self.context_window = info.get("model_context_window") or self.context_window
            return
        if t == "response_item":
            it = p.get("type")
            if it in ("function_call", "custom_tool_call"):
                name = p.get("name") or ""
                raw = p.get("arguments") if it == "function_call" else p.get("input")
                self.pending[p.get("call_id")] = {"name": name, "summary": f"{name}: {short(raw, 80)}", "ts": ts}
                self.last_tool = self.pending[p.get("call_id")]
                self.tool_count += 1
                if name == "update_plan":
                    try:
                        self.plan.apply("update_plan", json.loads(raw), ts)
                    except (TypeError, ValueError):
                        pass
                elif isinstance(raw, str) and "update_plan(" in raw:
                    for m in EXEC_PLAN_RE.finditer(raw):
                        try:
                            self.plan.apply("update_plan", json.loads(m.group(1)), ts)
                        except ValueError:
                            pass
            elif it in ("function_call_output", "custom_tool_call_output"):
                call = self.pending.pop(p.get("call_id"), None)
                out = p.get("output")
                text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False) if out else ""
                if call and re.search(r"exit code:? [1-9]|\"exit_code\": ?[1-9]|error", text[:400], re.I):
                    self.errors.append({"ts": ts, "tool": call["name"], "text": short(text, 200)})
                    self.errors = self.errors[-5:]

    def tokens(self) -> dict:
        t = self.total or {}
        cached = t.get("cached_input_tokens", 0)
        out = {
            "input": max(0, t.get("input_tokens", 0) - cached),
            "output": t.get("output_tokens", 0),
            "cache_read": cached,
            "cache_write": t.get("cache_write_input_tokens", 0),
        }
        out["total"] = sum(out.values())
        out["io"] = out["total"] - out["cache_read"]
        return out

    def context(self) -> int:
        return (self.last_usage or {}).get("input_tokens", 0)

    def title(self) -> str:
        return self.thread_name or "제목 없음"
