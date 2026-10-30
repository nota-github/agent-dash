"""설치 변경 기록과 원복.

설치가 바꾸는 파일은 처음 손대기 전에 원래 상태(내용, 권한, 소유자, ACL)를 백업한다.
모든 변경은 ledger.jsonl에 한 줄씩 남긴다. 원복은 다음 순서로 한다.

1. 설치 직후 상태(post)와 지금 상태가 같으면 백업한 원본으로 그대로 되돌린다.
2. 설치 후 다른 프로그램이 파일을 바꿨으면 agent-dash가 추가한 부분만 되돌리고, 차이를 보고한다.
   --exact를 주면 그 경우에도 원본으로 되돌린다.
"""

from __future__ import annotations

import copy
import difflib
import grp
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = 1

# 홈 기준으로 설치가 쓸 수 있는 경로. 이 밖은 거부한다.
ALLOWED_REL = (
    ".agentdash",
    ".claude/settings.json",
    ".claude/skills/track",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".codex/skills/track",
    ".local/share/agent-dash",
    ".local/bin/agent-dash",
)
# 다른 프로그램과 함께 쓰는 폴더. 없을 때 만들 수만 있고, 원복할 때는 비어 있을 때만 지운다.
SHARED_DIR_REL = (".local", ".local/share", ".local/bin")
# ACL만 바꿀 수 있는 경로. 빈 문자열은 홈 자체.
ACL_ALLOWED_REL = ("", ".claude", ".claude/projects")
# 어떤 경우에도 쓰지 않는 경로. transcript와 rollout.
FORBIDDEN_REL = (".claude/projects", ".codex/sessions")

# 설치 전후 비교용 목록에서 제외하는 이름. 앱이 계속 바꾸는 파일이 대부분이다.
INVENTORY_ROOTS = (".claude", ".codex", ".agentdash")
INVENTORY_SKIP_DIRS = {
    "projects", "sessions", "remote", "cache", "packages", "shell_snapshots",
    "file-history", "todos", "statsig", "logs", "tmp", "session-env", "debug",
    "ide", "plans", "paste-cache", "telemetry", "thread-writer-locks",
}
INVENTORY_SKIP_SUFFIX = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".log", ".lock")
HASH_LIMIT = 2 * 1024 * 1024


class LedgerError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def target_key(home: Path) -> str:
    return str(home).strip("/").replace("/", "-") or "root"


def now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def have_acl_tools() -> bool:
    return shutil.which("getfacl") is not None and shutil.which("setfacl") is not None


def get_acl(path: Path) -> str | None:
    if not have_acl_tools() or not path.exists():
        return None
    r = subprocess.run(["getfacl", "-p", "--", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise LedgerError(f"getfacl 실패: {path}: {r.stderr.strip()}")
    return r.stdout


def restore_acl(dump: str, workdir: Path) -> None:
    f = workdir / f"acl-restore-{os.getpid()}.txt"
    f.write_text(dump)
    try:
        r = subprocess.run(["setfacl", f"--restore={f}"], capture_output=True, text=True)
        if r.returncode != 0:
            raise LedgerError(f"setfacl --restore 실패: {r.stderr.strip()}")
    finally:
        f.unlink(missing_ok=True)


def acl_entries(dump: str | None) -> list[str]:
    """getfacl 출력에서 주석과 effective 표시를 뺀 항목 목록."""
    if not dump:
        return []
    out = []
    for line in dump.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def write_inplace(path: Path, data: bytes, mode: int = 0o644) -> None:
    """inode를 유지한 채 내용을 바꾼다. 기존 파일의 권한과 ACL이 그대로 남는다."""
    if path.exists():
        with open(path, "r+b") as f:
            f.write(data)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())


def dump_json(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


_MISSING = object()


def json_get(doc, keys):
    cur = doc
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return _MISSING
        cur = cur[k]
    return cur


def json_ensure_parent(doc: dict, keys: list[str]) -> int:
    """마지막 키의 부모 dict를 만든다. 새로 만든 단계 수를 돌려준다."""
    created = 0
    cur = doc
    for k in keys[:-1]:
        if k not in cur:
            cur[k] = {}
            created += 1
        if not isinstance(cur[k], dict):
            raise LedgerError(f"JSON 경로 중간 값이 객체가 아님: {'.'.join(keys)}")
        cur = cur[k]
    return created


def json_prune(doc: dict, keys: list[str], depth: int) -> None:
    """원복 시 설치가 만든 빈 부모 dict와 빈 목록을 지운다. depth는 마지막 키부터 지울 수 있는 단계 수."""
    for n in range(len(keys), len(keys) - depth, -1):
        parent = json_get(doc, keys[: n - 1]) if n > 1 else doc
        if parent is _MISSING or not isinstance(parent, dict):
            return
        k = keys[n - 1]
        if k in parent and parent[k] in ({}, []):
            del parent[k]
        else:
            return


def text_block(block_id: str, text: str) -> str:
    body = text if text.endswith("\n") else text + "\n"
    return f"# agentdash:begin {block_id}\n{body}# agentdash:end {block_id}\n"


def strip_text_block(content: str, block_id: str) -> tuple[str, bool]:
    begin = f"# agentdash:begin {block_id}\n"
    end = f"# agentdash:end {block_id}\n"
    i = content.find(begin)
    if i < 0:
        return content, False
    j = content.find(end, i)
    if j < 0:
        raise LedgerError(f"블록 끝 표시가 없음: {block_id}")
    j += len(end)
    # 설치할 때 블록 앞에 넣은 줄바꿈 하나도 함께 지운다.
    if i > 0 and content[i - 1] == "\n" and content[i - 2 : i] == "\n\n":
        i -= 1
    return content[:i] + content[j:], True


def file_state(path: Path) -> dict:
    """비교용 현재 상태."""
    if not os.path.lexists(path):
        return {"exists": False}
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return {"exists": True, "kind": "symlink", "target": os.readlink(path)}
    d = {
        "exists": True,
        "mode": stat.S_IMODE(st.st_mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "acl": get_acl(path),
    }
    if stat.S_ISDIR(st.st_mode):
        d["kind"] = "dir"
    else:
        d["kind"] = "file"
        d["sha256"] = sha256_file(path)
    return d


def describe_owner(uid: int, gid: int) -> str:
    try:
        u = pwd.getpwuid(uid).pw_name
    except KeyError:
        u = str(uid)
    try:
        g = grp.getgrgid(gid).gr_name
    except KeyError:
        g = str(gid)
    return f"{u}:{g}"


def inventory(home: Path) -> dict[str, str]:
    """설정 파일 목록. 경로별로 sha256 또는 크기와 수정 시각."""
    out: dict[str, str] = {}
    for root_rel in INVENTORY_ROOTS:
        base = home / root_rel
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = Path(dirpath).relative_to(home).as_posix()
            if rel_dir in FORBIDDEN_REL:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if d not in INVENTORY_SKIP_DIRS)
            out[rel_dir + "/"] = "dir"
            for name in sorted(filenames):
                if name.endswith(INVENTORY_SKIP_SUFFIX):
                    continue
                p = Path(dirpath) / name
                try:
                    st = os.lstat(p)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    out[f"{rel_dir}/{name}"] = "link:" + os.readlink(p)
                elif st.st_size <= HASH_LIMIT:
                    try:
                        out[f"{rel_dir}/{name}"] = sha256_file(p)
                    except (PermissionError, FileNotFoundError):
                        out[f"{rel_dir}/{name}"] = f"unreadable:{st.st_size}"
                else:
                    out[f"{rel_dir}/{name}"] = f"size:{st.st_size}:mtime:{int(st.st_mtime)}"
    return out


def inventory_diff(before: dict, after: dict) -> dict[str, list[str]]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }


@dataclass
class Report:
    restored: list[str] = field(default_factory=list)
    partial: list[tuple[str, str]] = field(default_factory=list)  # 경로, diff
    failed: list[tuple[str, str]] = field(default_factory=list)  # 경로, 사유
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.partial


class Ledger:
    """한 대상(홈 디렉터리)의 설치 기록. backup_root/<대상>/<시각>/ 하나가 설치 한 건이다."""

    def __init__(self, home: Path, backup_root: Path):
        self.home = Path(os.path.abspath(home))
        self.backup_root = Path(os.path.abspath(backup_root))
        self.target_dir = self.backup_root / target_key(self.home)
        self.dir: Path | None = None
        self._planned: set[Path] = set()  # 미리보기에서 앞 작업이 만들 폴더
        active = self.target_dir / "ACTIVE"
        if active.exists():
            self.dir = self.target_dir / active.read_text().strip()

    # ---------- 경로 검사 ----------

    def rel(self, path: Path) -> str:
        p = Path(os.path.abspath(path))
        if p == self.home:
            return ""
        try:
            return p.relative_to(self.home).as_posix()
        except ValueError:
            raise LedgerError(f"홈 밖의 경로는 쓸 수 없음: {p}")

    def check_path(self, path: Path, acl: bool = False, mkdir: bool = False) -> None:
        p = Path(os.path.abspath(path))
        if os.path.realpath(p) != str(p):
            raise LedgerError(f"심볼릭 링크가 포함된 경로는 쓸 수 없음: {p}")
        rel = self.rel(p)
        for f in FORBIDDEN_REL:
            if rel == f or rel.startswith(f + "/"):
                if not (acl and rel == f):
                    raise LedgerError(f"쓰기 금지 경로: {p}")
        if acl and rel in ACL_ALLOWED_REL:
            return
        if mkdir and rel in SHARED_DIR_REL:
            return
        for a in ALLOWED_REL:
            if rel == a or rel.startswith(a + "/"):
                return
        raise LedgerError(f"허용 목록에 없는 경로: {p}")

    # ---------- 기록 파일 ----------

    def _require(self) -> Path:
        if self.dir is None:
            raise LedgerError("진행 중인 설치 기록이 없음")
        return self.dir

    def open_or_create(self) -> Path:
        if self.dir is not None:
            return self.dir
        stamp = now_stamp()
        d = self.target_dir / stamp
        new_root = not self.backup_root.exists()
        (d / "files").mkdir(parents=True, exist_ok=False)
        for p in (self.backup_root, self.target_dir, d):
            os.chmod(p, 0o700)
        if new_root and os.geteuid() == 0:
            # root로 먼저 설치해도 백업 위치는 그 위치의 상위 폴더 소유자가 쓸 수 있어야 한다.
            st = os.stat(self.backup_root.parent)
            os.chown(self.backup_root, st.st_uid, st.st_gid)
        (d / "meta.json").write_bytes(dump_json({
            "format": FORMAT,
            "home": str(self.home),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "uid": os.getuid(),
        }))
        (d / "pre.json").write_bytes(dump_json({}))
        (d / "post.json").write_bytes(dump_json({}))
        (d / "inventory-pre.json").write_bytes(dump_json(inventory(self.home)))
        (self.target_dir / "ACTIVE").write_text(stamp + "\n")
        self.dir = d
        return d

    def _load(self, name: str):
        return json.loads((self._require() / name).read_text())

    def _save(self, name: str, value) -> None:
        write_atomic(self._require() / name, dump_json(value))

    def ops(self) -> list[dict]:
        f = self._require() / "ledger.jsonl"
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]

    def _append(self, rec: dict) -> None:
        with open(self._require() / "ledger.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # ---------- 설치 전 상태 ----------

    def capture_pre(self, path: Path) -> None:
        """처음 손대는 경로의 원래 상태를 저장한다. 이미 저장했으면 그대로 둔다."""
        pre = self._load("pre.json")
        key = str(path)
        if key in pre:
            return
        st = file_state(path)
        if st.get("kind") == "symlink":
            raise LedgerError(f"심볼릭 링크는 다루지 않음: {path}")
        if st.get("kind") == "file":
            name = f"{len(pre):04d}"
            data = path.read_bytes()
            write_atomic(self._require() / "files" / name, data)
            st["backup"] = name
        pre[key] = st
        self._save("pre.json", pre)

    def record_post(self) -> None:
        pre = self._load("pre.json")
        self._save("post.json", {k: file_state(Path(k)) for k in pre})
        n = len(list(self._require().glob("inventory-post-*.json")))
        self._save(f"inventory-post-{n + 1}.json", inventory(self.home))

    # ---------- 작업 적용 ----------

    def applied(self) -> dict[str, dict]:
        return {r["id"]: r for r in self.ops() if r.get("status") == "done"}

    def apply(self, action: dict, dry_run: bool = False) -> str:
        """작업 하나를 적용하고 결과 설명을 돌려준다."""
        op = action["op"]
        path = Path(action["path"])
        self.check_path(path, acl=(op == "acl_modify"), mkdir=(op == "mkdir"))
        done = self.applied() if self.dir else {}
        if action["id"] in done:
            if done[action["id"]]["action"] != action:
                raise LedgerError(f"이미 적용된 작업과 내용이 다름: {action['id']}. 원복 후 다시 설치하거나 새 id를 써야 함")
            return f"건너뜀 (이미 적용): {action['id']}"
        handler = getattr(self, "_op_" + op, None)
        if handler is None:
            raise LedgerError(f"알 수 없는 작업 종류: {op}")
        if dry_run:
            return handler(action, path, dry_run=True)
        self.open_or_create()
        seq = len(self.ops()) + 1
        self._append({"seq": seq, "id": action["id"], "status": "intent", "action": action})
        detail = handler(action, path, dry_run=False)
        self._append({"seq": seq, "id": action["id"], "status": "done", "action": action, "detail": detail})
        return f"적용: {action['id']}"

    def _op_mkdir(self, action, path: Path, dry_run: bool):
        missing = []
        p = path
        while not p.exists() and p not in self._planned:
            missing.append(p)
            p = p.parent
        if dry_run:
            self._planned.update(missing)
            return f"폴더 생성: {', '.join(str(m) for m in reversed(missing))}" if missing else f"이미 있음: {path}"
        for m in reversed(missing):
            self.check_path(m, mkdir=True)
            self.capture_pre(m)
            m.mkdir(mode=action.get("mode", 0o755))
        return {"created": [str(m) for m in reversed(missing)]}

    def _op_write_file(self, action, path: Path, dry_run: bool):
        data = action["content"].encode()
        if dry_run:
            if not path.exists():
                return f"파일 생성: {path} ({len(data)} bytes)"
            old = path.read_text(errors="replace")
            return f"파일 변경: {path}\n" + unified(old, action["content"], str(path))
        if not path.parent.exists():
            raise LedgerError(f"상위 폴더가 없음: {path.parent}. mkdir 작업을 먼저 둬야 함")
        self.capture_pre(path)
        write_inplace(path, data, action.get("mode", 0o644))
        return {"sha256": sha256_bytes(data)}

    def _json_load(self, path: Path):
        if not path.exists():
            return {}
        text = path.read_text()
        return json.loads(text) if text.strip() else {}

    def _op_json_set(self, action, path: Path, dry_run: bool):
        doc = self._json_load(path)
        before = copy.deepcopy(doc)
        keys = action["keys"]
        prev = json_get(doc, keys)
        created = json_ensure_parent(doc, keys)
        json_get(doc, keys[:-1])[keys[-1]] = action["value"]
        if dry_run:
            return f"JSON 변경: {path}\n" + unified(dump_json(before).decode(), dump_json(doc).decode(), str(path))
        self.capture_pre(path)
        write_inplace(path, dump_json(doc), 0o644)
        return {"prev_exists": prev is not _MISSING, "prev": None if prev is _MISSING else prev, "created_parents": created}

    def _op_json_append(self, action, path: Path, dry_run: bool):
        doc = self._json_load(path)
        before = copy.deepcopy(doc)
        keys = action["keys"]
        created = json_ensure_parent(doc, keys)
        parent = json_get(doc, keys[:-1])
        created_list = keys[-1] not in parent
        lst = parent.setdefault(keys[-1], [])
        if not isinstance(lst, list):
            raise LedgerError(f"목록이 아님: {'.'.join(keys)}")
        lst.append(action["item"])
        if dry_run:
            return f"JSON 항목 추가: {path}\n" + unified(dump_json(before).decode(), dump_json(doc).decode(), str(path))
        self.capture_pre(path)
        write_inplace(path, dump_json(doc), 0o644)
        return {"created_parents": created + (1 if created_list else 0)}

    def _op_text_block(self, action, path: Path, dry_run: bool):
        old = path.read_text() if path.exists() else ""
        if f"# agentdash:begin {action['block_id']}\n" in old:
            raise LedgerError(f"같은 블록이 이미 있음: {action['block_id']} in {path}")
        sep = "" if old == "" or old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
        new = old + sep + text_block(action["block_id"], action["text"])
        if dry_run:
            return f"텍스트 블록 추가: {path}\n" + unified(old, new, str(path))
        self.capture_pre(path)
        write_inplace(path, new.encode(), action.get("mode", 0o644))
        return {}

    def _op_acl_modify(self, action, path: Path, dry_run: bool):
        if not have_acl_tools():
            raise LedgerError("getfacl, setfacl이 없음")
        if dry_run:
            return f"ACL 추가: {path} {action['spec']}"
        self.capture_pre(path)
        args = ["setfacl", "-m", action["spec"], "--", str(path)]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise LedgerError(f"setfacl 실패: {r.stderr.strip()}")
        return {}

    # ---------- 원복 ----------

    def uninstall(self, exact: bool = False) -> Report:
        d = self._require()
        pre = self._load("pre.json")
        post = self._load("post.json")
        ops = [r for r in self.ops() if r["status"] == "done"]
        intents = {r["seq"] for r in self.ops() if r["status"] == "intent"}
        dones = {r["seq"] for r in ops}
        rep = Report()
        if intents - dones:
            rep.notes.append(f"중간에 끊긴 작업 {len(intents - dones)}건이 있음. 원본 상태 기준으로 처리함")

        # 설치 후 상태와 같은 경로는 원본으로 바로 되돌린다. 다른 경로는 작업 단위로 되돌린다.
        changed_since = {k for k in pre if not same_state(file_state(Path(k)), post.get(k, {"exists": False}))}
        # 기록은 남았지만 post가 없는 경로(설치가 끊긴 경우)는 원본 복원 대상으로 본다.
        changed_since -= {k for k in pre if k not in post}
        semantic = set() if exact else changed_since

        for rec in reversed(ops):
            a = rec["action"]
            if a["path"] in semantic and a["op"] in ("json_set", "json_append", "text_block", "write_file"):
                self._inverse_semantic(rec, rep)

        # 원본 복원. 폴더는 깊은 것부터 지운다.
        for key in sorted(pre, key=lambda k: -len(k)):
            if key in semantic:
                continue
            self._restore_exact(Path(key), pre[key], d, rep)

        # 검증
        for key in sorted(pre):
            now = file_state(Path(key))
            want = pre[key]
            if not want["exists"] and self.rel(Path(key)) in SHARED_DIR_REL and Path(key).is_dir():
                rep.notes.append(f"{key}: 설치 때 만든 폴더지만 다른 파일이 있어 그대로 둠")
                rep.restored.append(key)
            elif same_state(now, want):
                rep.restored.append(key)
            elif key in semantic:
                rep.partial.append((key, self._diff_against_backup(Path(key), want, d)))
            else:
                rep.failed.append((key, explain_difference(want, now)))
        return rep

    def _restore_exact(self, path: Path, want: dict, d: Path, rep: Report) -> None:
        try:
            if not want["exists"] and self.rel(path) in SHARED_DIR_REL:
                if path.is_dir() and not path.is_symlink():
                    try:
                        path.rmdir()  # 비어 있을 때만 지운다
                    except OSError:
                        pass
                return
            if not want["exists"]:
                if os.path.lexists(path):
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                return
            if want["kind"] == "dir":
                if not path.exists():
                    path.mkdir(mode=want["mode"])
            else:
                data = (d / "files" / want["backup"]).read_bytes()
                write_inplace(path, data, want["mode"])
            st = os.lstat(path)
            if stat.S_IMODE(st.st_mode) != want["mode"]:
                os.chmod(path, want["mode"])
            if (st.st_uid, st.st_gid) != (want["uid"], want["gid"]) and os.geteuid() == 0:
                os.chown(path, want["uid"], want["gid"])
            if want.get("acl") is not None and acl_entries(get_acl(path)) != acl_entries(want["acl"]):
                restore_acl(want["acl"], d)
        except (OSError, LedgerError) as e:
            rep.failed.append((str(path), f"복원 실패: {e}"))

    def _inverse_semantic(self, rec: dict, rep: Report) -> None:
        a = rec["action"]
        path = Path(a["path"])
        detail = rec.get("detail") or {}
        if not path.exists():
            return
        if a["op"] == "write_file":
            if sha256_file(path) == detail.get("sha256"):
                rep.notes.append(f"{path}: agent-dash가 쓴 내용 그대로라 원본 기준으로 처리함")
                self._restore_exact(path, self._load("pre.json")[str(path)], self._require(), rep)
            else:
                rep.notes.append(f"{path}: 설치 후 내용이 바뀌어 그대로 둠")
            return
        pre_exists = self._load("pre.json")[str(path)]["exists"]
        if a["op"] == "text_block":
            text = path.read_text()
            new, found = strip_text_block(text, a["block_id"])
            if found:
                if new == "" and not pre_exists:
                    path.unlink()
                else:
                    write_inplace(path, new.encode())
            return
        doc = self._json_load(path)
        keys = a["keys"]
        if a["op"] == "json_set":
            parent = json_get(doc, keys[:-1])
            if parent is _MISSING or json_get(doc, keys) != a["value"]:
                rep.notes.append(f"{path}: {'.'.join(keys)} 값이 설치 후 바뀌어 그대로 둠")
                return
            if detail.get("prev_exists"):
                parent[keys[-1]] = detail.get("prev")
            else:
                del parent[keys[-1]]
                json_prune(doc, keys[:-1], detail.get("created_parents", 0))
        else:
            lst = json_get(doc, keys)
            if isinstance(lst, list) and a["item"] in lst:
                lst.remove(a["item"])
                json_prune(doc, keys, detail.get("created_parents", 0))
            else:
                rep.notes.append(f"{path}: {'.'.join(keys)}에서 agent-dash 항목을 찾지 못함")
                return
        if doc == {} and not pre_exists:
            path.unlink()
        else:
            write_inplace(path, dump_json(doc))

    def _diff_against_backup(self, path: Path, want: dict, d: Path) -> str:
        if want.get("kind") == "file" and path.exists():
            old = (d / "files" / want["backup"]).read_text(errors="replace")
            return unified(old, path.read_text(errors="replace"), str(path))
        return explain_difference(want, file_state(path))

    def close(self) -> Path:
        d = self._require()
        (d / "CLOSED").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
        (self.target_dir / "ACTIVE").unlink()
        self.dir = None
        return d


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def unified(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=name + " (원본)", tofile=name + " (현재)",
    ))


def same_state(a: dict, b: dict) -> bool:
    if a.get("exists") != b.get("exists"):
        return False
    if not a.get("exists"):
        return True
    keys = ("kind", "mode", "uid", "gid", "sha256", "target")
    if any(a.get(k) != b.get(k) for k in keys):
        return False
    return acl_entries(a.get("acl")) == acl_entries(b.get("acl"))


def explain_difference(want: dict, now: dict) -> str:
    if want.get("exists") != now.get("exists"):
        return "원본에는 " + ("있음" if want.get("exists") else "없음") + ", 지금은 " + ("있음" if now.get("exists") else "없음")
    parts = []
    if want.get("sha256") != now.get("sha256"):
        parts.append("내용 다름")
    if want.get("mode") != now.get("mode"):
        parts.append(f"권한 {oct(want.get('mode') or 0)} -> {oct(now.get('mode') or 0)}")
    if (want.get("uid"), want.get("gid")) != (now.get("uid"), now.get("gid")):
        parts.append(f"소유자 {describe_owner(want['uid'], want['gid'])} -> {describe_owner(now['uid'], now['gid'])}")
    if acl_entries(want.get("acl")) != acl_entries(now.get("acl")):
        parts.append("ACL 다름")
    return ", ".join(parts) or "차이 없음"
