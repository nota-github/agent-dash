#!/bin/sh
# agent-dash 설치 스크립트
#
#   한 줄 설치:  curl -fsSL https://raw.githubusercontent.com/nota-github/agent-dash/main/install.sh | sh
#   저장소 안에서: ./install.sh   (내려받지 않고 이 폴더의 코드를 설치한다)
#
# 설치하는 것 (모두 설치 기록에 남고 `agent-dash uninstall`로 되돌린다)
#   ~/.local/share/agent-dash/   코드
#   ~/.local/bin/agent-dash      실행 파일
#   ~/.agentdash/                설정(config.json), 등록 목록, 로그
# 셸 설정 파일(.zshrc 등)은 바꾸지 않는다. PATH 추가 방법만 안내한다.
#
# 환경 변수로 질문을 건너뛸 수 있다
#   AGENT_DASH_MODE=web|tui|both   AGENT_DASH_PORT=24xxx   AGENT_DASH_READ_ROOT=1
#   AGENT_DASH_REPO=owner/repo     AGENT_DASH_REF=main     AGENT_DASH_SRC=<코드 폴더>
#
# 이전 방식(설치 계획을 직접 적용): ./install.sh --plan base [--root] [--dry-run]
set -eu

REPO="${AGENT_DASH_REPO:-nota-github/agent-dash}"
REF="${AGENT_DASH_REF:-main}"
PORT_MIN=24000
PORT_MAX=25000
TMP=""

say() { printf '%s\n' "$*"; }
step() { printf '  - %s\n' "$*"; }
die() { printf '\n설치 중단: %s\n' "$*" >&2; exit 1; }
cleanup() { if [ -n "$TMP" ]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT INT TERM

# 파이프로 실행해도 키보드 입력은 터미널에서 받는다. 터미널이 없으면 기본값을 쓴다.
ask() {
    REPLY=""
    if [ -t 0 ]; then
        printf '%s' "$1"
        IFS= read -r REPLY || REPLY=""
    elif (exec </dev/tty) 2>/dev/null; then
        printf '%s' "$1" >/dev/tty
        IFS= read -r REPLY </dev/tty || REPLY=""
    fi
    [ -n "$REPLY" ] || REPLY="$2"
}

# ---------- 이전 방식 ----------
if [ "${1:-}" = "--plan" ] || [ "${1:-}" = "--root" ]; then
    DIR=$(cd "$(dirname "$0")" && pwd)
    CLI="$DIR/agentdash/cli.py"
    BACKUP="$HOME/.agentdash-backup"
    if [ "$1" = "--root" ]; then
        shift
        [ "${1:-}" = "--plan" ] && shift
        exec sudo -n python3 -I "$CLI" --home /root --backup-root "$BACKUP" install "$@"
    fi
    shift
    exec python3 -I "$CLI" install "$@"
fi

say ""
say "agent-dash 설치"
say "Claude Code, Codex 세션의 진행 상황을 웹과 터미널 화면으로 보여주는 도구입니다."
say ""

# ---------- 1. Python ----------
PY=$(command -v python3 || true)
[ -n "$PY" ] || die "python3가 없습니다. Python 3.9 이상을 설치한 뒤 다시 실행하세요."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 이상이 필요합니다. 지금 버전: $("$PY" -V 2>&1)"
"$PY" -c 'import curses' 2>/dev/null || CURSES_MISSING=1

# ---------- 2. 코드 ----------
if [ -n "${AGENT_DASH_SRC:-}" ]; then
    SRC=$(cd "$AGENT_DASH_SRC" && pwd)
elif [ -f "$0" ] && [ -f "$(dirname "$0")/agentdash/cli.py" ]; then
    SRC=$(cd "$(dirname "$0")" && pwd)
else
    case "$REPO" in
        __*__) die "내려받을 저장소가 정해지지 않았습니다. AGENT_DASH_REPO=owner/repo 를 지정하세요." ;;
    esac
    command -v curl >/dev/null 2>&1 || die "curl이 없습니다."
    command -v tar >/dev/null 2>&1 || die "tar가 없습니다."
    TMP=$(mktemp -d)
    say "코드를 내려받는 중: github.com/$REPO ($REF)"
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xzf - -C "$TMP" --strip-components=1 \
        || die "내려받기에 실패했습니다. 저장소 주소와 네트워크를 확인하세요."
    SRC="$TMP"
fi
[ -f "$SRC/agentdash/cli.py" ] || die "코드 폴더가 올바르지 않습니다: $SRC"
CLI="$SRC/agentdash/cli.py"

# ---------- 업데이트인지 확인 ----------
# 이미 설치돼 있으면 지금 설정(화면 종류, 포트)을 기본값으로 쓰고, 웹 서버를 잠시 끈다.
UPGRADE=0
WAS_RUNNING=0
OLD_MODE=""
OLD_PORT=""
OLD_READ_ROOT=""
conf_get() {
    "$PY" -I -c 'import json, sys; v = json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v in (None, False) else v)' \
        "$HOME/.agentdash/config.json" "$1" 2>/dev/null || true
}
if "$PY" -I "$CLI" status --quiet; then
    UPGRADE=1
    if [ -f "$HOME/.agentdash/config.json" ]; then
        OLD_MODE=$(conf_get mode)
        OLD_PORT=$(conf_get port)
        OLD_READ_ROOT=$(conf_get read_root)
    fi
    say "이미 설치돼 있습니다. 새 버전으로 바꿉니다. 등록한 세션 목록과 설정은 유지합니다."
    PIDF="$HOME/.agentdash/server.pid"
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
        WAS_RUNNING=1
        step "웹 서버를 잠시 끄는 중"
        # stop은 프로세스가 끝날 때까지 기다린다. 포트를 놓아야 같은 포트를 다시 쓸 수 있다.
        "$PY" -I "$CLI" stop >/dev/null 2>&1 || true
    fi
fi

# ---------- 3. 설치할 화면 ----------
MODE="${AGENT_DASH_MODE:-}"
case "$OLD_MODE" in web) DEF=1 ;; tui) DEF=2 ;; *) DEF=3 ;; esac
while :; do
    if [ -z "$MODE" ]; then
        say ""
        say "설치할 화면을 고르세요."
        say "  1) 웹 화면      브라우저로 봅니다. 원격 서버라면 SSH 터널로 접속합니다."
        say "  2) 터미널 화면  SSH 터미널 안에서 바로 봅니다(TUI)."
        say "  3) 둘 다"
        ask "번호 [$DEF]: " "$DEF"
        case "$REPLY" in
            1|web) MODE=web ;;
            2|tui) MODE=tui ;;
            3|both) MODE=both ;;
            *) say "1, 2, 3 중에서 고르세요."; continue ;;
        esac
    fi
    case "$MODE" in web|tui|both) break ;; *) say "AGENT_DASH_MODE는 web, tui, both 중 하나여야 합니다."; MODE="" ;; esac
done
if [ "$MODE" != "web" ] && [ "${CURSES_MISSING:-0}" = 1 ]; then
    say "참고: 이 Python에는 curses가 없어 터미널 화면이 실행되지 않을 수 있습니다."
fi

# ---------- 4. 포트 ----------
# 비어 있는지 확인한다. 같은 서버를 여러 사람이 쓰면 사용자마다 다른 포트에서 찾기 시작한다.
port_free() {
    "$PY" -I - "$1" <<'PYEOF'
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
# 웹 서버와 같은 기준으로 확인한다. 방금 끈 서버의 연결이 남은 포트(TIME_WAIT)도 다시 쓸 수 있다.
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
PYEOF
}
valid_port() {
    case "$1" in ''|*[!0-9]*) return 1 ;; esac
    [ "$1" -ge 1024 ] && [ "$1" -le 65535 ]
}
PORT=""
if [ "$MODE" != "tui" ]; then
    # 업데이트라면 이전 웹 서버가 포트를 놓을 때까지 잠시 기다린다
    if [ -n "$OLD_PORT" ] && valid_port "$OLD_PORT"; then
        n=0
        while [ "$n" -lt 20 ] && ! port_free "$OLD_PORT"; do sleep 0.5; n=$((n + 1)); done
    fi
    if [ -n "${AGENT_DASH_PORT:-}" ]; then
        valid_port "$AGENT_DASH_PORT" || die "AGENT_DASH_PORT가 올바르지 않습니다: $AGENT_DASH_PORT"
        port_free "$AGENT_DASH_PORT" || die "포트 $AGENT_DASH_PORT 는 이미 사용 중입니다."
        PORT="$AGENT_DASH_PORT"
    elif [ -n "$OLD_PORT" ] && valid_port "$OLD_PORT" && port_free "$OLD_PORT"; then
        PORT="$OLD_PORT"
    else
        say ""
        step "비어 있는 포트를 찾는 중 ($PORT_MIN~$PORT_MAX)"
        span=$((PORT_MAX - PORT_MIN + 1))
        start=$(( $(id -u) * 37 % span ))
        i=0
        while [ "$i" -lt "$span" ]; do
            p=$(( PORT_MIN + (start + i) % span ))
            if port_free "$p"; then PORT="$p"; break; fi
            i=$((i + 1))
        done
        if [ -z "$PORT" ]; then
            say ""
            say "$PORT_MIN~$PORT_MAX 대역에 비어 있는 포트가 없습니다."
            say "웹 서버가 쓸 포트 번호를 직접 입력하세요. 다른 프로그램이 쓰지 않는 1024~65535 사이 번호여야 합니다."
            while :; do
                ask "포트 번호: " ""
                [ -n "$REPLY" ] || die "터미널 입력을 받을 수 없습니다. AGENT_DASH_PORT=<포트> 를 지정해 다시 실행하세요."
                if ! valid_port "$REPLY"; then say "1024~65535 사이 숫자를 입력하세요."; continue; fi
                if ! port_free "$REPLY"; then say "포트 $REPLY 는 이미 사용 중입니다. 다른 번호를 입력하세요."; continue; fi
                PORT="$REPLY"; break
            done
        fi
    fi
fi

# ---------- 5. 설치 ----------
APP="$HOME/.local/share/agent-dash"
BIN="$HOME/.local/bin/agent-dash"
BIN_PY_CLI="$APP/agentdash/cli.py"
READ_ROOT=""
case "${AGENT_DASH_READ_ROOT:-$OLD_READ_ROOT}" in 1|true|True|yes) READ_ROOT="--read-root" ;; esac
say ""
say "설치를 진행합니다."
KEEP=""
if [ "$UPGRADE" = 1 ]; then
    step "기존 설치를 정리하는 중 (등록 목록은 보관)"
    KEEP=$(mktemp -d)
    for f in tracked.json tui.json; do
        [ -f "$HOME/.agentdash/$f" ] && cp -p "$HOME/.agentdash/$f" "$KEEP/$f"
    done
    "$PY" -I "$CLI" uninstall --yes >/dev/null || die "기존 설치를 되돌리지 못했습니다. agent-dash uninstall 결과를 확인하세요."
fi

step "코드와 설정을 설치하는 중"
if [ -n "$PORT" ]; then
    "$PY" -I "$CLI" install app --src "$SRC" --mode "$MODE" --port "$PORT" $READ_ROOT >/dev/null || die "설치에 실패했습니다."
else
    "$PY" -I "$CLI" install app --src "$SRC" --mode "$MODE" $READ_ROOT >/dev/null || die "설치에 실패했습니다."
fi
if [ -n "$KEEP" ]; then
    for f in tracked.json tui.json; do
        [ -f "$KEEP/$f" ] && cp -p "$KEEP/$f" "$HOME/.agentdash/$f"
    done
    rm -rf "$KEEP"
fi
RESTARTED=0
if [ "$WAS_RUNNING" = 1 ] && [ "$MODE" != "tui" ]; then
    step "웹 서버를 다시 켜는 중"
    "$PY" -I "$BIN_PY_CLI" start >/dev/null 2>&1 && RESTARTED=1 || true
fi
step "완료"
say ""

# ---------- 6. 안내 ----------
case "$MODE" in web) MODE_TEXT="웹 화면" ;; tui) MODE_TEXT="터미널 화면" ;; *) MODE_TEXT="웹 화면, 터미널 화면" ;; esac
CMD="agent-dash"
ON_PATH=1
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) ON_PATH=0; CMD="~/.local/bin/agent-dash" ;; esac
# 터널 명령: SSH로 접속한 터미널이면 실제로 접속한 주소와 SSH 포트를 쓴다
TUNNEL=$("$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from agentdash.model import tunnel_command; print(tunnel_command(int(sys.argv[2]), sys.argv[3]))' "$APP" "${PORT:-0}" "$(id -un)" 2>/dev/null || echo "ssh -f -N -L ${PORT}:127.0.0.1:${PORT} $(id -un)@<서버 주소>")
case "${SSH_CONNECTION:-}" in "") SSH_KNOWN=0 ;; *) SSH_KNOWN=1 ;; esac

if [ "$UPGRADE" = 1 ]; then say "업데이트를 마쳤습니다."; else say "설치를 마쳤습니다."; fi
say "  화면: $MODE_TEXT${PORT:+, 웹 포트 $PORT}"
say "  코드: $APP"
say "  실행 파일: $BIN"
say "  설정: $HOME/.agentdash/config.json"

if [ "$ON_PATH" = 0 ]; then
    case "$(basename "${SHELL:-sh}")" in
        zsh) RC="$HOME/.zshrc"; LINE='export PATH="$HOME/.local/bin:$PATH"' ;;
        bash) RC="$HOME/.bashrc"; LINE='export PATH="$HOME/.local/bin:$PATH"' ;;
        fish) RC="$HOME/.config/fish/config.fish"; LINE='fish_add_path $HOME/.local/bin' ;;
        *) RC="$HOME/.profile"; LINE='export PATH="$HOME/.local/bin:$PATH"' ;;
    esac
    say ""
    say "[agent-dash 명령 쓰기]"
    say "  ~/.local/bin 이 PATH에 없습니다. 아래 명령으로 셸 설정에 추가한 뒤 새 터미널을 여세요."
    say "    echo '$LINE' >> $RC"
    say "  지금 터미널에서 바로 쓰려면: $LINE"
    say "  추가하기 전에는 전체 경로로 실행합니다: ~/.local/bin/agent-dash"
fi

say ""
say "[처음 할 일: 볼 세션 등록]"
say "  $CMD sessions              최근 세션 목록"
say "  $CMD track <세션 ID 앞부분>  세션 등록"
say "  $CMD track --dir <폴더>     폴더 아래에서 실행하는 세션을 모두 등록"
say "  화면에서는 웹의 설정 > 세션 관리, 터미널 화면의 a 키로 등록합니다."

if [ "$MODE" != "tui" ]; then
    say ""
    say "[웹 화면]"
    if [ "$RESTARTED" = 1 ]; then
        say "  웹 서버를 다시 켰습니다."
    fi
    say "  켜기: $CMD start    끄기: $CMD stop    다시 켜기: $CMD restart    상태: $CMD status"
    say "  웹 서버는 백그라운드로 실행되므로 tmux 없이도 SSH 연결을 끊어도 계속 돕니다."
    say "  서버 자체를 재부팅하면 꺼지므로 그때는 $CMD start 를 다시 실행합니다."
    say "  주소: http://127.0.0.1:$PORT (이 서버 안에서만 열립니다)"
    say "  원격 서버라면 로컬 PC 터미널에서 SSH 터널을 열고 브라우저로 http://localhost:$PORT 에 접속합니다."
    say "    $TUNNEL"
    if [ "$SSH_KNOWN" = 0 ]; then
        say "    (SSH 포트가 22가 아니면 -p <포트>를 붙입니다)"
    fi
    say "    터널 끄기: pkill -f \"$PORT:127.0.0.1:$PORT\""
fi
if [ "$MODE" != "web" ]; then
    say ""
    say "[터미널 화면]"
    say "  실행: $CMD tui   (q 종료, a 세션 관리, v 세로형/가로형, s 정렬, g Claude/Codex)"
fi

say ""
say "[사용 방식]"
say "  1) 로컬 PC에서 Claude, Codex 앱을 직접 쓰는 경우: 아직 지원하지 않습니다(다음 버전에서 Mac 지원 예정)."
say "  2) 로컬 앱을 원격 서버에 SSH로 연결해 쓰는 경우: 원격 서버에 설치하고, 웹 화면은 위 터널로 접속합니다."
say "  3) 원격 서버에 SSH로 접속해 CLI로 실행하는 경우: 원격 서버에 설치하고, 웹 화면은 터널로, 터미널 화면은 그 SSH 터미널에서 엽니다."
say ""
say "[업데이트] $CMD update   (최신 버전으로 바꿉니다. 화면 종류, 포트, 등록 목록은 유지합니다)"
say "[삭제]     $CMD uninstall   (설치 전 상태로 되돌립니다)"
say ""
