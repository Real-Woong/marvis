"""SQLite 연결, 스키마, 추가 전용 이벤트 로그를 담당합니다.

Phase 0에서 JSON 파일 저장소를 대체합니다. 설계상 중요한 두 가지:

1. 모든 레코드는 재부여되지 않는 UUID(`id`)와, 사용자에게 보여줄 안정적인
   정수 번호(`seq`)를 함께 가집니다. 예전 JSON 구조는 저장할 때마다 번호를
   1..N으로 다시 매겨서 `/done 3`이 매번 다른 항목을 가리켰습니다.
2. 삭제 대신 보관(archive)합니다. 지난 일정도 남겨 두어야 "지난주에 뭐 했지"에
   답할 수 있고, 나중에 eval 데이터셋으로 쓸 수 있습니다.
"""

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from .settings import DB_FILE
from .time_utils import now_string

SCHEMA_VERSION = 1

# 스레드마다 별도 연결을 씁니다. 텔레그램 핸들러, 알림 루프, 웹훅 서버가
# 각각 다른 스레드에서 동시에 접근합니다.
_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id             TEXT PRIMARY KEY,
    seq            INTEGER NOT NULL UNIQUE,
    type           TEXT NOT NULL,
    content        TEXT NOT NULL,
    schedule_date  TEXT,
    reminder_at    TEXT,
    reminded       INTEGER NOT NULL DEFAULT 0,
    reminded_at    TEXT,
    done           INTEGER NOT NULL DEFAULT 0,
    done_at        TEXT,
    archived       INTEGER NOT NULL DEFAULT 0,
    archived_at    TEXT,
    archive_reason TEXT,
    source         TEXT NOT NULL DEFAULT 'telegram',
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_active   ON items(archived, type, done);
CREATE INDEX IF NOT EXISTS idx_items_reminder ON items(archived, done, reminded, reminder_at);
CREATE INDEX IF NOT EXISTS idx_items_created  ON items(created_at);

CREATE TABLE IF NOT EXISTS projects (
    id                  TEXT PRIMARY KEY,
    seq                 INTEGER NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL,
    sub_status          TEXT,
    next_steps          TEXT,
    note                TEXT,
    muted_from_briefing INTEGER NOT NULL DEFAULT 0,
    archived            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(archived, status);

-- 추가 전용. 수정도 삭제도 하지 않습니다.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    entity    TEXT,
    entity_id TEXT,
    source    TEXT,
    payload   TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_at     ON events(at);
CREATE INDEX IF NOT EXISTS idx_events_kind   ON events(kind, at);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity, entity_id);

CREATE TABLE IF NOT EXISTS config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def new_id() -> str:
    """레코드의 영구 식별자를 만듭니다. 재부여되지 않습니다."""
    return uuid.uuid4().hex


def get_connection() -> sqlite3.Connection:
    """현재 스레드 전용 연결을 반환합니다(없으면 만듭니다)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL은 읽기와 쓰기가 서로를 막지 않게 합니다. 알림 루프가 30초마다 읽는
    # 동안 텔레그램 핸들러가 쓰기를 기다리던 예전 락 구조를 대체합니다.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    _local.conn = conn
    return conn


@contextmanager
def transaction():
    """쓰기 트랜잭션. seq 채번과 갱신이 원자적으로 일어나도록 IMMEDIATE로 엽니다."""
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def next_seq(conn: sqlite3.Connection, table: str) -> int:
    """사용자에게 보여줄 다음 번호. 삭제된 번호를 재사용하지 않습니다."""
    if table not in ("items", "projects"):
        raise ValueError(f"unknown table: {table}")
    row = conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM {table}").fetchone()
    return int(row["next"])


def log_event(
    conn: sqlite3.Connection,
    kind: str,
    entity: str | None = None,
    entity_id: str | None = None,
    source: str | None = None,
    payload: dict | None = None,
) -> None:
    """상태 변화를 이벤트 로그에 남깁니다. 호출부의 트랜잭션 안에서 실행됩니다."""
    conn.execute(
        "INSERT INTO events (at, kind, entity, entity_id, source, payload)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            now_string(),
            kind,
            entity,
            entity_id,
            source,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        ),
    )


def get_meta(key: str, default: str | None = None) -> str | None:
    row = get_connection().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def init_db() -> None:
    """스키마를 만들고 버전을 기록합니다. 이미 있으면 아무것도 하지 않습니다."""
    conn = get_connection()
    conn.executescript(_SCHEMA)
    conn.commit()

    current = get_meta("schema_version")
    if current is None:
        with transaction() as tx:
            set_meta(tx, "schema_version", str(SCHEMA_VERSION))
        logging.info("Initialized Marvis database at %s (schema v%s)", DB_FILE, SCHEMA_VERSION)
    elif int(current) != SCHEMA_VERSION:
        # 아직 마이그레이션 경로가 하나뿐이라 경고만 남깁니다.
        logging.warning(
            "Database schema version is %s but code expects %s", current, SCHEMA_VERSION
        )
