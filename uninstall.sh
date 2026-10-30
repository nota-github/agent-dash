#!/bin/sh
# 사용법: uninstall.sh [--exact] [--yes] [--purge-backup]
# root와 현재 사용자 순서로 설치 전 상태로 되돌린다. 한 줄 설치를 했다면 agent-dash uninstall과 같다.
#   --exact         설치 후 다른 이유로 바뀐 부분도 원본으로 되돌린다 (diff 확인 후)
#   --purge-backup  원복이 끝난 뒤 백업 폴더를 지운다
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
CLI="$DIR/agentdash/cli.py"
BACKUP="$HOME/.agentdash-backup"
# 개발 저장소 옆에 notes/ 폴더가 있으면 보고서를 거기에 남긴다. 없으면 백업 폴더 안에 남긴다.
NOTES="$(dirname "$DIR")/notes"
REPORT=""
[ -d "$NOTES" ] && REPORT="--report-dir $NOTES"

opts=""
purge=0
yes=""
for a in "$@"; do
    case "$a" in
        --exact) opts="$opts --exact" ;;
        --yes) yes="--yes" ;;
        --purge-backup) purge=1 ;;
        *) echo "알 수 없는 옵션: $a" >&2; exit 2 ;;
    esac
done

status=0
python3 -I "$CLI" stop || true
if sudo -n test -e "$BACKUP/root/ACTIVE" 2>/dev/null; then
    echo "== root (/root)"
    sudo -n python3 -I "$CLI" --home /root --backup-root "$BACKUP" uninstall $opts $yes $REPORT || status=1
    sudo -n chown "$(id -u):$(id -g)" "$NOTES"/rollback-root-*.md 2>/dev/null || true
fi
echo "== $(id -un) ($HOME)"
python3 -I "$CLI" uninstall $opts $yes $REPORT || status=1

if [ "$status" -eq 0 ] && [ "$purge" -eq 1 ]; then
    if sudo -n test -d "$BACKUP/root" 2>/dev/null; then
        sudo -n python3 -I "$CLI" --home /root --backup-root "$BACKUP" purge-backup $yes || status=1
    fi
    python3 -I "$CLI" purge-backup $yes || status=1
fi
exit $status
