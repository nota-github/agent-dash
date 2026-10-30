"""agent-dash 명령. 설치와 원복, 세션 등록, 웹 서버와 터미널 화면 실행."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdash import ledger as L  # noqa: E402
from agentdash import plans  # noqa: E402


def default_backup_root(home: Path) -> Path:
    return home / ".agentdash-backup"


def make_ledger(args) -> L.Ledger:
    home = Path(args.home).expanduser() if args.home else Path.home()
    root = Path(args.backup_root).expanduser() if args.backup_root else default_backup_root(home)
    return L.Ledger(home, root)


def confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(f"{prompt} 확인이 필요함. 터미널에서 실행하거나 --yes를 줘야 함", file=sys.stderr)
        return False
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def cmd_install(args) -> int:
    lg = make_ledger(args)
    actions = plans.build(args.plans, lg.home, src=args.src, mode=args.mode, port=args.port,
                          read_root=args.read_root, python=sys.executable)
    print(f"대상 홈: {lg.home}")
    print(f"백업 위치: {lg.target_dir}")
    if args.dry_run:
        print("미리보기. 아무것도 바꾸지 않음\n")
    for a in actions:
        try:
            print(lg.apply(a, dry_run=args.dry_run))
        except L.LedgerError as e:
            print(f"중단: {a['id']}: {e}", file=sys.stderr)
            if lg.dir is not None:
                lg.record_post()
            return 1
    if not args.dry_run and lg.dir is not None:
        lg.record_post()
        pre = L.inventory_diff(lg._load("inventory-pre.json"), L.inventory(lg.home))
        touched = {str(Path(k).relative_to(lg.home)) for k in lg._load("pre.json") if k != str(lg.home)}
        outside = [p for kind in pre.values() for p in kind if p.rstrip("/") not in touched
                   and not any(p.startswith(t + "/") for t in touched)]
        if outside:
            print("\n참고: 설치 기록 밖에서 바뀐 설정 파일 (앱이 바꾼 것일 수 있음)")
            for p in outside[:30]:
                print(f"  {p}")
    return 0


def cmd_status(args) -> int:
    lg = make_ledger(args)
    if args.quiet:
        return 0 if lg.dir is not None else 3
    if lg.dir is None:
        print(f"진행 중인 설치 없음 ({lg.home})")
        return 0
    print(f"설치 기록: {lg.dir}")
    for r in lg.ops():
        if r["status"] == "done":
            print(f"  {r['seq']:>3} {r['id']:<28} {r['action']['op']:<12} {r['action']['path']}")
    pre, post = lg._load("pre.json"), lg._load("post.json")
    changed = [k for k in pre if not L.same_state(L.file_state(Path(k)), post.get(k, {"exists": False}))]
    if changed:
        print("설치 후 다른 이유로 바뀐 경로:")
        for k in changed:
            print(f"  {k}")
    return 0


def write_report(lg: L.Ledger, rep: L.Report, exact: bool, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    f = report_dir / f"rollback-{L.target_key(lg.home)}-{L.now_stamp()}.md"
    inv = L.inventory_diff(lg._load("inventory-pre.json"), L.inventory(lg.home))
    lines = [
        f"# 원복 결과 ({lg.home})",
        "",
        f"- 시각: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 설치 기록: {lg.dir}",
        f"- 방식: {'원본으로 교체 (--exact)' if exact else '기본 (agent-dash가 추가한 부분만 제거)'}",
        f"- 판정: {'원복 완료' if rep.ok else ('일부 남음' if not rep.failed else '실패')}",
        "",
        "## 원복 완료",
        "",
    ]
    lines += [f"- `{p}`" for p in rep.restored] or ["- 없음"]
    lines += ["", "## agent-dash와 무관한 변경 (원본과 다르게 남은 부분)", ""]
    if rep.partial:
        for p, diff in rep.partial:
            lines += [f"### `{p}`", "", "```diff", diff.rstrip(), "```", ""]
    else:
        lines.append("- 없음")
    lines += ["", "## 실패", ""]
    lines += [f"- `{p}`: {why}" for p, why in rep.failed] or ["- 없음"]
    lines += ["", "## 참고", ""]
    lines += [f"- {n}" for n in rep.notes] or ["- 없음"]
    lines += ["", "## 설정 파일 전체 목록 비교 (설치 전과 지금)", "",
              "앱이 계속 바꾸는 파일이 포함될 수 있다. 설치 기록에 있는 경로는 위 판정을 기준으로 한다.", ""]
    for kind, label in (("added", "추가"), ("removed", "삭제"), ("changed", "변경")):
        items = inv[kind]
        lines.append(f"- {label} {len(items)}건" + (": " + ", ".join(f"`{x}`" for x in items[:20]) if items else ""))
    f.write_text("\n".join(lines) + "\n")
    return f


def cmd_uninstall(args) -> int:
    lg = make_ledger(args)
    if lg.dir is None:
        print(f"진행 중인 설치 없음 ({lg.home})")
        return 0
    if lg.home == Path.home():
        cmd_stop(args)
    pre, post = lg._load("pre.json"), lg._load("post.json")
    changed = [k for k in pre if k in post and not L.same_state(L.file_state(Path(k)), post[k])]
    if args.exact and changed:
        print("설치 후 다른 이유로 바뀐 경로가 있음. --exact는 이 변경도 함께 되돌림:")
        for k in changed:
            print(f"  {k}")
            want = pre[k]
            if want.get("kind") == "file" and Path(k).exists():
                print(lg._diff_against_backup(Path(k), want, lg.dir))
        if not confirm("원본으로 교체할까?", args.yes):
            return 1
    rep = lg.uninstall(exact=args.exact)
    report_dir = Path(args.report_dir) if args.report_dir else lg.dir / "reports"
    f = write_report(lg, rep, args.exact, report_dir)
    print(f"원복 완료 {len(rep.restored)}건, 무관한 변경 {len(rep.partial)}건, 실패 {len(rep.failed)}건")
    for n in rep.notes:
        print(f"  참고: {n}")
    for p, why in rep.failed:
        print(f"  실패: {p}: {why}", file=sys.stderr)
    print(f"보고서: {f}")
    if rep.failed:
        print("실패한 경로가 있어 설치 기록을 닫지 않음. 원인을 해결한 뒤 다시 실행해야 함", file=sys.stderr)
        return 1
    d = lg.close()
    print(f"설치 기록을 닫음: {d}")
    if args.purge_backup:
        args.yes = True
        return cmd_purge(args)
    print(f"원복용 백업은 {lg.backup_root} 에 남겨 둠. 필요 없으면 지워도 됨: rm -rf {lg.backup_root}")
    return 0


def cmd_purge(args) -> int:
    lg = make_ledger(args)
    if lg.dir is not None:
        print("진행 중인 설치가 있어 백업을 지울 수 없음. 먼저 원복해야 함", file=sys.stderr)
        return 1
    if not lg.target_dir.exists():
        print("지울 백업 없음")
        return 0
    closed = [d for d in sorted(lg.target_dir.iterdir()) if (d / "CLOSED").exists()]
    if not closed:
        print("지울 백업 없음")
        return 0
    for d in closed:
        print(f"  {d}")
    if not confirm("위 백업을 지울까?", args.yes):
        return 1
    for d in closed:
        shutil.rmtree(d)
    for d in (lg.target_dir, lg.backup_root):
        try:
            d.rmdir()
        except OSError:
            break
    print("백업을 지움")
    return 0


def _registry():
    from agentdash.model import Registry
    return Registry()


def _resolve_key(prefix: str) -> str | None:
    """세션 ID 앞부분이나 key로 세션을 찾는다."""
    items = _registry().discover(days=30)
    hits = [d for d in items if d["key"] == prefix or d["id"].startswith(prefix) or d["key"].split(":", 1)[1].startswith(prefix)]
    if len(hits) == 1:
        return hits[0]["key"]
    if not hits:
        print(f"최근 30일 안에 해당하는 세션 없음: {prefix}", file=sys.stderr)
    else:
        print(f"여러 세션이 해당함. 더 길게 입력해야 함: {', '.join(d['key'] for d in hits[:5])}", file=sys.stderr)
    return None


def cmd_track(args) -> int:
    from agentdash.model import Tracked
    tr = Tracked()
    try:
        if args.dir:
            tr.add_dir(args.target)
            print(f"폴더 등록: {os.path.abspath(args.target)}")
            return 0
        key = _resolve_key(args.target)
        if not key:
            return 1
        tr.add(key)
        print(f"등록: {key}")
    except RuntimeError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_untrack(args) -> int:
    from agentdash.model import Tracked
    tr = Tracked()
    try:
        if args.dir:
            tr.remove_dir(args.target)
            print(f"폴더 해제: {os.path.abspath(args.target)}")
            return 0
        key = next((k for k in tr.data["sessions"] if k == args.target or k.split(":", 1)[1].startswith(args.target)), None)
        if not key:
            print(f"등록된 세션 중에 없음: {args.target}", file=sys.stderr)
            return 1
        tr.remove(key)
        print(f"해제: {key}")
    except RuntimeError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_sessions(args) -> int:
    for d in _registry().discover(days=args.days):
        mark = "*" if d["tracked"] else " "
        when = time.strftime("%m-%d %H:%M", time.localtime(d["mtime"]))
        print(f"{mark} {when}  {d['key'][:26]:<26}  {d['workspace'][:26]:<26}  {(d.get('entry') or '')[:14]:<14}  {d.get('title') or ''}")
    print("\n* 등록됨. 등록: agent-dash track <ID 앞부분>")
    return 0


def _pidfile() -> Path:
    return Path.home() / ".agentdash" / "server.pid"


def _config() -> dict:
    from agentdash.model import load_config
    return load_config()


def default_port() -> int:
    from agentdash import server
    return int(_config().get("port") or server.default_port())


REPO = "nota-github/agent-dash"
INSTALL_URL = f"https://raw.githubusercontent.com/{REPO}/main/install.sh"


def _tunnel_hint(port: int) -> str:
    from agentdash.model import host_info
    user = os.environ.get("USER") or Path.home().name
    h = host_info()
    return (f"원격 서버라면 로컬 PC에서 터널을 연 뒤 브라우저로 http://localhost:{port} 에 접속:\n"
            f"  ssh -f -N -L {port}:127.0.0.1:{port} {user}@{h['ip'] or h['name']}\n"
            f"  (SSH 포트가 22가 아니면 -p <포트>를 붙인다. 끄기: pkill -f \"{port}:127.0.0.1:{port}\")")


def cmd_web(args) -> int:
    if not _web_installed():
        return 1
    port = args.port or default_port()
    if args.action == "stop":
        return cmd_stop(args)
    if args.action == "status":
        from agentdash import server
        print(f"실행 중: http://127.0.0.1:{port}" if server.port_open(port=port) else "꺼져 있음")
        return 0
    args.port, args.ensure = port, True
    rc = cmd_serve(args)
    if rc == 0:
        print(_tunnel_hint(port))
    return rc


def _web_installed() -> bool:
    if _config().get("mode") == "tui":
        print("웹 화면은 설치하지 않았음. 'agent-dash update --mode both'로 다시 설치하면 됨", file=sys.stderr)
        return False
    return True


def cmd_start(args) -> int:
    if not _web_installed():
        return 1
    args.action = "start"
    return cmd_web(args)


def cmd_restart(args) -> int:
    if not _web_installed():
        return 1
    cmd_stop(args)
    from agentdash import server
    port = args.port or default_port()
    for _ in range(20):  # 이전 프로세스가 포트를 놓을 때까지 기다린다
        if not server.port_open(port=port):
            break
        time.sleep(0.25)
    return cmd_start(args)


def cmd_overview(args) -> int:
    """설치 정보, 웹 서버, 등록 세션을 한 번에 보여준다. 설치 기록은 --ledger."""
    if args.ledger or args.quiet:
        return cmd_status(args)
    lg = make_ledger(args)
    cfg = _config()
    mode = {"web": "웹 화면", "tui": "터미널 화면", "both": "웹 화면, 터미널 화면"}.get(cfg.get("mode"), "개발용 실행 (설정 파일 없음)")
    print(f"설치: {'됨' if lg.dir is not None else '설치 기록 없음'}, {mode}")
    app = Path.home() / plans.APP_DIR
    if app.exists():
        print(f"코드: {app}")
    if cfg.get("mode") != "tui":
        from agentdash import server
        port = default_port()
        running = server.port_open(port=port)
        print(f"웹 서버: {'실행 중' if running else '꺼져 있음'} (http://127.0.0.1:{port})")
        if not running:
            print("  켜기: agent-dash start")
    try:
        from agentdash.model import Tracked
        tr = Tracked()
        print(f"등록: 세션 {len(tr.data['sessions'])}개, 폴더 {len(tr.data['dirs'])}개")
    except Exception:
        pass
    return 0


def cmd_update(args) -> int:
    """최신 설치 스크립트를 내려받아 다시 설치한다. 화면 종류와 포트는 지금 설정을 그대로 쓴다."""
    import shutil as sh
    import subprocess
    from agentdash import updates
    from agentdash.version import __version__
    if not sh.which("curl"):
        print("curl이 없음", file=sys.stderr)
        return 1
    lv = updates.latest(force=True)
    if not args.force and not args.mode:
        if lv is None:
            print("최신 버전을 확인하지 못함 (네트워크 확인). 그래도 다시 설치하려면 --force")
            return 1
        if not updates.newer(lv):
            print(f"이미 최신 버전임: v{__version__}")
            return 0
        print(f"v{__version__} -> v{lv}")
    cfg = _config()
    env = dict(os.environ)
    env["AGENT_DASH_MODE"] = args.mode or cfg.get("mode") or "both"
    if cfg.get("port") and not os.environ.get("AGENT_DASH_PORT"):
        env["AGENT_DASH_PORT"] = str(cfg["port"])
    print(f"최신 버전으로 다시 설치함: {INSTALL_URL}")
    p = subprocess.run(f'curl -fsSL "{INSTALL_URL}" | sh', shell=True, env=env)
    return p.returncode


def cmd_tui(args) -> int:
    if _config().get("mode") == "web":
        print("터미널 화면은 설치하지 않았음. 'agent-dash update --mode both'로 다시 설치하면 됨", file=sys.stderr)
        return 1
    from agentdash import tui
    return tui.main()


def cmd_serve(args) -> int:
    from agentdash import server
    args.port = args.port or default_port()
    if server.port_open(port=args.port):
        print(f"이미 실행 중: http://127.0.0.1:{args.port}")
        return 0
    if args.ensure:
        state_dir = Path.home() / ".agentdash"
        if not state_dir.exists():
            print(f"{state_dir}가 없음. 먼저 설치해야 함", file=sys.stderr)
            return 1
        log = open(state_dir / "server.log", "ab")
        import subprocess
        p = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "serve", "--port", str(args.port)],
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        _pidfile().write_text(f"{p.pid}\n")
        for _ in range(20):
            time.sleep(0.25)
            if server.port_open(port=args.port):
                print(f"실행함: http://127.0.0.1:{args.port} (pid {p.pid}, 로그 {state_dir / 'server.log'})")
                return 0
        print(f"서버가 열리지 않음. 로그 확인: {state_dir / 'server.log'}", file=sys.stderr)
        return 1
    server.serve(port=args.port)
    return 0


def cmd_stop(args) -> int:
    import signal
    pf = _pidfile()
    if not pf.exists():
        print("실행 중인 서버 기록 없음")
        return 0
    pid = int(pf.read_text().strip() or 0)
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"서버 종료: pid {pid}")
    except ProcessLookupError:
        print("이미 종료됨")
    pf.unlink(missing_ok=True)
    return 0


def main(argv=None) -> int:
    from agentdash.version import __version__
    p = argparse.ArgumentParser(prog="agent-dash", description="Claude Code, Codex 세션 진행 현황")
    p.add_argument("--version", action="version", version=f"agent-dash {__version__}")
    p.add_argument("--home", help="대상 홈 디렉터리 (기본: 현재 사용자 홈)")
    p.add_argument("--backup-root", help="백업 위치 (기본: <홈>/.agentdash-backup)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("install", help="계획을 적용한다")
    s.add_argument("plans", nargs="+", help=f"적용할 계획: {', '.join(plans.PLANS)}")
    s.add_argument("--dry-run", action="store_true", help="바꿀 내용만 보여준다")
    s.add_argument("--src", help="app 계획: 복사할 코드 폴더")
    s.add_argument("--mode", default="both", help="app 계획: web, tui, both")
    s.add_argument("--port", type=int, help="app, config 계획: 웹 서버 포트")
    s.add_argument("--read-root", action="store_true", help="app, config 계획: root 계정 기록도 읽기 (sudo -n 필요)")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("uninstall", help="설치 전 상태로 되돌린다")
    s.add_argument("--exact", action="store_true", help="설치 후 다른 이유로 바뀐 부분도 원본으로 되돌린다")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--report-dir")
    s.add_argument("--purge-backup", action="store_true", help="원복이 끝나면 백업도 지운다")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("status", help="설치 정보, 웹 서버 상태, 등록 세션 수")
    s.add_argument("--ledger", action="store_true", help="설치 기록(바꾼 파일 목록)을 보여준다")
    s.add_argument("--quiet", action="store_true", help="출력 없이 설치돼 있으면 0, 아니면 3을 돌려준다")
    s.set_defaults(fn=cmd_overview)

    s = sub.add_parser("start", help="웹 서버를 백그라운드로 켠다 (SSH를 끊어도 계속 실행)")
    s.add_argument("--port", type=int, help="기본: 설치할 때 정한 포트")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("restart", help="웹 서버를 다시 켠다")
    s.add_argument("--port", type=int, help="기본: 설치할 때 정한 포트")
    s.set_defaults(fn=cmd_restart)

    s = sub.add_parser("update", help="최신 버전으로 업데이트한다 (화면 종류와 포트 유지)")
    s.add_argument("--mode", choices=["web", "tui", "both"], help="화면 종류를 바꿀 때만 준다")
    s.add_argument("--force", action="store_true", help="최신 버전이어도 다시 설치한다")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("purge-backup", help="원복이 끝난 백업을 지운다")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_purge)

    s = sub.add_parser("track", help="세션이나 폴더를 대시보드에 등록한다")
    s.add_argument("target", help="세션 ID 앞부분, key(claude:<id>), 또는 --dir와 함께 폴더")
    s.add_argument("--dir", action="store_true", help="폴더 아래에서 실행한 세션을 모두 등록")
    s.set_defaults(fn=cmd_track)

    s = sub.add_parser("untrack", help="등록을 해제한다")
    s.add_argument("target")
    s.add_argument("--dir", action="store_true")
    s.set_defaults(fn=cmd_untrack)

    s = sub.add_parser("sessions", help="최근 세션 목록")
    s.add_argument("--days", type=float, default=3)
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("web", help="웹 화면 서버를 백그라운드로 실행한다 (stop: 끄기, status: 상태)")
    s.add_argument("action", nargs="?", default="start", choices=["start", "stop", "status"])
    s.add_argument("--port", type=int, help="기본: 설치할 때 정한 포트")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("tui", help="터미널 화면을 연다")
    s.set_defaults(fn=cmd_tui)

    s = sub.add_parser("serve", help="웹 서버를 이 터미널에서 실행한다 (127.0.0.1)")
    s.add_argument("--port", type=int, help="기본: 설치할 때 정한 포트")
    s.add_argument("--ensure", action="store_true", help="꺼져 있으면 백그라운드로 실행하고 바로 돌아온다")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("stop", help="웹 서버를 끈다")
    s.set_defaults(fn=cmd_stop)

    args = p.parse_args(argv)
    if args.cmd is None:
        return cmd_home(args)
    try:
        rc = args.fn(args)
    except L.LedgerError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    if args.cmd in NOTICE_AFTER and not getattr(args, "quiet", False) and args.home is None:
        _print_notice()
    return rc


# 이 명령을 실행한 뒤에는 새 버전이 있는지 알려준다 (하루 한 번만 실제로 확인한다)
NOTICE_AFTER = {"status", "start", "stop", "restart", "sessions", "track", "untrack"}


def _print_notice() -> None:
    try:
        from agentdash import updates
        msg = updates.notice()
    except Exception:
        msg = None
    if msg:
        print(f"\n{msg}", file=sys.stderr)


def cmd_home(args) -> int:
    """인자 없이 실행했을 때: 버전, 상태, 자주 쓰는 명령, 새 버전 안내."""
    from agentdash.version import __version__
    print(f"agent-dash v{__version__}\n")
    args.ledger = args.quiet = False
    cmd_overview(args)
    print("""
명령:
  agent-dash start | stop | restart   웹 서버 켜기, 끄기, 다시 켜기
  agent-dash status                   상태
  agent-dash tui                      터미널 화면
  agent-dash sessions                 최근 세션 목록
  agent-dash track <ID 앞부분>         세션 등록 (--dir <폴더>: 폴더 단위)
  agent-dash update                   업데이트
  agent-dash uninstall                삭제
  agent-dash --help                   전체 명령""")
    _print_notice()
    return 0


if __name__ == "__main__":
    sys.exit(main())
