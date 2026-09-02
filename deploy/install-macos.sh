#!/bin/bash
# 맥미니에 Marvis를 상시 실행 서비스로 설치합니다.
#
#   sudo ./deploy/install-macos.sh
#
# 하는 일:
#   1. 로그 디렉터리 생성
#   2. LaunchDaemon 설치 및 기동
#   3. 절전/자동재시작 설정 확인
#
# 되돌리려면: sudo ./deploy/uninstall-macos.sh

set -euo pipefail

LABEL="com.marvis.bot"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/${LABEL}.plist"
PLIST_DST="/Library/LaunchDaemons/${LABEL}.plist"
LOG_DIR="/Users/jinwoong_kim/Library/Logs/marvis"
PROJECT_DIR="/Users/jinwoong_kim/Desktop/project/Project_AI/LLM/Marvis"

if [ "$EUID" -ne 0 ]; then
  echo "sudo로 실행해야 합니다: sudo $0" >&2
  exit 1
fi

if [ ! -f "${PROJECT_DIR}/.env" ]; then
  echo "오류: ${PROJECT_DIR}/.env 가 없습니다." >&2
  echo "      우분투 서버에서 먼저 복사해 오세요." >&2
  exit 1
fi

echo "1. 로그 디렉터리"
mkdir -p "$LOG_DIR"
chown jinwoong_kim:staff "$LOG_DIR"
echo "   $LOG_DIR"

echo "2. LaunchDaemon 설치"
# 이미 떠 있으면 먼저 내립니다. 프로세스가 커널 호출 안에서 멈춰 있으면
# 종료에 시간이 걸리는데, 그 사이에 bootstrap을 치면 아직 등록 해제가 끝나지
# 않아 "Input/output error 5"가 납니다. 실제로 사라질 때까지 기다립니다.
if launchctl print "system/${LABEL}" >/dev/null 2>&1; then
  echo "   기존 서비스 정지 중..."
  launchctl bootout "system/${LABEL}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    launchctl print "system/${LABEL}" >/dev/null 2>&1 || break
    sleep 1
  done
  if launchctl print "system/${LABEL}" >/dev/null 2>&1; then
    echo "   경고: 20초를 기다려도 내려가지 않았습니다. 강제로 진행합니다." >&2
  fi
fi

cp "$PLIST_SRC" "$PLIST_DST"
chown root:wheel "$PLIST_DST"
chmod 644 "$PLIST_DST"
launchctl bootstrap system "$PLIST_DST"
launchctl enable "system/${LABEL}"
echo "   $PLIST_DST"

echo "3. 전원 설정"
# 잠들면 봇이 멈춥니다. 정전 복구 후 자동으로 다시 켜지게도 합니다.
pmset -a sleep 0 disksleep 0 womp 1 autorestart 1
echo "   sleep=0 disksleep=0 womp=1 autorestart=1"

echo
echo "4. 기동 확인"
sleep 6
if pgrep -f "bot.py" >/dev/null; then
  echo "   실행 중 (pid $(pgrep -f 'bot.py' | head -1))"
else
  echo "   경고: 프로세스가 보이지 않습니다. 로그를 확인하세요." >&2
  tail -5 "${LOG_DIR}/bot.error.log" 2>/dev/null || true
fi

echo
echo "설치 완료. 상태 확인:"
echo "  sudo launchctl print system/${LABEL} | head -20"
echo "  tail -f ${LOG_DIR}/bot.log"
