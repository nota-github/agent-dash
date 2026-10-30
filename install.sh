#!/bin/sh
# agent-dash installer
#
#   One line:     curl -fsSL https://raw.githubusercontent.com/nota-github/agent-dash/main/install.sh | sh
#   Korean:       curl -fsSL https://raw.githubusercontent.com/nota-github/agent-dash/main/install_ko.sh | sh
#   From a clone: ./install.sh   (installs the code in this folder without downloading)
#
# What it installs (all recorded, `agent-dash uninstall` restores the previous state)
#   ~/.local/share/agent-dash/   code
#   ~/.local/bin/agent-dash      command
#   ~/.agentdash/                settings (config.json), tracked sessions, logs
# Shell startup files (.zshrc and so on) are not changed. Only how to add PATH is shown.
#
# Environment variables (skip the questions)
#   AGENT_DASH_MODE=web|tui|both   AGENT_DASH_PORT=24xxx   AGENT_DASH_READ_ROOT=1
#   AGENT_DASH_LANG=en|ko          AGENT_DASH_REPO=owner/repo   AGENT_DASH_REF=main
#   AGENT_DASH_SRC=<code folder>
#
# Legacy (apply install plans directly): ./install.sh --plan base [--root] [--dry-run]
set -eu

REPO="${AGENT_DASH_REPO:-nota-github/agent-dash}"
REF="${AGENT_DASH_REF:-main}"
PORT_MIN=24000
PORT_MAX=25000
TMP=""
L="${AGENT_DASH_LANG:-}"

# m "English" "한국어": prints the message in the selected language
m() { if [ "$L" = ko ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
say() { printf '%s\n' "$*"; }
step() { printf '  - %s\n' "$*"; }
die() { printf '\n%s %s\n' "$(m "Install aborted:" "설치 중단:")" "$*" >&2; exit 1; }
cleanup() { if [ -n "$TMP" ]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT INT TERM

# Read answers from the terminal even when piped. Without a terminal, use the default.
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

# ---------- legacy ----------
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

# ---------- 1. Python ----------
PY=$(command -v python3 || true)
[ -n "$PY" ] || die "$(m "python3 not found. Install Python 3.9 or later and run again." "python3가 없습니다. Python 3.9 이상을 설치한 뒤 다시 실행하세요.")"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "$(m "Python 3.9 or later is required. Found: " "Python 3.9 이상이 필요합니다. 지금 버전: ")$("$PY" -V 2>&1)"
"$PY" -c 'import curses' 2>/dev/null || CURSES_MISSING=1

conf_get() {
    "$PY" -I -c 'import json, sys; v = json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v in (None, False) else v)' \
        "$HOME/.agentdash/config.json" "$1" 2>/dev/null || true
}
# Language: AGENT_DASH_LANG, then the language of the current install, then English.
# Installs made before v0.1.3 have no lang setting; their installer was Korean only, so keep Korean.
if [ -z "$L" ] && [ -f "$HOME/.agentdash/config.json" ]; then
    L=$(conf_get lang)
    [ -n "$L" ] || L=ko
fi
case "$L" in ko) ;; *) L=en ;; esac

say ""
say "$(m "agent-dash installer" "agent-dash 설치")"
say "$(m "Shows the progress of your Claude Code and Codex sessions in a web page and a terminal view." "Claude Code, Codex 세션의 진행 상황을 웹과 터미널 화면으로 보여주는 도구입니다.")"
say ""

# ---------- 2. code ----------
if [ -n "${AGENT_DASH_SRC:-}" ]; then
    SRC=$(cd "$AGENT_DASH_SRC" && pwd)
elif [ -f "$0" ] && [ -f "$(dirname "$0")/agentdash/cli.py" ]; then
    SRC=$(cd "$(dirname "$0")" && pwd)
else
    case "$REPO" in
        __*__) die "$(m "No repository to download from. Set AGENT_DASH_REPO=owner/repo." "내려받을 저장소가 정해지지 않았습니다. AGENT_DASH_REPO=owner/repo 를 지정하세요.")" ;;
    esac
    command -v curl >/dev/null 2>&1 || die "$(m "curl not found." "curl이 없습니다.")"
    command -v tar >/dev/null 2>&1 || die "$(m "tar not found." "tar가 없습니다.")"
    TMP=$(mktemp -d)
    say "$(m "Downloading" "코드를 내려받는 중"): github.com/$REPO ($REF)"
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xzf - -C "$TMP" --strip-components=1 \
        || die "$(m "Download failed. Check the repository and your network." "내려받기에 실패했습니다. 저장소 주소와 네트워크를 확인하세요.")"
    SRC="$TMP"
fi
[ -f "$SRC/agentdash/cli.py" ] || die "$(m "Not an agent-dash code folder:" "코드 폴더가 올바르지 않습니다:") $SRC"
CLI="$SRC/agentdash/cli.py"

# ---------- update check ----------
# If already installed, keep the current settings (view, port) as defaults and stop the web server for a moment.
UPGRADE=0
WAS_RUNNING=0
OLD_MODE=""
OLD_PORT=""
OLD_READ_ROOT=""
if "$PY" -I "$CLI" status --quiet; then
    UPGRADE=1
    if [ -f "$HOME/.agentdash/config.json" ]; then
        OLD_MODE=$(conf_get mode)
        OLD_PORT=$(conf_get port)
        OLD_READ_ROOT=$(conf_get read_root)
    fi
    say "$(m "Already installed. Updating to the new version. Tracked sessions and settings are kept." "이미 설치돼 있습니다. 새 버전으로 바꿉니다. 등록한 세션 목록과 설정은 유지합니다.")"
    PIDF="$HOME/.agentdash/server.pid"
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
        WAS_RUNNING=1
        step "$(m "Stopping the web server" "웹 서버를 잠시 끄는 중")"
        # stop waits until the process exits, so the same port can be used again
        "$PY" -I "$CLI" stop >/dev/null 2>&1 || true
    fi
fi

# ---------- 3. view ----------
MODE="${AGENT_DASH_MODE:-}"
case "$OLD_MODE" in web) DEF=1 ;; tui) DEF=2 ;; *) DEF=3 ;; esac
while :; do
    if [ -z "$MODE" ]; then
        say ""
        say "$(m "Choose what to install." "설치할 화면을 고르세요.")"
        say "$(m "  1) Web        View in a browser. On a remote server, connect through an SSH tunnel." "  1) 웹 화면      브라우저로 봅니다. 원격 서버라면 SSH 터널로 접속합니다.")"
        say "$(m "  2) Terminal   View right inside your SSH terminal (TUI)." "  2) 터미널 화면  SSH 터미널 안에서 바로 봅니다(TUI).")"
        say "$(m "  3) Both" "  3) 둘 다")"
        ask "$(m "Number" "번호") [$DEF]: " "$DEF"
        case "$REPLY" in
            1|web) MODE=web ;;
            2|tui) MODE=tui ;;
            3|both) MODE=both ;;
            *) say "$(m "Choose 1, 2 or 3." "1, 2, 3 중에서 고르세요.")"; continue ;;
        esac
    fi
    case "$MODE" in web|tui|both) break ;; *) say "$(m "AGENT_DASH_MODE must be web, tui or both." "AGENT_DASH_MODE는 web, tui, both 중 하나여야 합니다.")"; MODE="" ;; esac
done
if [ "$MODE" != "web" ] && [ "${CURSES_MISSING:-0}" = 1 ]; then
    say "$(m "Note: this Python has no curses module, so the terminal view may not run." "참고: 이 Python에는 curses가 없어 터미널 화면이 실행되지 않을 수 있습니다.")"
fi

# ---------- 4. port ----------
# Free-port check. Each user starts searching at a different port so users on one server rarely collide.
port_free() {
    "$PY" -I - "$1" <<'PYEOF'
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
# Same rule as the web server: a port left in TIME_WAIT by a server just stopped can be reused.
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
    # On update, wait until the old web server releases its port
    if [ -n "$OLD_PORT" ] && valid_port "$OLD_PORT"; then
        n=0
        while [ "$n" -lt 20 ] && ! port_free "$OLD_PORT"; do sleep 0.5; n=$((n + 1)); done
    fi
    if [ -n "${AGENT_DASH_PORT:-}" ]; then
        valid_port "$AGENT_DASH_PORT" || die "$(m "Invalid AGENT_DASH_PORT:" "AGENT_DASH_PORT가 올바르지 않습니다:") $AGENT_DASH_PORT"
        port_free "$AGENT_DASH_PORT" || die "$(m "Port $AGENT_DASH_PORT is already in use." "포트 $AGENT_DASH_PORT 는 이미 사용 중입니다.")"
        PORT="$AGENT_DASH_PORT"
    elif [ -n "$OLD_PORT" ] && valid_port "$OLD_PORT" && port_free "$OLD_PORT"; then
        PORT="$OLD_PORT"
    else
        say ""
        step "$(m "Looking for a free port" "비어 있는 포트를 찾는 중") ($PORT_MIN~$PORT_MAX)"
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
            say "$(m "No free port in $PORT_MIN~$PORT_MAX." "$PORT_MIN~$PORT_MAX 대역에 비어 있는 포트가 없습니다.")"
            say "$(m "Enter a port for the web server. It must be a number from 1024 to 65535 that no other program uses." "웹 서버가 쓸 포트 번호를 직접 입력하세요. 다른 프로그램이 쓰지 않는 1024~65535 사이 번호여야 합니다.")"
            while :; do
                ask "$(m "Port" "포트 번호"): " ""
                [ -n "$REPLY" ] || die "$(m "No terminal input. Run again with AGENT_DASH_PORT=<port>." "터미널 입력을 받을 수 없습니다. AGENT_DASH_PORT=<포트> 를 지정해 다시 실행하세요.")"
                if ! valid_port "$REPLY"; then say "$(m "Enter a number from 1024 to 65535." "1024~65535 사이 숫자를 입력하세요.")"; continue; fi
                if ! port_free "$REPLY"; then say "$(m "Port $REPLY is in use. Try another one." "포트 $REPLY 는 이미 사용 중입니다. 다른 번호를 입력하세요.")"; continue; fi
                PORT="$REPLY"; break
            done
        fi
    fi
fi

# ---------- 5. install ----------
APP="$HOME/.local/share/agent-dash"
BIN="$HOME/.local/bin/agent-dash"
BIN_PY_CLI="$APP/agentdash/cli.py"
READ_ROOT=""
case "${AGENT_DASH_READ_ROOT:-$OLD_READ_ROOT}" in 1|true|True|yes) READ_ROOT="--read-root" ;; esac
say ""
say "$(m "Installing." "설치를 진행합니다.")"
KEEP=""
if [ "$UPGRADE" = 1 ]; then
    step "$(m "Removing the previous install (tracked sessions are kept)" "기존 설치를 정리하는 중 (등록 목록은 보관)")"
    KEEP=$(mktemp -d)
    for f in tracked.json tui.json; do
        [ -f "$HOME/.agentdash/$f" ] && cp -p "$HOME/.agentdash/$f" "$KEEP/$f"
    done
    "$PY" -I "$CLI" uninstall --yes >/dev/null || die "$(m "Could not remove the previous install. Check the output of agent-dash uninstall." "기존 설치를 되돌리지 못했습니다. agent-dash uninstall 결과를 확인하세요.")"
fi

step "$(m "Installing code and settings" "코드와 설정을 설치하는 중")"
if [ -n "$PORT" ]; then
    "$PY" -I "$CLI" install app --src "$SRC" --mode "$MODE" --port "$PORT" --lang "$L" $READ_ROOT >/dev/null || die "$(m "Install failed." "설치에 실패했습니다.")"
else
    "$PY" -I "$CLI" install app --src "$SRC" --mode "$MODE" --lang "$L" $READ_ROOT >/dev/null || die "$(m "Install failed." "설치에 실패했습니다.")"
fi
if [ -n "$KEEP" ]; then
    for f in tracked.json tui.json; do
        [ -f "$KEEP/$f" ] && cp -p "$KEEP/$f" "$HOME/.agentdash/$f"
    done
    rm -rf "$KEEP"
fi
RESTARTED=0
if [ "$WAS_RUNNING" = 1 ] && [ "$MODE" != "tui" ]; then
    step "$(m "Starting the web server again" "웹 서버를 다시 켜는 중")"
    "$PY" -I "$BIN_PY_CLI" start >/dev/null 2>&1 && RESTARTED=1 || true
fi
step "$(m "Done" "완료")"
say ""

# ---------- 6. next steps ----------
case "$MODE" in
    web) MODE_TEXT="$(m "web" "웹 화면")" ;;
    tui) MODE_TEXT="$(m "terminal" "터미널 화면")" ;;
    *) MODE_TEXT="$(m "web, terminal" "웹 화면, 터미널 화면")" ;;
esac
CMD="agent-dash"
ON_PATH=1
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) ON_PATH=0; CMD="~/.local/bin/agent-dash" ;; esac
# Tunnel command: in an SSH session, use the address and SSH port actually connected to
TUNNEL=$("$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from agentdash.model import tunnel_command; print(tunnel_command(int(sys.argv[2]), sys.argv[3]))' "$APP" "${PORT:-0}" "$(id -un)" 2>/dev/null || echo "ssh -f -N -L ${PORT}:127.0.0.1:${PORT} $(id -un)@<server>")
case "${SSH_CONNECTION:-}" in "") SSH_KNOWN=0 ;; *) SSH_KNOWN=1 ;; esac

if [ "$UPGRADE" = 1 ]; then say "$(m "Update complete." "업데이트를 마쳤습니다.")"; else say "$(m "Install complete." "설치를 마쳤습니다.")"; fi
say "  $(m "View" "화면"): $MODE_TEXT${PORT:+, $(m "web port" "웹 포트") $PORT}"
say "  $(m "Code" "코드"): $APP"
say "  $(m "Command" "실행 파일"): $BIN"
say "  $(m "Settings" "설정"): $HOME/.agentdash/config.json"

if [ "$ON_PATH" = 0 ]; then
    case "$(basename "${SHELL:-sh}")" in
        zsh) RC="$HOME/.zshrc"; LINE='export PATH="$HOME/.local/bin:$PATH"' ;;
        bash) RC="$HOME/.bashrc"; LINE='export PATH="$HOME/.local/bin:$PATH"' ;;
        fish) RC="$HOME/.config/fish/config.fish"; LINE='fish_add_path $HOME/.local/bin' ;;
        *) RC="$HOME/.profile"; LINE='export PATH="$HOME/.local/bin:$PATH"' ;;
    esac
    say ""
    say "$(m "[Using the agent-dash command]" "[agent-dash 명령 쓰기]")"
    say "  $(m "~/.local/bin is not on your PATH. Add it with the command below, then open a new terminal." "~/.local/bin 이 PATH에 없습니다. 아래 명령으로 셸 설정에 추가한 뒤 새 터미널을 여세요.")"
    say "    echo '$LINE' >> $RC"
    say "  $(m "To use it in this terminal now:" "지금 터미널에서 바로 쓰려면:") $LINE"
    say "  $(m "Until then, use the full path:" "추가하기 전에는 전체 경로로 실행합니다:") ~/.local/bin/agent-dash"
fi

say ""
say "$(m "[First step: track the sessions to show]" "[처음 할 일: 볼 세션 등록]")"
say "  $CMD sessions              $(m "recent sessions" "최근 세션 목록")"
say "  $CMD track <$(m "session ID prefix" "세션 ID 앞부분")>  $(m "track a session" "세션 등록")"
say "  $CMD track --dir <$(m "folder" "폴더")>     $(m "track every session started under a folder" "폴더 아래에서 실행하는 세션을 모두 등록")"
say "  $(m "In the views: 설정 > 세션 관리 at the top right of the web view, or the a key in the terminal view." "화면에서는 웹의 설정 > 세션 관리, 터미널 화면의 a 키로 등록합니다.")"

if [ "$MODE" != "tui" ]; then
    say ""
    say "$(m "[Web]" "[웹 화면]")"
    if [ "$RESTARTED" = 1 ]; then
        say "  $(m "The web server is running again." "웹 서버를 다시 켰습니다.")"
    fi
    say "  $(m "start" "켜기"): $CMD start    $(m "stop" "끄기"): $CMD stop    $(m "restart" "다시 켜기"): $CMD restart    $(m "status" "상태"): $CMD status"
    say "  $(m "The web server runs in the background, so it keeps running after you close SSH. No tmux needed." "웹 서버는 백그라운드로 실행되므로 tmux 없이도 SSH 연결을 끊어도 계속 돕니다.")"
    say "  $(m "After the server reboots, run $CMD start again." "서버 자체를 재부팅하면 꺼지므로 그때는 $CMD start 를 다시 실행합니다.")"
    say "  $(m "Address" "주소"): http://127.0.0.1:$PORT ($(m "only reachable on this server" "이 서버 안에서만 열립니다"))"
    say "  $(m "On a remote server, open an SSH tunnel from your PC, then browse to http://localhost:$PORT" "원격 서버라면 로컬 PC 터미널에서 SSH 터널을 열고 브라우저로 http://localhost:$PORT 에 접속합니다.")"
    say "    $TUNNEL"
    if [ "$SSH_KNOWN" = 0 ]; then
        say "    $(m "(add -p <port> if SSH does not use port 22)" "(SSH 포트가 22가 아니면 -p <포트>를 붙입니다)")"
    fi
    say "    $(m "Close the tunnel" "터널 끄기"): pkill -f \"$PORT:127.0.0.1:$PORT\""
fi
if [ "$MODE" != "web" ]; then
    say ""
    say "$(m "[Terminal view]" "[터미널 화면]")"
    say "  $(m "Run" "실행"): $CMD tui   ($(m "q quit, a sessions, v grid/list, s sort, g Claude/Codex, ? help" "q 종료, a 세션 관리, v 세로형/가로형, s 정렬, g Claude/Codex, ? 도움말"))"
fi

say ""
say "$(m "[Supported setups]" "[사용 방식]")"
say "  $(m "1) Claude or Codex apps on your local PC: not supported yet (Mac support planned)." "1) 로컬 PC에서 Claude, Codex 앱을 직접 쓰는 경우: 아직 지원하지 않습니다(다음 버전에서 Mac 지원 예정).")"
say "  $(m "2) Local apps connected to a remote server over SSH: install on the server, open the web view through the tunnel above." "2) 로컬 앱을 원격 서버에 SSH로 연결해 쓰는 경우: 원격 서버에 설치하고, 웹 화면은 위 터널로 접속합니다.")"
say "  $(m "3) CLI over SSH on a remote server: install on the server, web view through the tunnel, terminal view in that SSH session." "3) 원격 서버에 SSH로 접속해 CLI로 실행하는 경우: 원격 서버에 설치하고, 웹 화면은 터널로, 터미널 화면은 그 SSH 터미널에서 엽니다.")"
say ""
say "$(m "[Update]" "[업데이트]") $CMD update   ($(m "keeps your view, port and tracked sessions" "화면 종류, 포트, 등록 목록은 유지합니다"))"
say "$(m "[Uninstall]" "[삭제]") $CMD uninstall   ($(m "restores the state before install" "설치 전 상태로 되돌립니다"))"
say ""
