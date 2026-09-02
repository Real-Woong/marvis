"""JSON 파일 저장소를 SQLite로 옮기는 1회성 마이그레이션입니다.

`python -m marvis.migrate` 로 직접 실행하거나, 봇 기동 시 app.py가 호출합니다.
여러 번 실행해도 안전합니다(이미 옮겼으면 아무 일도 하지 않습니다).
"""

import json
import logging
import shutil
from pathlib import Path

from .db import get_connection, get_meta, init_db, log_event, new_id, set_meta, transaction
from .settings import LEGACY_CONFIG_FILE, LEGACY_MEMORY_FILE, LEGACY_PROJECTS_FILE
from .time_utils import now_string

MIGRATION_KEY = "migrated_from_json_at"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logging.error("Could not read %s: %s", path, error)
        return default


def _backup(path: Path) -> None:
    """원본 JSON은 지우지 않고 .bak으로 한 벌 남깁니다."""
    if not path.exists():
        return
    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)


def _migrate_items(conn) -> int:
    """기억 항목을 옮깁니다. 예전 정수 id는 seq로, 새 UUID를 부여합니다."""
    raw = _read_json(LEGACY_MEMORY_FILE, [])
    if not isinstance(raw, list):
        return 0

    count = 0
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or not item.get("content"):
            continue
        item_id = new_id()
        conn.execute(
            "INSERT INTO items (id, seq, type, content, schedule_date, reminder_at,"
            " reminded, reminded_at, done, done_at, archived, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'migration', ?)",
            (
                item_id,
                index,
                item.get("type") or "note",
                str(item["content"]).strip(),
                item.get("schedule_date"),
                item.get("reminder_at"),
                1 if item.get("reminded") else 0,
                item.get("reminded_at"),
                1 if item.get("done") else 0,
                item.get("done_at"),
                item.get("created_at") or now_string(),
            ),
        )
        count += 1
    return count


def _migrate_projects(conn) -> int:
    raw = _read_json(LEGACY_PROJECTS_FILE, [])
    if not isinstance(raw, list):
        return 0

    count = 0
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        conn.execute(
            "INSERT INTO projects (id, seq, name, status, sub_status, next_steps, note,"
            " muted_from_briefing, archived, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                new_id(),
                index,
                str(item["name"]).strip(),
                item.get("status") or "진행중",
                item.get("sub_status"),
                item.get("next_steps"),
                item.get("note"),
                1 if item.get("muted_from_briefing") else 0,
                item.get("created_at") or now_string(),
                item.get("updated_at") or now_string(),
            ),
        )
        count += 1
    return count


def _migrate_config(conn) -> int:
    raw = _read_json(LEGACY_CONFIG_FILE, {})
    if not isinstance(raw, dict):
        return 0

    count = 0
    for key, value in raw.items():
        if value is None:
            continue
        conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, str(value), now_string()),
        )
        count += 1
    return count


def run_migration_if_needed() -> bool:
    """아직 옮기지 않았고 옮길 JSON이 있으면 마이그레이션합니다."""
    init_db()

    if get_meta(MIGRATION_KEY):
        return False

    legacy_files = [LEGACY_MEMORY_FILE, LEGACY_PROJECTS_FILE, LEGACY_CONFIG_FILE]
    if not any(path.exists() for path in legacy_files):
        # 새로 시작하는 설치. 옮길 게 없으니 완료로 표시만 합니다.
        with transaction() as tx:
            set_meta(tx, MIGRATION_KEY, now_string())
        return False

    # 이미 데이터가 들어 있는 DB에는 덮어쓰지 않습니다.
    existing = get_connection().execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    if existing:
        logging.warning("items 테이블에 이미 %s건이 있어 마이그레이션을 건너뜁니다.", existing)
        with transaction() as tx:
            set_meta(tx, MIGRATION_KEY, now_string())
        return False

    for path in legacy_files:
        _backup(path)

    with transaction() as tx:
        items = _migrate_items(tx)
        projects = _migrate_projects(tx)
        config = _migrate_config(tx)
        set_meta(tx, MIGRATION_KEY, now_string())
        log_event(
            tx,
            "system.migrated",
            entity="system",
            source="migration",
            payload={"items": items, "projects": projects, "config_keys": config},
        )

    logging.info(
        "JSON → SQLite 마이그레이션 완료: 기억 %s건, 프로젝트 %s건, 설정 %s개",
        items,
        projects,
        config,
    )
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not run_migration_if_needed():
        print("옮길 데이터가 없거나 이미 마이그레이션되었습니다.")
