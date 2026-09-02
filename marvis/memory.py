"""기억의 생성, 보관, 조회, 완료 처리와 출력 형식을 담당합니다.

Phase 0에서 바뀐 것 두 가지:

* 항목마다 영구 UUID(`id`)와, 사용자에게 노출하는 안정적 번호(`seq`)를 가집니다.
  `seq`는 한 번 부여되면 절대 바뀌지 않으므로 `/done 12`는 언제 실행해도 같은
  항목을 가리킵니다.
* 지난 일정을 삭제하지 않고 보관(archive)합니다. 조회에서만 빠지고 데이터는
  남습니다.
"""

from datetime import datetime

from .db import get_connection, log_event, new_id, next_seq, transaction
from .schedule_parser import classify_memory, extract_reminder_datetime, extract_schedule_date
from .time_utils import now_string, today_kst_date

# 조회에 쓰는 공통 컬럼. sqlite3.Row를 dict처럼 다루므로 기존 포매터가 그대로 동작합니다.
_COLUMNS = (
    "id, seq, type, content, schedule_date, reminder_at, reminded, reminded_at,"
    " done, done_at, archived, archived_at, archive_reason, source, created_at"
)


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def archive_past_schedules() -> int:
    """지난 날짜의 일정을 보관 처리하고 처리한 개수를 반환합니다.

    예전 `prune_past_schedules`는 항목을 영구 삭제했습니다. 이제는 `archived`
    플래그만 세우므로 히스토리가 남습니다.
    """
    today = today_kst_date().isoformat()
    with transaction() as tx:
        rows = tx.execute(
            "SELECT id FROM items WHERE archived = 0 AND type = 'schedule'"
            " AND schedule_date IS NOT NULL AND schedule_date < ?",
            (today,),
        ).fetchall()
        if not rows:
            return 0
        tx.execute(
            "UPDATE items SET archived = 1, archived_at = ?, archive_reason = 'past_schedule'"
            " WHERE archived = 0 AND type = 'schedule'"
            " AND schedule_date IS NOT NULL AND schedule_date < ?",
            (now_string(), today),
        )
        for row in rows:
            log_event(
                tx,
                "item.archived",
                entity="item",
                entity_id=row["id"],
                source="system",
                payload={"reason": "past_schedule"},
            )
        return len(rows)


def add_memory(text: str, source: str = "telegram") -> dict:
    """정규식으로 분류·날짜 추출한 뒤 저장합니다(구 라우터 경로)."""
    memory_type = classify_memory(text)
    schedule_date = extract_schedule_date(text)
    reminder_at = extract_reminder_datetime(text, schedule_date)
    if schedule_date or reminder_at:
        memory_type = "schedule"
    if reminder_at and not schedule_date:
        try:
            schedule_date = datetime.strptime(reminder_at, "%Y-%m-%d %H:%M:%S").date().isoformat()
        except ValueError:
            schedule_date = None

    return create_item(
        content=text,
        kind=memory_type,
        schedule_date=schedule_date,
        reminder_at=reminder_at,
        source=source,
    )


def create_item(
    content: str,
    kind: str,
    schedule_date: str | None = None,
    reminder_at: str | None = None,
    source: str = "telegram",
) -> dict:
    """분류와 날짜가 이미 정해진 항목을 저장합니다.

    LLM 라우터는 도구 인자로 값을 직접 받으므로 이 함수를 씁니다. 정규식
    라우터도 add_memory를 통해 결국 여기로 들어와서, 쓰기 경로는 하나입니다.
    """
    memory_type = kind
    item_id = new_id()
    created_at = now_string()
    content = content.strip()

    with transaction() as tx:
        seq = next_seq(tx, "items")
        tx.execute(
            "INSERT INTO items (id, seq, type, content, schedule_date, reminder_at,"
            " source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, seq, memory_type, content, schedule_date, reminder_at, source, created_at),
        )
        log_event(
            tx,
            "item.created",
            entity="item",
            entity_id=item_id,
            source=source,
            payload={
                "seq": seq,
                "type": memory_type,
                "content": content,
                "schedule_date": schedule_date,
                "reminder_at": reminder_at,
            },
        )

    return {
        "id": item_id,
        "seq": seq,
        "type": memory_type,
        "content": content,
        "schedule_date": schedule_date,
        "reminder_at": reminder_at,
        "reminded": 0,
        "done": 0,
        "archived": 0,
        "source": source,
        "created_at": created_at,
    }


def get_recent_memories(limit: int = 30) -> list[dict]:
    """보관되지 않은 최근 항목을 오래된 순서로 반환합니다."""
    rows = get_connection().execute(
        f"SELECT {_COLUMNS} FROM items WHERE archived = 0"
        " ORDER BY seq DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return _rows_to_dicts(reversed(rows))


def get_active_schedules() -> list[dict]:
    """미완료 일정을 반환합니다(지난 일정은 먼저 보관 처리)."""
    archive_past_schedules()
    rows = get_connection().execute(
        f"SELECT {_COLUMNS} FROM items"
        " WHERE archived = 0 AND type = 'schedule' AND done = 0 ORDER BY seq",
    ).fetchall()
    return _rows_to_dicts(rows)


def get_ideas() -> list[dict]:
    rows = get_connection().execute(
        f"SELECT {_COLUMNS} FROM items WHERE archived = 0 AND type = 'idea' ORDER BY seq",
    ).fetchall()
    return _rows_to_dicts(rows)


def get_due_reminders(now_str: str) -> list[dict]:
    """알림 시각이 지났고 아직 보내지 않은 항목을 반환합니다."""
    rows = get_connection().execute(
        f"SELECT {_COLUMNS} FROM items"
        " WHERE archived = 0 AND type = 'schedule' AND done = 0 AND reminded = 0"
        " AND reminder_at IS NOT NULL AND reminder_at <= ? ORDER BY reminder_at",
        (now_str,),
    ).fetchall()
    return _rows_to_dicts(rows)


def mark_reminded(item_id: str) -> bool:
    """UUID로 항목을 찾아 알림 발송 완료로 표시합니다.

    예전에는 매 저장마다 재부여되는 정수 번호로 항목을 다시 찾았기 때문에,
    그사이 다른 항목이 정리되면 엉뚱한 항목에 표시가 찍혔습니다.
    """
    with transaction() as tx:
        cursor = tx.execute(
            "UPDATE items SET reminded = 1, reminded_at = ? WHERE id = ? AND reminded = 0",
            (now_string(), item_id),
        )
        if cursor.rowcount == 0:
            return False
        log_event(tx, "item.reminded", entity="item", entity_id=item_id, source="reminder_loop")
        return True


def mark_done(seq: int) -> bool:
    """사용자가 말한 번호(seq)로 항목을 완료 처리합니다."""
    with transaction() as tx:
        row = tx.execute(
            "SELECT id FROM items WHERE seq = ? AND archived = 0", (seq,)
        ).fetchone()
        if row is None:
            return False
        tx.execute(
            "UPDATE items SET done = 1, done_at = ? WHERE id = ?", (now_string(), row["id"])
        )
        log_event(
            tx, "item.done", entity="item", entity_id=row["id"], source="telegram",
            payload={"seq": seq},
        )
        return True


def archive_all_memories() -> int:
    """모든 기억을 보관 처리합니다. 조회에서 사라지지만 데이터는 남습니다."""
    with transaction() as tx:
        cursor = tx.execute(
            "UPDATE items SET archived = 1, archived_at = ?, archive_reason = 'forget_all'"
            " WHERE archived = 0",
            (now_string(),),
        )
        count = cursor.rowcount
        log_event(
            tx, "item.archived_all", entity="system", source="telegram",
            payload={"count": count},
        )
        return count


def format_memories(items: list[dict]) -> str:
    """기억 목록을 Telegram에서 읽기 쉬운 텍스트로 변환합니다."""
    if not items:
        return "저장된 기억이 없습니다."
    lines = []
    for item in items:
        status = "완료" if item.get("done") else "미완료"
        date_part = f", 일정일: {item['schedule_date']}" if item.get("schedule_date") else ""
        reminder_part = f", 알림: {item['reminder_at']}" if item.get("reminder_at") else ""
        lines.append(
            f"[{item['seq']}] ({item.get('type', 'note')}, {status}{date_part}{reminder_part}) "
            f"{item['content']} - 저장시각: {item['created_at']}"
        )
    return "\n".join(lines)


def format_schedule_by_date() -> str:
    """미완료 일정을 날짜별로 묶고 날짜 미정 일정을 마지막에 표시합니다."""
    schedules = get_active_schedules()
    if not schedules:
        return "현재 남아있는 스케쥴이 없습니다."

    dated: dict[str, list] = {}
    undated = []
    for item in schedules:
        schedule_date = item.get("schedule_date")
        if schedule_date:
            dated.setdefault(schedule_date, []).append(item)
        else:
            undated.append(item)

    lines = ["현재 남아있는 스케쥴입니다.\n"]
    for schedule_date in sorted(dated):
        lines.append(schedule_date)
        for item in dated[schedule_date]:
            reminder = item.get("reminder_at")
            reminder_text = f" / 알림: {reminder[11:16]}" if reminder else ""
            lines.append(f"{item['seq']}. {item['content']}{reminder_text}")
        lines.append("")
    if undated:
        lines.append("날짜 미정")
        lines.extend(f"{item['seq']}. {item['content']}" for item in undated)
    return "\n".join(lines).strip()


def get_schedules_between(date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """기간 내 미완료 일정을 반환합니다. 날짜 미정 항목은 범위를 지정하면 제외됩니다."""
    archive_past_schedules()
    clauses = ["archived = 0", "type = 'schedule'", "done = 0"]
    params: list = []
    if date_from:
        clauses.append("schedule_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("schedule_date <= ?")
        params.append(date_to)
    rows = get_connection().execute(
        f"SELECT {_COLUMNS} FROM items WHERE {' AND '.join(clauses)}"
        " ORDER BY schedule_date IS NULL, schedule_date, seq",
        params,
    ).fetchall()
    return _rows_to_dicts(rows)


def search_memories(
    kind: str | None = None, query: str | None = None, limit: int = 20
) -> list[dict]:
    """종류와 키워드로 기억을 찾습니다."""
    clauses = ["archived = 0"]
    params: list = []
    if kind:
        clauses.append("type = ?")
        params.append(kind)
    if query:
        clauses.append("content LIKE ?")
        params.append(f"%{query}%")
    params.append(max(1, min(int(limit), 50)))
    rows = get_connection().execute(
        f"SELECT {_COLUMNS} FROM items WHERE {' AND '.join(clauses)}"
        " ORDER BY seq DESC LIMIT ?",
        params,
    ).fetchall()
    return _rows_to_dicts(reversed(rows))


def counts_summary() -> dict:
    """시스템 프롬프트에 넣을 건수 요약. 내용은 담지 않습니다."""
    row = get_connection().execute(
        "SELECT"
        "  SUM(type = 'schedule' AND done = 0) AS open_schedules,"
        "  SUM(type = 'idea') AS ideas,"
        "  COUNT(*) AS total"
        " FROM items WHERE archived = 0"
    ).fetchone()
    return {
        "open_schedules": row["open_schedules"] or 0,
        "ideas": row["ideas"] or 0,
        "total": row["total"] or 0,
    }
