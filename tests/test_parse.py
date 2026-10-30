"""기록 해석과 상태 계산 테스트. 실제 기록 형식을 줄여 만든 가짜 fixture를 쓴다."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdash import model, procs  # noqa: E402
from agentdash.claude import ClaudeSession  # noqa: E402
from agentdash.codex import CodexSession  # noqa: E402
from agentdash.plan import Plan  # noqa: E402
from agentdash.sources import LocalFS  # noqa: E402


def iso(sec_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - sec_ago))


def assistant(ts, mid, blocks, stop="tool_use", usage=None, **extra):
    return {"type": "assistant", "timestamp": ts, "sessionId": "s1", "entrypoint": "claude-desktop",
            "cwd": "/work/ws/261029-demo", "gitBranch": "main", "isSidechain": False,
            "message": {"id": mid, "model": "claude-opus-5-5", "stop_reason": stop, "content": blocks,
                        "usage": usage or {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                                           "cache_creation_input_tokens": 0}}, **extra}


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(ts, tid, text="ok", error=False):
    return {"type": "user", "timestamp": ts, "sessionId": "s1",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": error}]}}


def user(ts, text):
    return {"type": "user", "timestamp": ts, "sessionId": "s1", "message": {"role": "user", "content": text}}


def write_jsonl(path: Path, recs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class PlanTest(unittest.TestCase):
    def test_desktop_update_plan_sequence(self):
        p = Plan()
        p.apply("mcp__ccd_turn__update_plan", {"items": [
            {"content": "조사 [S]", "status": "in_progress"}, {"content": "구현 [L]", "status": "pending"},
            {"content": "검증", "status": "pending"}]}, iso(300))
        p.apply("mcp__ccd_turn__update_plan", {"advance": True}, iso(200))
        self.assertEqual([s["status"] for s in p.steps], ["completed", "in_progress", "pending"])
        self.assertEqual([s["size"] for s in p.steps], ["S", "L", None])
        p.apply("mcp__ccd_turn__update_plan", {"done": [2], "start": 3}, iso(100))
        self.assertEqual([s["status"] for s in p.steps], ["completed", "completed", "in_progress"])
        self.assertEqual(p.revisions, [])
        p.apply("mcp__ccd_turn__update_plan", {"items": [
            {"content": "조사 [S]", "status": "completed"}, {"content": "구현 [L]", "status": "completed"},
            {"content": "수정 [M]", "status": "in_progress"}, {"content": "검증", "status": "pending"}]}, iso(50))
        self.assertEqual(len(p.revisions), 1)
        self.assertEqual(p.revisions[0]["added"], ["수정 [M]"])
        self.assertEqual(p.steps[0]["started"], p.steps[0]["started"])

    def test_todowrite_and_codex_plan(self):
        p = Plan()
        p.apply("TodoWrite", {"todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}]}, iso(10))
        self.assertEqual(p.current()["content"], "b")
        q = Plan()
        q.apply("update_plan", {"plan": [{"step": "x", "status": "completed"}, {"step": "y", "status": "pending"}]}, iso(10))
        self.assertEqual([s["status"] for s in q.steps], ["completed", "pending"])

    def test_progress_and_eta(self):
        steps = [
            {"content": "a", "size": "S", "status": "completed", "started": iso(400), "ended": iso(300)},
            {"content": "b", "size": "M", "status": "in_progress", "started": iso(100), "ended": None},
            {"content": "c", "size": None, "status": "pending", "started": None, "ended": None},
        ]
        self.assertAlmostEqual(model.progress(steps), (1 + 1.5) / 7)
        self.assertAlmostEqual(model.progress(steps, {"b": 1.0}), (1 + 3) / 7)
        lo, hi = model.eta(steps, time.time())
        self.assertTrue(0 < lo < hi)


class ClaudeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "proj"
        self.path = self.dir / "s1.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def session(self):
        s = ClaudeSession(LocalFS(), str(self.path))
        s.poll()
        return s

    def test_tokens_dedup_title_and_subagent_tree(self):
        write_jsonl(self.path, [
            {"type": "custom-title", "customTitle": "데모 세션", "sessionId": "s1"},
            {"type": "last-prompt", "lastPrompt": "대시보드 만들어줘", "sessionId": "s1"},
            user(iso(120), "대시보드 만들어줘"),
            assistant(iso(110), "m1", [tool_use("t1", "mcp__ccd_turn__update_plan", {"items": [
                {"content": "조사", "status": "in_progress"}, {"content": "구현", "status": "pending"}]})]),
            result(iso(109), "t1"),
            # 같은 응답이 두 줄로 기록되는 경우
            assistant(iso(100), "m2", [{"type": "text", "text": "조사합니다"}]),
            assistant(iso(100), "m2", [tool_use("t2", "Agent", {"description": "코드 조사", "subagent_type": "Explore", "prompt": "..."})]),
        ])
        sub = self.dir / "s1" / "subagents"
        write_jsonl(sub / "agent-a1.jsonl", [
            {**assistant(iso(90), "x1", [tool_use("u1", "Read", {"file_path": "/a"})]), "isSidechain": True, "agentId": "a1"},
            {**result(iso(89), "u1"), "isSidechain": True, "agentId": "a1"},
        ])
        (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "코드 조사", "toolUseId": "t2", "spawnDepth": 1}))
        s = self.session()
        self.assertEqual(s.title(), "데모 세션")
        self.assertEqual(s.main.tokens()["total"], 2 * (10 + 5 + 100))
        self.assertIn("a1", s.subs)
        st, _, _ = model.claude_status(s.main, time.time(), s.sub_mtime)
        self.assertEqual(st, "running")
        reg = model.Registry.__new__(model.Registry)
        reg.src = type("S", (), {"local": LocalFS("tester"), "root": None})()
        reg.source_of = {}
        view = reg._claude_view("claude:s1", s, time.time(), "2000-01-01", [])
        self.assertEqual([n["kind"] for n in view["nodes"]], ["main", "subagent"])
        self.assertEqual(view["nodes"][1]["step"], "조사")
        self.assertEqual(view["workspace"], "261029-demo")
        self.assertIn("claude --resume s1", view["resume"])
        # 사용자가 입력한 내용은 어디에도 남지 않는다
        blob = json.dumps(view, ensure_ascii=False) + json.dumps(s.main.meta, ensure_ascii=False)
        self.assertNotIn("대시보드 만들어줘", blob)
        self.assertNotIn("last_prompt", blob)

    def test_user_rejection_text_is_not_kept(self):
        write_jsonl(self.path, [
            user(iso(30), "비밀 요청 내용"),
            assistant(iso(20), "m1", [tool_use("t1", "Bash", {"command": "ls"})]),
            result(iso(10), "t1", "The user doesn't want to proceed with this tool use. The user said: 비밀 사유", error=True),
        ])
        s = self.session()
        self.assertEqual(s.main.errors[-1]["text"], "사용자가 도구 실행을 거부함")
        self.assertEqual(s.title(), "제목 없음")

    def test_status_end_turn_and_user_input(self):
        write_jsonl(self.path, [user(iso(30), "hi"), assistant(iso(20), "m1", [{"type": "text", "text": "done"}], stop="end_turn")])
        s = self.session()
        self.assertEqual(model.claude_status(s.main, time.time())[0], "idle")
        write_jsonl(self.path, [user(iso(10), "next"), assistant(iso(5), "m2", [tool_use("q", "AskUserQuestion", {"questions": []})])])
        s.poll()
        self.assertEqual(model.claude_status(s.main, time.time())[0], "needs_input")

    def test_long_pending_tool_is_maybe_waiting_and_errors_streak(self):
        write_jsonl(self.path, [user(iso(400), "go")] + [
            r for i in range(3) for r in (assistant(iso(390 - i), f"e{i}", [tool_use(f"b{i}", "Bash", {"command": "make"})]),
                                          result(iso(389 - i), f"b{i}", "Exit code 2", error=True))
        ] + [assistant(iso(300), "m9", [tool_use("b9", "Bash", {"command": "rm -rf build"})])])
        s = self.session()
        self.assertEqual(s.main.error_streak, ("Bash", 3))
        st, since, detail = model.claude_status(s.main, time.time())
        self.assertEqual(st, "maybe_waiting")
        self.assertIn("rm -rf build", detail)

    def test_background_task_keeps_session_running(self):
        write_jsonl(self.path, [
            user(iso(100), "go"),
            assistant(iso(90), "m1", [tool_use("bg1", "Bash", {"command": "make long", "run_in_background": True})]),
            result(iso(89), "bg1", "Command running in background with ID: abc."),
            assistant(iso(80), "m2", [{"type": "text", "text": "기다립니다"}], stop="end_turn"),
        ])
        s = self.session()
        self.assertIn("bg1", s.main.background)
        reg = model.Registry.__new__(model.Registry)
        reg.src = type("S", (), {"local": LocalFS("tester"), "root": None})()
        reg.source_of = {}
        view = reg._claude_view("claude:s1", s, time.time(), "2000-01-01", [])
        self.assertEqual(view["status"], "running")
        self.assertIn("백그라운드", view["status_label"])
        self.assertEqual(view["nodes"][0]["status"], "idle")
        # 완료 알림이 오면 응답 완료로 돌아간다. 알림 본문은 저장하지 않는다.
        write_jsonl(self.path, [
            user(iso(10), "<task-notification>\n<task-id>abc</task-id>\n<tool-use-id>bg1</tool-use-id>\n<status>completed</status>\n<summary>비밀</summary>\n</task-notification>"),
            assistant(iso(5), "m3", [{"type": "text", "text": "끝"}], stop="end_turn"),
        ])
        s.poll()
        self.assertEqual(s.main.background, {})
        view = reg._claude_view("claude:s1", s, time.time(), "2000-01-01", [])
        self.assertEqual(view["status"], "idle")
        self.assertNotIn("비밀", json.dumps(view, ensure_ascii=False))

    def test_headless_long_tool_is_running_not_waiting(self):
        write_jsonl(self.path, [user(iso(400), "go"), assistant(iso(300), "m1", [tool_use("b1", "Bash", {"command": "scp big"})])])
        s = self.session()
        self.assertEqual(model.claude_status(s.main, time.time())[0], "maybe_waiting")
        self.assertEqual(model.claude_status(s.main, time.time(), headless=True)[0], "running")
        self.assertTrue(s.headless)  # 앱 제목 기록이 없는 claude-desktop 세션

    def test_incremental_read_handles_partial_line(self):
        write_jsonl(self.path, [user(iso(30), "hi")])
        s = self.session()
        with open(self.path, "a") as f:
            f.write(json.dumps(assistant(iso(20), "m1", [], stop="end_turn"))[:40])
        s.poll()
        self.assertEqual(s.main.last_kind, "user")
        with open(self.path, "a") as f:
            f.write(json.dumps(assistant(iso(20), "m1", [], stop="end_turn"))[40:] + "\n")
        s.poll()
        self.assertEqual(s.main.last_kind, "assistant")


class CodexTest(unittest.TestCase):
    def test_rollout(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "rollout-2026-10-29T10-40-30-00000000-0000-7000-8000-000000000001.jsonl"
            write_jsonl(p, [
                {"timestamp": iso(60), "type": "session_meta", "payload": {"id": "x", "cwd": "/w", "originator": "codex_exec", "source": "exec"}},
                {"timestamp": iso(59), "type": "event_msg", "payload": {"type": "task_started", "model_context_window": 258400}},
                {"timestamp": iso(58), "type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "call_id": "c1",
                                                                         "arguments": json.dumps({"plan": [{"step": "a", "status": "in_progress"}]})}},
                {"timestamp": iso(57), "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50},
                    "last_token_usage": {"input_tokens": 1000}, "model_context_window": 258400}}},
            ])
            s = CodexSession(LocalFS(), str(p))
            s.poll()
            self.assertEqual(s.id, "00000000-0000-7000-8000-000000000001")
            self.assertEqual(model._codex_status(s, time.time()), "running")
            self.assertEqual(s.tokens()["total"], 200 + 800 + 50)
            self.assertEqual(s.plan.steps[0]["content"], "a")
            write_jsonl(p, [{"timestamp": iso(1), "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "끝"}}])
            s.poll()
            self.assertEqual(model._codex_status(s, time.time()), "idle")
            self.assertEqual(s.last_message, "끝")
            self.assertFalse(hasattr(s, "last_prompt"))


class TrackedTest(unittest.TestCase):
    def test_dir_match_and_persist(self):
        with tempfile.TemporaryDirectory() as t:
            tr = model.Tracked(Path(t) / "tracked.json")
            tr.add("claude:abc")
            tr.add_dir("/work/ws/x")
            tr2 = model.Tracked(Path(t) / "tracked.json")
            self.assertTrue(tr2.matches("claude:abc", None))
            self.assertTrue(tr2.matches("claude:zzz", "/work/ws/x/runs/a"))
            self.assertFalse(tr2.matches("claude:zzz", "/work/ws/xy"))
            tr2.remove("claude:abc")
            self.assertFalse(model.Tracked(Path(t) / "tracked.json").matches("claude:abc", None))

    def test_untrack_overrides_dir_and_keeps_recent_list(self):
        with tempfile.TemporaryDirectory() as t:
            tr = model.Tracked(Path(t) / "tracked.json")
            tr.add_dir("/w/x")
            tr.add("claude:a")
            tr.remove("claude:a")
            tr.remove("claude:b")
            tr2 = model.Tracked(Path(t) / "tracked.json")
            self.assertEqual(tr2.data["untracked"], ["claude:b", "claude:a"])
            self.assertFalse(tr2.matches("claude:b", "/w/x/y"))  # 폴더 등록보다 해제가 우선한다
            self.assertTrue(tr2.matches("claude:c", "/w/x/y"))
            tr2.add("claude:b")
            self.assertTrue(tr2.matches("claude:b", None))
            self.assertEqual(model.Tracked(Path(t) / "tracked.json").data["untracked"], ["claude:a"])


class ProcsTest(unittest.TestCase):
    def procs(self, chain):
        out, ppid = {}, 1
        for pid, comm, argv in chain:
            out[pid] = procs.Proc(pid, ppid, comm, argv, 0.0, True)
            ppid = pid
        return out

    def test_mux_from_parent_process(self):
        with mock.patch.object(procs, "_environ", return_value={}):
            p = self.procs([(10, "zellij", ["/usr/bin/zellij", "--server", "/tmp/zellij-1/v1/overnight"]),
                            (11, "bash", ["bash"]), (12, "claude", ["claude", "-p", "x"])])
            self.assertEqual(procs.mux_of(12, p), ("zellij", "overnight"))
            p = self.procs([(20, "tmux: server", ["tmux"]), (21, "zsh", ["zsh"]), (22, "claude", ["claude"])])
            self.assertEqual(procs.mux_of(22, p), ("tmux", None))
            p = self.procs([(30, "sshd", ["sshd"]), (31, "bash", ["bash"]), (32, "claude", ["claude"])])
            self.assertEqual(procs.mux_of(32, p), (None, None))

    def test_mux_from_environment(self):
        p = self.procs([(40, "bash", ["bash"]), (41, "codex", ["codex"])])
        with mock.patch.object(procs, "_environ", return_value={"HERDR_PANE": "1"}):
            self.assertEqual(procs.mux_of(41, p), ("herdr", None))
        with mock.patch.object(procs, "_environ", return_value={"STY": "123.work"}):
            self.assertEqual(procs.mux_of(41, p), ("screen", "work"))

    def test_agent_process_and_resume_argument(self):
        sid = "11111111-2222-4333-8444-555555555555"
        self.assertEqual(procs._arg_session(["claude", "--resume", sid]), sid)
        self.assertEqual(procs._arg_session(["codex", "resume", sid]), sid)
        self.assertIsNone(procs._arg_session(["claude", "-p", "hello"]))
        mk = lambda argv: procs.Proc(1, 0, "x", argv, 0.0, True)  # noqa: E731
        self.assertEqual(mk(["/usr/local/bin/claude"]).agent, "claude")
        self.assertEqual(mk(["codex", "exec", "x"]).agent, "codex")
        self.assertIsNone(mk(["codex", "app-server"]).agent)
        self.assertIsNone(mk(["/x/codex-code-mode-host"]).agent)


class WrapUpTest(unittest.TestCase):
    def test_wrap_up_completes_remaining_steps(self):
        p = Plan()
        p.apply("mcp__ccd_turn__update_plan", {"items": [
            {"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "pending"}]}, "2026-10-29T01:00:00Z")
        p.apply("mcp__ccd_turn__wrap_up", {"recap": "x"}, "2026-10-29T01:05:00Z")
        self.assertEqual([s["status"] for s in p.steps], ["completed"] * 3)
        self.assertIsNone(p.current())
        self.assertFalse(p.finished_before("2026-10-29T01:04:00Z"))
        self.assertTrue(p.finished_before("2026-10-29T02:00:00Z"))  # 다음 턴에 계획 없음
        p.apply("mcp__ccd_turn__update_plan", {"items": [{"content": "d", "status": "in_progress"}]}, "2026-10-29T02:01:00Z")
        self.assertFalse(p.finished_before("2026-10-29T02:00:00Z"))  # 새 턴에서 계획을 세움


class CompletedTest(unittest.TestCase):
    def test_mark_done_and_reopen_on_new_activity(self):
        with tempfile.TemporaryDirectory() as t:
            tr = model.Tracked(Path(t) / "tracked.json")
            reg = object.__new__(model.Registry)
            reg.tracked = tr
            tr.set_completed("claude:a", True)
            mark = tr.data["completed"]["claude:a"]
            quiet = {"key": "claude:a", "status": "idle", "status_label": "x", "last_activity": mark - 100, "eta": [1, 2]}
            reg._apply_completed([quiet])
            self.assertEqual((quiet["status"], quiet["eta"]), ("done", None))
            busy = {"key": "claude:a", "status": "running", "status_label": "x", "last_activity": mark + 600}
            reg._apply_completed([busy])
            self.assertEqual(busy["status"], "running")  # 표시 뒤 새 활동이 있으면 다시 연다
            self.assertNotIn("claude:a", model.Tracked(Path(t) / "tracked.json").data["completed"])


class ProgressTest(unittest.TestCase):
    def test_active_step_fills_by_time_and_tools(self):
        t0 = model.parse_ts("2026-10-29T01:00:00Z")
        steps = [
            {"content": "a", "size": None, "status": "completed", "started": "2026-10-29T01:00:00Z", "ended": "2026-10-29T01:10:00Z", "tools": 20},
            {"content": "b", "size": None, "status": "in_progress", "started": "2026-10-29T01:10:00Z", "ended": None, "tools": 10},
            {"content": "c", "size": None, "status": "pending", "started": None, "ended": None},
        ]
        # b는 5분, 도구 10회: 끝난 단계(10분, 20회)의 절반 → 전체 (1 + 0.5) / 3
        self.assertAlmostEqual(model.progress(steps, now=t0 + 15 * 60), 0.5, places=2)
        # 아무리 오래 걸려도 진행 중 단계는 90%를 넘지 않는다
        steps[1]["tools"] = 999
        self.assertAlmostEqual(model.progress(steps, now=t0 + 10 * 3600), (1 + 0.9) / 3, places=2)

    def test_session_counts_keep_previous_turn_plans(self):
        p = Plan()
        p.apply("mcp__ccd_turn__update_plan", {"items": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "pending"}]}, "2026-10-29T01:00:00Z")
        p.apply("mcp__ccd_turn__wrap_up", {}, "2026-10-29T01:30:00Z")
        p.apply("mcp__ccd_turn__update_plan", {"items": [{"content": "c", "status": "in_progress"}]}, "2026-10-29T02:00:00Z")
        self.assertEqual([s["content"] for s in p.steps], ["c"])
        self.assertEqual(p.session_counts(), (2, 3))


class ContinuationTest(unittest.TestCase):
    def test_tracking_moves_to_continued_session(self):
        with tempfile.TemporaryDirectory() as t:
            tr = model.Tracked(Path(t) / "tracked.json")
            tr.add("claude:old")
            tr.set_completed("claude:old", True)
            tr.replace("claude:old", "claude:new")
            d = model.Tracked(Path(t) / "tracked.json").data
            self.assertEqual(d["sessions"], ["claude:new"])
            self.assertEqual(d["untracked"], [])  # 사용자가 해제한 것이 아니다
            self.assertIn("claude:new", d["completed"])


class LaunchTest(unittest.TestCase):
    def test_launch_is_remote_or_cli(self):
        L = model.launch_of
        self.assertEqual(L("claude", "claude-desktop"), "remote")
        self.assertEqual(L("claude", "claude-desktop", headless=True), "cli")  # 앱 세션이 실행한 claude -p
        self.assertEqual(L("claude", "cli"), "cli")
        self.assertEqual(L("claude", "sdk-cli"), "cli")
        self.assertEqual(L("codex", "codex-tui", "vscode"), "remote")  # Codex 앱, IDE 확장
        self.assertEqual(L("codex", "codex-tui", "cli"), "cli")
        self.assertEqual(L("codex", "codex_exec", "exec"), "cli")
        self.assertEqual(L("codex", "codex_exec"), "cli")


class TunnelTest(unittest.TestCase):
    def test_tunnel_uses_ssh_connection(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.1 50000 192.0.2.10 2222"}):
            self.assertEqual(model.tunnel_command(24001, "alex"), "ssh -f -N -p 2222 -L 24001:127.0.0.1:24001 alex@192.0.2.10")
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.1 50000 192.0.2.10 22"}):
            self.assertEqual(model.tunnel_command(24001, "alex"), "ssh -f -N -L 24001:127.0.0.1:24001 alex@192.0.2.10")


class UpdatesTest(unittest.TestCase):
    def test_version_compare_and_notice(self):
        from agentdash import updates
        self.assertTrue(updates.newer("0.1.2", "0.1.1"))
        self.assertTrue(updates.newer("0.10.0", "0.9.9"))
        self.assertFalse(updates.newer("0.1.1", "0.1.1"))
        self.assertFalse(updates.newer(None, "0.1.1"))
        with mock.patch.object(updates, "latest", return_value="9.9.9"):
            self.assertIn("agent-dash update", updates.notice())
        with mock.patch.object(updates, "latest", return_value=updates.__version__):
            self.assertIsNone(updates.notice())

    def test_disabled_by_environment(self):
        from agentdash import updates
        with mock.patch.dict(os.environ, {"AGENT_DASH_NO_UPDATE_CHECK": "1"}):
            self.assertIsNone(updates.latest(force=True))


if __name__ == "__main__":
    unittest.main()
