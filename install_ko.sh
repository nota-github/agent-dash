#!/bin/sh
# agent-dash 설치 스크립트 (한국어 안내)
#
#   한 줄 설치: curl -fsSL https://raw.githubusercontent.com/nota-github/agent-dash/main/install_ko.sh | sh
#
# 설치 과정은 install.sh와 같고 안내 문구만 한국어다. 고른 언어는 설정에 남아 agent-dash update에도 쓰인다.
set -eu
export AGENT_DASH_LANG=ko
DIR=$(dirname "$0")
if [ -f "$0" ] && [ -f "$DIR/install.sh" ]; then
    exec sh "$DIR/install.sh" "$@"
fi
REPO="${AGENT_DASH_REPO:-nota-github/agent-dash}"
REF="${AGENT_DASH_REF:-main}"
curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/install.sh" | sh
