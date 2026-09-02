#!/bin/bash
# 우분투(오라클) 서버에서 맥미니로 Marvis 데이터를 가져옵니다.
#
#   ./deploy/migrate-from-oracle.sh ubuntu@<서버IP> ~/.ssh/<키파일>
#
# 순서가 중요합니다. 먼저 원격 서비스를 멈춰서 DB 쓰기를 끝낸 뒤 복사합니다.
# 돌고 있는 SQLite를 복사하면 WAL이 어긋난 채로 넘어올 수 있습니다.
#
# 오라클 인스턴스는 지우지 않습니다. 서비스만 멈추고 며칠 두었다가,
# 맥미니가 안정적이면 그때 정리하세요.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "사용법: $0 <ssh대상> [키파일]" >&2
  echo "예:    $0 ubuntu@123.45.67.89 ~/.ssh/oracle.key" >&2
  exit 1
fi

TARGET="$1"
KEY="${2:-}"
SSH_OPTS=()
[ -n "$KEY" ] && SSH_OPTS=(-i "$KEY")

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="~/marvis-bot"
STAMP="$(date +%Y%m%d-%H%M%S)"

# 검증 단계가 marvis 패키지를 import하므로 어디서 실행하든 루트로 옮깁니다.
cd "$PROJECT_DIR"

echo "대상   $TARGET"
echo "받는곳 $PROJECT_DIR"
echo

echo "1. 원격 봇 정지 (DB 쓰기 마무리)"
ssh "${SSH_OPTS[@]}" "$TARGET" "sudo systemctl stop marvis-bot"

echo "2. 원격 WAL 체크포인트"
# WAL 내용을 본 파일에 합쳐 두면 marvis.db 하나만 가져와도 안전합니다.
ssh "${SSH_OPTS[@]}" "$TARGET" \
  "cd $REMOTE_DIR && ./venv/bin/python -c \"
import sqlite3
c = sqlite3.connect('marvis.db')
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
c.close()
print('checkpoint done')\"" || echo "   (체크포인트 실패 — WAL 파일도 함께 받습니다)"

echo "3. 기존 로컬 파일 백업"
for f in marvis.db .env; do
  if [ -f "$PROJECT_DIR/$f" ]; then
    cp "$PROJECT_DIR/$f" "$PROJECT_DIR/${f}.before-migration-${STAMP}"
    echo "   $f → ${f}.before-migration-${STAMP}"
  fi
done

echo "4. 복사"
scp "${SSH_OPTS[@]}" "$TARGET:$REMOTE_DIR/.env" "$PROJECT_DIR/.env"
scp "${SSH_OPTS[@]}" "$TARGET:$REMOTE_DIR/marvis.db" "$PROJECT_DIR/marvis.db"
# WAL/SHM은 있으면 가져오고 없으면 넘어갑니다.
scp "${SSH_OPTS[@]}" "$TARGET:$REMOTE_DIR/marvis.db-wal" "$PROJECT_DIR/" 2>/dev/null || true
scp "${SSH_OPTS[@]}" "$TARGET:$REMOTE_DIR/marvis.db-shm" "$PROJECT_DIR/" 2>/dev/null || true
chmod 600 "$PROJECT_DIR/.env"

echo "5. 검증"
"/Users/jinwoong_kim/Desktop/project/Project_AI/_venvs/marvis/bin/python" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.getcwd())
from marvis.db import get_connection, init_db
init_db()
conn = get_connection()
print("   무결성:", conn.execute("PRAGMA integrity_check").fetchone()[0])
for table in ("items", "projects", "events", "config"):
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"   {table:9} {n:>5}건")
PYEOF

echo
echo "복사 완료. 오라클 서비스는 멈춘 상태이며 인스턴스는 살아 있습니다."
echo
echo "주의: 오라클 봇을 다시 켜지 마세요. 두 봇이 같은 토큰으로 동시에"
echo "      폴링하면 텔레그램이 409를 내며 둘 다 불안정해집니다."
echo "      되돌리려면 맥 데몬을 먼저 내리세요:"
echo "        sudo ./deploy/uninstall-macos.sh"
echo
echo "다음: sudo ./deploy/install-macos.sh"
