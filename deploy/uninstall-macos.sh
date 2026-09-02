#!/bin/bash
# Marvis 서비스를 내립니다. 코드와 데이터는 그대로 둡니다.
set -euo pipefail
LABEL="com.marvis.bot"
if [ "$EUID" -ne 0 ]; then echo "sudo로 실행하세요: sudo $0" >&2; exit 1; fi
launchctl bootout "system/${LABEL}" 2>/dev/null || true
rm -f "/Library/LaunchDaemons/${LABEL}.plist"
echo "내렸습니다. 데이터(marvis.db)와 코드는 그대로입니다."
