"""계획 단계 상태. 도구 호출을 순서대로 적용해 현재 단계와 변경 이력을 만든다.

지원하는 도구:
- Claude 데스크톱 `mcp__ccd_turn__update_plan`: items(전체 교체), advance, done/start.
  턴 마무리 `mcp__ccd_turn__wrap_up`은 남은 단계를 모두 완료로 바꾼다
- Claude `TodoWrite`: todos 전체 교체
- Claude `TaskCreate`, `TaskUpdate`: 항목 추가, 상태 변경
- Codex `update_plan`: plan 전체 교체
"""

from __future__ import annotations

import re

SIZE_POINTS = {"S": 1, "M": 3, "L": 8}
SIZE_RE = re.compile(r"\[(S|M|L)\]")
DONE = "completed"
ACTIVE = "in_progress"
PENDING = "pending"


def norm_status(s: str | None) -> str:
    s = (s or "").lower()
    if s in ("completed", "done", "complete"):
        return DONE
    if s in ("in_progress", "in-progress", "active", "running"):
        return ACTIVE
    return PENDING


class Plan:
    def __init__(self):
        self.steps: list[dict] = []
        self.revisions: list[dict] = []
        self.tasks: dict[str, dict] = {}  # TaskCreate id -> step
        self.updated: str | None = None  # 마지막으로 계획을 바꾼 시각
        self.closed: str | None = None  # 턴을 마무리해 계획이 끝난 시각
        # 앞선 턴에서 끝난 계획의 단계 수. 새 턴의 계획이 오면 지금 계획을 여기에 더하고 비운다.
        self.archived_done = 0
        self.archived_total = 0

    def _archive_if_new_turn(self) -> None:
        if self.closed and (self.updated or "") <= self.closed and self.steps:
            self.archived_total += len(self.steps)
            self.archived_done += sum(1 for s in self.steps if s["status"] == DONE)
            self.steps, self.tasks, self.closed = [], {}, None

    def session_counts(self) -> tuple[int, int]:
        """세션 전체에서 (완료 단계 수, 계획한 단계 수). 지난 턴의 계획을 포함한다."""
        return (self.archived_done + sum(1 for s in self.steps if s["status"] == DONE),
                self.archived_total + len(self.steps))

    def finished_before(self, turn_ts: str | None) -> bool:
        """계획이 앞선 턴에서 끝났고, 그 뒤 새 턴에서는 계획을 세우지 않았는지."""
        return bool(self.closed and turn_ts and turn_ts > self.closed and (self.updated or "") <= self.closed)

    def _step(self, content: str, status: str, ts: str) -> dict:
        m = SIZE_RE.search(content or "")
        st = {"content": content or "", "size": m.group(1) if m else None, "status": status,
              "started": ts if status == ACTIVE else None, "ended": ts if status == DONE else None}
        return st

    def _set_status(self, step: dict, status: str, ts: str) -> None:
        if step["status"] == status:
            return
        if status == ACTIVE and not step["started"]:
            step["started"] = ts
        if status == DONE:
            step["ended"] = ts
            step["started"] = step["started"] or ts
        step["status"] = status

    def _replace(self, items: list[tuple[str, str]], ts: str) -> None:
        old = {s["content"]: s for s in self.steps}
        before = [s["content"] for s in self.steps]
        new = []
        for content, status in items:
            if content in old:
                st = dict(old[content])
                self._set_status(st, status, ts)
            else:
                st = self._step(content, status, ts)
            new.append(st)
        after = [s["content"] for s in new]
        if self.steps and (set(before) - set(after) or set(after) - set(before)):
            self.revisions.append({
                "ts": ts,
                "added": [c for c in after if c not in before],
                "removed": [c for c in before if c not in after],
                "points_before": points(self.steps),
                "points_after": points(new),
            })
        self.steps = new

    def apply(self, tool: str, inp: dict, ts: str) -> bool:
        """계획 도구면 적용하고 True를 돌려준다."""
        if not isinstance(inp, dict):
            return False
        if tool.endswith("update_plan") and tool != "update_plan" or tool == "mcp__ccd_turn__update_plan":
            if isinstance(inp.get("items"), list):
                self._archive_if_new_turn()
            self.updated = ts
            if isinstance(inp.get("items"), list):
                self._replace([(i.get("content", ""), norm_status(i.get("status"))) for i in inp["items"] if isinstance(i, dict)], ts)
            if inp.get("advance"):
                cur = next((s for s in self.steps if s["status"] == ACTIVE), None)
                if cur:
                    self._set_status(cur, DONE, ts)
                nxt = next((s for s in self.steps if s["status"] == PENDING), None)
                if nxt:
                    self._set_status(nxt, ACTIVE, ts)
            for n in inp.get("done") or []:
                if isinstance(n, int) and 1 <= n <= len(self.steps):
                    self._set_status(self.steps[n - 1], DONE, ts)
            n = inp.get("start")
            if isinstance(n, int) and 1 <= n <= len(self.steps):
                for s in self.steps:
                    if s["status"] == ACTIVE:
                        self._set_status(s, PENDING, ts)
                self._set_status(self.steps[n - 1], ACTIVE, ts)
            return True
        if tool == "mcp__ccd_turn__wrap_up":
            # 데스크톱 앱은 턴을 마무리할 때 남은 단계를 모두 완료로 표시한다. 같은 기준을 쓴다.
            for st in self.steps:
                if st["status"] != DONE:
                    self._set_status(st, DONE, ts)
            if self.steps:
                self.closed = ts
            return True
        if tool == "TodoWrite" and isinstance(inp.get("todos"), list):
            self._archive_if_new_turn()
            self.updated = ts
            self._replace([(t.get("content", ""), norm_status(t.get("status"))) for t in inp["todos"] if isinstance(t, dict)], ts)
            return True
        if tool in ("TaskCreate", "TaskUpdate"):
            if tool == "TaskCreate":
                self._archive_if_new_turn()
            self.updated = ts
        if tool == "TaskCreate":
            st = self._step(inp.get("subject") or inp.get("description") or "", PENDING, ts)
            self.steps.append(st)
            self.tasks[str(len(self.tasks) + 1)] = st
            return True
        if tool == "TaskUpdate":
            st = self.tasks.get(str(inp.get("taskId") or inp.get("id") or ""))
            if st and inp.get("status"):
                if inp["status"] == "deleted":
                    self.steps = [s for s in self.steps if s is not st]
                else:
                    self._set_status(st, norm_status(inp["status"]), ts)
            return True
        if tool == "update_plan" and isinstance(inp.get("plan"), list):
            self._replace([(p.get("step", ""), norm_status(p.get("status"))) for p in inp["plan"] if isinstance(p, dict)], ts)
            return True
        # 계획 도구가 아닌 호출은 진행 중 단계의 활동량으로 센다
        cur = self.current()
        if cur is not None:
            cur["tools"] = cur.get("tools", 0) + 1
        return False

    def current(self) -> dict | None:
        return next((s for s in self.steps if s["status"] == ACTIVE), None)


def step_points(step: dict) -> int:
    return SIZE_POINTS.get(step.get("size") or "M", 3)


def points(steps: list[dict]) -> int:
    return sum(step_points(s) for s in steps)
