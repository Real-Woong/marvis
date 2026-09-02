"""저장된 데이터를 실제로 삭제하고 처음부터 다시 시작합니다.

    python -m marvis.reset                  # 기억·일정만 (기본)
    python -m marvis.reset --projects       # 프로젝트도 함께
    python -m marvis.reset --all            # 전부 (이벤트 로그 포함)
    python -m marvis.reset --dry-run        # 뭐가 지워질지만 보기

텔레그램의 /forget_all_marvis와 다릅니다. 그쪽은 보관(archive)이라 되돌릴 수
있고, 이쪽은 행을 실제로 지웁니다. 실수로 부르기 어렵도록 일부러 CLI에만
뒀고, 확인 문구를 직접 입력해야 진행됩니다.

지우기 전에 항상 backups/ 아래에 JSON으로 한 벌 떠 둡니다.
"""

import argparse
import json
import sys
from pathlib import Path

from .db import get_connection, init_db, log_event, transaction
from .settings import BASE_DIR
from .time_utils import now_kst, now_string

BACKUP_DIR = BASE_DIR / "backups"
CONFIRM_PHRASE = "RESET"

# config는 기본적으로 남깁니다. telegram_chat_id가 지워지면 /start를 다시
# 보내기 전까지 능동 알림이 나가지 않습니다.
TABLES = {
    "items": "기억·일정·아이디어",
    "projects": "프로젝트",
    "events": "이벤트 로그 (발화 기록 · eval 데이터셋)",
    "config": "설정 (채팅 ID, 마지막 브리핑 날짜)",
}


def counts() -> dict[str, int]:
    conn = get_connection()
    return {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in TABLES
    }


def write_backup(tables: list[str]) -> Path:
    """지울 테이블의 현재 내용을 JSON으로 떠 둡니다."""
    BACKUP_DIR.mkdir(exist_ok=True)
    conn = get_connection()
    payload = {
        "created_at": now_string(),
        "tables": {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in tables
        },
    }
    path = BACKUP_DIR / f"reset-{now_kst().strftime('%Y%m%d-%H%M%S')}.json"
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    return path


def reset(tables: list[str]) -> dict[str, int]:
    """지정한 테이블을 비웁니다. seq는 MAX+1이라 자동으로 1번부터 다시 시작합니다."""
    before = counts()
    with transaction() as tx:
        for table in tables:
            tx.execute(f"DELETE FROM {table}")
        if "events" not in tables:
            # 이벤트 로그를 남긴 경우, 초기화했다는 사실 자체를 기록합니다.
            log_event(
                tx,
                "system.reset",
                entity="system",
                source="cli",
                payload={table: before[table] for table in tables},
            )
    # 지운 공간을 실제로 반환합니다.
    get_connection().execute("VACUUM")
    return {table: before[table] for table in tables}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Marvis 데이터를 실제로 삭제합니다. 되돌릴 수 없습니다."
    )
    parser.add_argument("--projects", action="store_true", help="프로젝트도 함께 삭제")
    parser.add_argument(
        "--events",
        action="store_true",
        help="이벤트 로그도 삭제 (발화 기록과 eval 데이터셋이 사라집니다)",
    )
    parser.add_argument("--config", action="store_true", help="설정도 삭제 (채팅 ID 포함)")
    parser.add_argument("--all", action="store_true", help="위 전부")
    parser.add_argument("--dry-run", action="store_true", help="지우지 않고 대상만 보기")
    parser.add_argument("--yes", action="store_true", help="확인 절차 건너뛰기 (스크립트용)")
    options = parser.parse_args()

    init_db()

    tables = ["items"]
    if options.all or options.projects:
        tables.append("projects")
    if options.all or options.events:
        tables.append("events")
    if options.all or options.config:
        tables.append("config")

    current = counts()
    print(f"대상 DB: {get_connection().execute('PRAGMA database_list').fetchone()['file']}\n")
    print("지울 것:")
    for table in tables:
        print(f"  {table:9} {current[table]:>6}건  — {TABLES[table]}")
    keep = [t for t in TABLES if t not in tables]
    if keep:
        print("\n남길 것:")
        for table in keep:
            print(f"  {table:9} {current[table]:>6}건  — {TABLES[table]}")

    if not sum(current[table] for table in tables):
        print("\n지울 데이터가 없습니다.")
        return 0

    if options.dry_run:
        print("\n--dry-run 이므로 아무것도 지우지 않았습니다.")
        return 0

    if "events" in tables:
        print(
            "\n경고: 이벤트 로그를 지우면 지금까지 쌓인 발화 기록이 사라집니다."
            "\n      Phase 1의 골든셋을 이 기록에서 뽑기로 했습니다."
        )

    if not options.yes:
        print(f"\n되돌릴 수 없습니다. 진행하려면 {CONFIRM_PHRASE} 를 입력하세요.")
        try:
            answer = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n취소했습니다.")
            return 1
        if answer != CONFIRM_PHRASE:
            print("취소했습니다.")
            return 1

    backup_path = write_backup(tables)
    print(f"\n백업: {backup_path}")

    removed = reset(tables)
    print("\n삭제 완료:")
    for table, count in removed.items():
        print(f"  {table:9} {count:>6}건")
    print("\n번호(seq)는 다시 1번부터 시작합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
