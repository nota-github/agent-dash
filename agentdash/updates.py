"""새 버전 확인.

GitHub main 브랜치의 version.py를 한 시간에 한 번만 읽고 결과를 ~/.agentdash/update-check.json에 둔다.
보내는 정보는 없다(파일 하나를 내려받기만 한다). 끄려면 config.json에 "update_check": false를 두거나
AGENT_DASH_NO_UPDATE_CHECK=1을 준다.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from agentdash.model import STATE_DIR, load_config
from agentdash.version import __version__

LATEST_URL = "https://raw.githubusercontent.com/nota-github/agent-dash/main/agentdash/version.py"
CACHE = STATE_DIR / "update-check.json"
CHECK_SEC = 3600  # 한 시간에 한 번만 실제로 확인한다
TIMEOUT_SEC = 2.0


def enabled() -> bool:
    if os.environ.get("AGENT_DASH_NO_UPDATE_CHECK"):
        return False
    return load_config().get("update_check", True) is not False


def parse(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).lstrip("v").split("."))
    except ValueError:
        return ()


def newer(latest: str | None, current: str = __version__) -> bool:
    return bool(latest) and parse(latest) > parse(current)


def _read_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def latest(force: bool = False) -> str | None:
    """최신 버전 문자열. 한 시간 안에 확인했으면 저장한 값을 쓴다. 실패하면 None."""
    if not enabled():
        return None
    cache = _read_cache()
    if not force and time.time() - cache.get("checked", 0) < CHECK_SEC:
        return cache.get("latest")
    found = None
    try:
        with urllib.request.urlopen(LATEST_URL, timeout=TIMEOUT_SEC) as r:
            m = re.search(r'__version__\s*=\s*"([0-9.]+)"', r.read(4096).decode(errors="replace"))
            found = m.group(1) if m else None
    except Exception:  # 네트워크가 없어도 다른 기능은 그대로 동작해야 한다
        found = None
    if STATE_DIR.exists():
        try:
            tmp = CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"checked": time.time(), "latest": found or cache.get("latest")}))
            os.replace(tmp, CACHE)
        except OSError:
            pass
    return found or cache.get("latest")


def info() -> dict:
    lv = latest()
    return {"current": __version__, "latest": lv, "update": newer(lv)}


def notice() -> str | None:
    lv = latest()
    if newer(lv):
        return f"새 버전이 있습니다: v{lv} (지금 v{__version__}). 업데이트: agent-dash update"
    return None
