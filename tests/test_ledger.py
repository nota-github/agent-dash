"""원복 테스트. 임시 홈에 설정 파일을 만들고 설치, 원복, 비교를 실행한다."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdash import cli  # noqa: E402
from agentdash import ledger as L  # noqa: E402

SETTINGS = {
    "extraKnownMarketplaces": {
        "claude-plugins-official": {"source": {"source": "github", "repo": "anthropics/claude-plugins-official"}},
    },
    "theme": "dark",
}
CONFIG_TOML = 'model = "gpt-5"\n\n[projects."/work"]\ntrust_level = "trusted"\n'


def make_home(base: Path) -> Path:
    home = base / "home"
    (home / ".claude/skills").mkdir(parents=True)
    (home / ".claude/projects/-shared-workspace").mkdir(parents=True)
    (home / ".codex/skills").mkdir(parents=True)
    (home / ".codex/sessions/2026/10/29").mkdir(parents=True)
    s = home / ".claude/settings.json"
    s.write_text(json.dumps(SETTINGS, indent=2) + "\n")
    os.chmod(s, 0o664)
    c = home / ".codex/config.toml"
    c.write_text(CONFIG_TOML)
    os.chmod(c, 0o600)
    (home / ".claude/projects/-shared-workspace/abc.jsonl").write_text('{"type":"user"}\n')
    (home / ".codex/sessions/2026/10/29/rollout-1.jsonl").write_text('{"type":"session_meta"}\n')
    return home


def tree(root: Path, skip: str | None = None) -> dict:
    """경로별 종류, 권한, 내용, ACL."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if skip and skip in dirnames:
            dirnames.remove(skip)
        for name in dirnames + sorted(filenames):
            p = Path(dirpath) / name
            st = os.lstat(p)
            entry = [stat.S_IMODE(st.st_mode), tuple(L.acl_entries(L.get_acl(p)))]
            entry.append("dir" if stat.S_ISDIR(st.st_mode) else p.read_bytes())
            out[p.relative_to(root).as_posix()] = entry
    return out


def sample_actions(home: Path) -> list[dict]:
    hook = {"matcher": "*", "hooks": [{"type": "command", "command": "[ -x /x/collect ] && /x/collect || true"}]}
    return [
        {"id": "t/dir", "op": "mkdir", "path": str(home / ".agentdash/events")},
        {"id": "t/hook", "op": "json_append", "path": str(home / ".claude/settings.json"),
         "keys": ["hooks", "PostToolUse"], "item": hook},
        {"id": "t/env", "op": "json_set", "path": str(home / ".claude/settings.json"),
         "keys": ["env", "AGENTDASH_HOME"], "value": str(home / ".agentdash")},
        {"id": "t/skill-dir", "op": "mkdir", "path": str(home / ".claude/skills/track")},
        {"id": "t/skill", "op": "write_file", "path": str(home / ".claude/skills/track/SKILL.md"),
         "content": "---\nname: track\n---\n규칙\n"},
        {"id": "t/codex-hooks", "op": "write_file", "path": str(home / ".codex/hooks.json"), "content": "{}\n"},
        {"id": "t/codex-trust", "op": "text_block", "path": str(home / ".codex/config.toml"),
         "block_id": "hooks", "text": "[hooks]\ntrusted = true\n"},
    ]


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self._tmp.name))
        self.home = make_home(self.base)
        self.backup = self.base / "backup"
        self.before = tree(self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def install(self, actions=None) -> L.Ledger:
        lg = L.Ledger(self.home, self.backup)
        for a in actions or sample_actions(self.home):
            lg.apply(a)
        lg.record_post()
        return lg

    def test_roundtrip_restores_every_byte(self):
        lg = self.install()
        self.assertNotEqual(tree(self.home), self.before)
        rep = lg.uninstall()
        self.assertEqual(rep.failed, [])
        self.assertEqual(rep.partial, [])
        self.assertEqual(tree(self.home), self.before)
        lg.close()
        self.assertFalse((lg.target_dir / "ACTIVE").exists())

    def test_dry_run_changes_nothing(self):
        lg = L.Ledger(self.home, self.backup)
        for a in sample_actions(self.home):
            lg.apply(a, dry_run=True)
        self.assertEqual(tree(self.home), self.before)
        self.assertFalse(self.backup.exists())

    def test_unrelated_changes_are_kept_by_default(self):
        lg = self.install()
        s = self.home / ".claude/settings.json"
        doc = json.loads(s.read_text())
        doc["model"] = "opus"
        s.write_text(json.dumps(doc, indent=2) + "\n")
        c = self.home / ".codex/config.toml"
        c.write_text(c.read_text() + 'approval_policy = "never"\n')
        rep = lg.uninstall()
        self.assertEqual(rep.failed, [])
        after = json.loads(s.read_text())
        self.assertEqual(after, {**SETTINGS, "model": "opus"})
        self.assertEqual(c.read_text(), CONFIG_TOML + 'approval_policy = "never"\n')
        self.assertEqual({p for p, _ in rep.partial}, {str(s), str(c)})
        now = tree(self.home)
        for rel in now:
            if rel not in (".claude/settings.json", ".codex/config.toml"):
                self.assertEqual(now[rel], self.before[rel], rel)
        self.assertEqual(set(now), set(self.before))

    def test_exact_restores_original_even_after_unrelated_changes(self):
        lg = self.install()
        s = self.home / ".claude/settings.json"
        s.write_text(s.read_text().replace('"dark"', '"light"'))
        os.chmod(s, 0o600)
        rep = lg.uninstall(exact=True)
        self.assertEqual(rep.failed, [])
        self.assertEqual(rep.partial, [])
        self.assertEqual(tree(self.home), self.before)

    def test_app_replaces_file_with_new_inode(self):
        lg = self.install()
        s = self.home / ".claude/settings.json"
        data = s.read_bytes()
        tmp = s.with_name("settings.json.tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, s)
        rep = lg.uninstall(exact=True)
        self.assertEqual(rep.failed, [])
        self.assertEqual(tree(self.home), self.before)

    def test_install_is_idempotent(self):
        lg = self.install()
        n = len(lg.ops())
        for a in sample_actions(self.home):
            self.assertTrue(lg.apply(a).startswith("건너뜀"))
        self.assertEqual(len(lg.ops()), n)
        lg.record_post()
        lg.uninstall()
        self.assertEqual(tree(self.home), self.before)

    def test_same_id_with_different_content_is_rejected(self):
        lg = self.install()
        a = sample_actions(self.home)[2]
        a["value"] = "/other"
        with self.assertRaises(L.LedgerError):
            lg.apply(a)

    def test_forbidden_and_outside_paths_are_rejected(self):
        lg = L.Ledger(self.home, self.backup)
        bad = [
            {"id": "x1", "op": "write_file", "path": str(self.home / ".claude/projects/x.jsonl"), "content": ""},
            {"id": "x2", "op": "write_file", "path": str(self.home / ".codex/sessions/x.jsonl"), "content": ""},
            {"id": "x3", "op": "write_file", "path": str(self.home / ".bashrc"), "content": ""},
            {"id": "x4", "op": "write_file", "path": str(self.base / "elsewhere.txt"), "content": ""},
            {"id": "x5", "op": "mkdir", "path": str(self.home / ".claude/agents/x")},
        ]
        for a in bad:
            with self.assertRaises(L.LedgerError, msg=a["id"]):
                lg.apply(a)
        self.assertEqual(tree(self.home), self.before)

    def test_symlinked_path_is_rejected(self):
        real = self.base / "real-skills"
        real.mkdir()
        os.symlink(real, self.home / ".codex/skills/track")
        lg = L.Ledger(self.home, self.backup)
        with self.assertRaises(L.LedgerError):
            lg.apply({"id": "s", "op": "write_file", "path": str(self.home / ".codex/skills/track/SKILL.md"), "content": "x"})

    def test_interrupted_install_is_rolled_back(self):
        lg = L.Ledger(self.home, self.backup)
        actions = sample_actions(self.home)
        for a in actions[:3]:
            lg.apply(a)
        real_append = lg._append

        def crash_on_done(rec):
            if rec["status"] == "done":
                raise KeyboardInterrupt
            real_append(rec)

        with mock.patch.object(lg, "_append", side_effect=crash_on_done):
            with self.assertRaises(KeyboardInterrupt):
                lg.apply(actions[5])
        self.assertTrue((self.home / ".codex/hooks.json").exists())
        lg2 = L.Ledger(self.home, self.backup)
        rep = lg2.uninstall()
        self.assertEqual(rep.failed, [])
        self.assertEqual(tree(self.home), self.before)

    @unittest.skipUnless(L.have_acl_tools(), "getfacl, setfacl 없음")
    def test_acl_change_is_restored(self):
        probe = subprocess.run(["setfacl", "-m", f"u:{os.getuid()}:rwx", str(self.home)], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("이 파일시스템은 ACL을 지원하지 않음")
        subprocess.run(["setfacl", "-x", f"u:{os.getuid()}", str(self.home)], check=True)
        subprocess.run(["setfacl", "-k", str(self.home)], check=True)
        before = L.acl_entries(L.get_acl(self.home))
        lg = self.install([
            {"id": "acl", "op": "acl_modify", "path": str(self.home), "spec": f"u:{os.getuid()}:x"},
            {"id": "acl2", "op": "acl_modify", "path": str(self.home / ".claude/projects"), "spec": f"u:{os.getuid()}:rx"},
        ])
        self.assertNotEqual(L.acl_entries(L.get_acl(self.home)), before)
        rep = lg.uninstall()
        self.assertEqual(rep.failed, [])
        self.assertEqual(L.acl_entries(L.get_acl(self.home)), before)
        self.assertEqual(tree(self.home), self.before)


class CliTest(unittest.TestCase):
    def test_install_uninstall_purge_leaves_no_trace(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(os.path.realpath(t))
            home = make_home(base)
            before = tree(home)
            notes = base / "notes"
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                self.assertEqual(cli.main(["--home", str(home), "install", "--dry-run", "base"]), 0)
                self.assertEqual(tree(home), before)
                self.assertEqual(cli.main(["--home", str(home), "install", "base"]), 0)
                self.assertTrue((home / ".agentdash").is_dir())
                self.assertTrue((home / ".agentdash-backup").is_dir())
                self.assertEqual(cli.main(["--home", str(home), "status"]), 0)
                self.assertEqual(cli.main(["--home", str(home), "uninstall", "--report-dir", str(notes)]), 0)
                self.assertEqual(cli.main(["--home", str(home), "purge-backup", "--yes"]), 0)
            self.assertEqual(tree(home), before, out.getvalue())
            reports = list(notes.glob("rollback-*.md"))
            self.assertEqual(len(reports), 1)
            self.assertIn("판정: 원복 완료", reports[0].read_text())

    def test_app_install_and_uninstall_keep_shared_folders(self):
        repo = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as t:
            home = make_home(Path(os.path.realpath(t)))
            before = tree(home)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                self.assertEqual(cli.main(["--home", str(home), "install", "app", "--src", str(repo),
                                           "--mode", "tui", "--port", "24001"]), 0, out.getvalue())
            launcher = home / ".local/bin/agent-dash"
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertIn("-I -B", launcher.read_text())
            app = home / ".local/share/agent-dash/agentdash"
            self.assertTrue((app / "tui.py").exists())
            self.assertFalse((app / "server.py").exists())  # 터미널 화면만 고르면 웹 서버는 설치하지 않는다
            self.assertEqual(json.loads((home / ".agentdash/config.json").read_text())["mode"], "tui")
            # 설치 뒤 다른 프로그램이 같은 폴더에 파일을 둔 경우
            (home / ".local/bin/other-tool").write_text("x")
            with redirect_stdout(out), redirect_stderr(out):
                self.assertEqual(cli.main(["--home", str(home), "uninstall", "--yes", "--purge-backup"]), 0, out.getvalue())
            self.assertTrue((home / ".local/bin/other-tool").exists())
            self.assertFalse(launcher.exists())
            self.assertFalse((home / ".local/share").exists())
            (home / ".local/bin/other-tool").unlink()
            (home / ".local/bin").rmdir()
            (home / ".local").rmdir()
            self.assertEqual(tree(home), before)

    def test_shared_folder_is_only_creatable_not_writable(self):
        with tempfile.TemporaryDirectory() as t:
            home = make_home(Path(os.path.realpath(t)))
            lg = L.Ledger(home, home / ".agentdash-backup")
            lg.check_path(home / ".local/bin", mkdir=True)
            with self.assertRaises(L.LedgerError):
                lg.check_path(home / ".local/bin/other-tool")


if __name__ == "__main__":
    unittest.main()
