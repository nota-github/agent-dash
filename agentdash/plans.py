"""설치 계획. 계획 하나는 작업 목록이고, 작업은 ledger.Ledger.apply가 처리한다.

작업 id는 내용이 바뀌면 함께 바꾼다. 같은 id에 다른 내용을 적용하려 하면 ledger가 거부한다.
"""

from __future__ import annotations

import json
from pathlib import Path

APP_DIR = ".local/share/agent-dash"
LAUNCHER = ".local/bin/agent-dash"
CORE = ["claude.py", "cli.py", "codex.py", "ledger.py", "model.py", "plan.py",
        "plans.py", "procs.py", "sources.py", "updates.py", "version.py"]
WEB = ["server.py", "web/index.html"]
TUI = ["tui.py"]
DOCS = ["README.md", "README_ko.md", "MANUAL.md", "CHANGELOG.md", "uninstall.sh"]
MODES = ("web", "tui", "both")


def base(home: Path, **_) -> list[dict]:
    """런타임 폴더. 등록 목록, 설정, 서버 로그를 둔다."""
    return [{"id": "base/dir", "op": "mkdir", "path": str(home / ".agentdash"), "mode": 0o755}]


def config(home: Path, mode: str = "both", port: int | None = None, read_root: bool = False,
           lang: str | None = None, **_) -> list[dict]:
    """설정 파일. 저장소에서 바로 실행하는 개발 환경도 이 계획으로 포트를 정한다."""
    if mode not in MODES:
        raise SystemExit(f"--mode는 {', '.join(MODES)} 중 하나여야 함")
    cfg = {"mode": mode, "port": port, "read_root": bool(read_root), "lang": lang if lang in ("en", "ko") else "en"}
    return base(home) + [{"id": "app/config", "op": "write_file", "path": str(home / ".agentdash" / "config.json"),
                          "content": json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "mode": 0o644}]


def app(home: Path, src: str | None = None, mode: str = "both", port: int | None = None,
        read_root: bool = False, lang: str | None = None, python: str = "python3", **_) -> list[dict]:
    """코드, 설정, 실행 파일(~/.local/bin/agent-dash). 원본 폴더(src)의 파일을 그대로 복사한다."""
    if not src:
        raise SystemExit("app 계획에는 --src가 필요함")
    if mode not in MODES:
        raise SystemExit(f"--mode는 {', '.join(MODES)} 중 하나여야 함")
    src_dir = Path(src)
    dest = home / APP_DIR
    names = CORE + (WEB if mode in ("web", "both") else []) + (TUI if mode in ("tui", "both") else [])
    acts = base(home)
    acts.append({"id": "app/dir", "op": "mkdir", "path": str(dest / "agentdash" / "web" if mode != "tui" else dest / "agentdash"), "mode": 0o755})
    for n in names:
        f = src_dir / "agentdash" / n
        acts.append({"id": f"app/code/{n}", "op": "write_file", "path": str(dest / "agentdash" / n),
                     "content": f.read_text(encoding="utf-8"), "mode": 0o644})
    for n in DOCS:
        f = src_dir / n
        if f.exists():
            acts.append({"id": f"app/doc/{n}", "op": "write_file", "path": str(dest / n),
                         "content": f.read_text(encoding="utf-8"), "mode": 0o755 if n.endswith(".sh") else 0o644})
    acts += config(home, mode=mode, port=port, read_root=read_root, lang=lang)[1:]
    acts.append({"id": "app/bin-dir", "op": "mkdir", "path": str(home / ".local" / "bin"), "mode": 0o755})
    launcher = ("#!/bin/sh\n"
                "# agent-dash 실행 파일. 설치 스크립트가 만들었다. 지우려면: agent-dash uninstall\n"
                f'exec "{python}" -I -B "{dest / "agentdash" / "cli.py"}" "$@"\n')
    acts.append({"id": "app/launcher", "op": "write_file", "path": str(home / LAUNCHER), "content": launcher, "mode": 0o755})
    return acts


PLANS = {
    "base": base,
    "app": app,
    "config": config,
}


def build(names: list[str], home: Path, **opts) -> list[dict]:
    actions = []
    for n in names:
        if n not in PLANS:
            raise SystemExit(f"알 수 없는 계획: {n}. 사용 가능: {', '.join(PLANS)}")
        actions.extend(PLANS[n](home, **opts))
    seen, out = set(), []
    for a in actions:  # base와 app을 함께 줘도 같은 작업은 한 번만 한다
        if a["id"] not in seen:
            seen.add(a["id"])
            out.append(a)
    return out
