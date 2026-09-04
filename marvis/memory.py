"""기억의 생성, 보관, 조회, 완료 처리와 출력 형식을 담당합니다.

Phase 0에서 바뀐 것 두 가지:

* 항목마다 영구 UUID(`id`)와, 사용자에게 노출하는 안정적 번호(`seq`)를 가집니다.
  `seq`는 한 번 부여되면 절대 바뀌지 않으므로 `/done 12`는 언제 실행해도 같은
  항목을 가리킵니다.
* 지난 일정을 삭제하지 않고 보관(archive)합니다. 조회에서만 빠지고 데이터는
  남습니다.
"""

from datetime import date, datetime, timedelta

from .db import get_connection, log_event, new_id, next_seq, transaction
from .schedule_parser import classify_memory, extract_reminder_datetime, extract_schedule_date
from .time_utils import now_string, today_kst_date

WEEKDAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]

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


def mark_done(seq: int, source: str = "telegram") -> bool:
    """사용자가 말한 번호(seq)로 항목을 완료 처리합니다.

    source 를 받는 이유: 예전에는 "telegram" 으로 못박혀 있어서, 누가 완료
    처리했는지가 이벤트 로그에 남지 않았습니다. 2026-09-04 에 shadow 라우터가
    사용자의 일정 다섯 건을 임의로 완료 처리했는데, 로그만 봐서는 사용자가
    한 것과 구별되지 않았습니다. 되짚으려면 출처가 남아 있어야 합니다.
    """
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
            tx, "item.done", entity="item", entity_id=row["id"], source=source,
            payload={"seq": seq},
        )
        return True


def reopen_item(seq: int, source: str = "telegram") -> dict:
    """완료 표시를 되돌립니다. 잘못 눌린 완료는 알림을 조용히 꺼 버립니다."""
    with transaction() as tx:
        row = tx.execute(
            "SELECT id, content FROM items WHERE seq = ? AND done = 1", (int(seq),)
        ).fetchone()
        if row is None:
            return {"reopened": False, "verified": False, "seq": int(seq),
                    "reason": "not_found_or_not_done"}
        tx.execute(
            "UPDATE items SET done = 0, done_at = NULL WHERE id = ?", (row["id"],)
        )
        log_event(
            tx, "item.reopened", entity="item", entity_id=row["id"], source=source,
            payload={"seq": int(seq)},
        )
        content = row["content"]

    after = get_item(seq)
    verified = after is not None and not after["done"]
    return {"reopened": True, "verified": verified, "seq": int(seq), "content": content}


def get_item(seq: int) -> dict | None:
    """번호로 항목 하나를 읽습니다. 보관된 것도 보입니다(확인용)."""
    row = get_connection().execute(
        f"SELECT {_COLUMNS} FROM items WHERE seq = ?", (int(seq),)
    ).fetchone()
    return dict(row) if row else None


def delete_item(seq: int) -> dict:
    """사용자가 "지워줘"라고 한 항목을 조회에서 치웁니다.

    실제로는 archived 플래그를 세웁니다(이 저장소는 아무것도 지우지 않습니다).
    사용자에게는 사라진 것과 같지만, 잘못 지웠을 때 되살릴 수 있습니다.

    돌려주는 값에는 **쓰기 뒤에 다시 읽은 결과**가 들어 있습니다. 호출부는
    `verified` 가 True 일 때만 "지웠습니다"라고 말해야 합니다. 예전에는
    완료 처리(done=1)를 해 놓고 "삭제했습니다"라고 답해서, 조회하면 항목이
    그대로 남아 있었습니다.
    """
    with transaction() as tx:
        row = tx.execute(
            "SELECT id, content FROM items WHERE seq = ? AND archived = 0", (int(seq),)
        ).fetchone()
        if row is None:
            return {"deleted": False, "verified": False, "seq": int(seq),
                    "reason": "not_found"}
        tx.execute(
            "UPDATE items SET archived = 1, archived_at = ?, archive_reason = 'user_deleted'"
            " WHERE id = ?",
            (now_string(), row["id"]),
        )
        log_event(
            tx, "item.deleted", entity="item", entity_id=row["id"], source="telegram",
            payload={"seq": int(seq), "content": row["content"]},
        )
        content = row["content"]

    # 트랜잭션이 커밋된 뒤에 저장소에서 다시 읽습니다. 우리가 보낸 값이 아니라
    # 실제로 남은 값을 봅니다.
    after = get_item(seq)
    verified = after is not None and bool(after["archived"])
    return {"deleted": True, "verified": verified, "seq": int(seq), "content": content}


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


# ------------------------------------------------------------------ 반복 일정
#
# items 는 한 줄이 한 번의 일정입니다. recurrences 는 한 줄이 규칙입니다.
# "월~금 06:10" 을 84건의 단발로 펼쳐 놓으면 시각 하나를 바꾸려 할 때 84군데를
# 고쳐야 하고, 사용자가 말한 규칙 자체는 어디에도 남지 않습니다.

_RECURRENCE_COLUMNS = (
    "id, seq, content, weekdays, at_time, starts_on, ends_on, timezone,"
    " last_fired_on, archived, archived_at, source, created_at, updated_at"
)


def parse_weekdays(weekdays) -> list[int]:
    """요일 표현을 월=0..일=6 정수 목록으로 정규화합니다.

    받는 형태: [0,1,2], "0,1,2", "월화수", "월~금".
    """
    if isinstance(weekdays, str):
        text = weekdays.strip()
        if not text:
            raise ValueError("요일이 비어 있습니다.")
        # '월~금' 같은 범위 표현.
        if "~" in text or "-" in text:
            separator = "~" if "~" in text else "-"
            start, _, end = text.partition(separator)
            start, end = start.strip(), end.strip()
            if start in WEEKDAY_NAMES and end in WEEKDAY_NAMES:
                first, last = WEEKDAY_NAMES.index(start), WEEKDAY_NAMES.index(end)
                if first <= last:
                    return list(range(first, last + 1))
                # '토~월' 처럼 주를 넘어가는 범위.
                return list(range(first, 7)) + list(range(0, last + 1))
        if all(character in WEEKDAY_NAMES for character in text):
            return sorted({WEEKDAY_NAMES.index(character) for character in text})
        values = [part.strip() for part in text.split(",") if part.strip()]
    else:
        values = list(weekdays)

    parsed: set[int] = set()
    for value in values:
        if isinstance(value, str) and value in WEEKDAY_NAMES:
            parsed.add(WEEKDAY_NAMES.index(value))
            continue
        number = int(value)
        if not 0 <= number <= 6:
            raise ValueError(f"요일은 0(월)~6(일)이어야 합니다. 받은 값: {number}")
        parsed.add(number)
    if not parsed:
        raise ValueError("요일이 비어 있습니다.")
    return sorted(parsed)


def format_weekdays(weekdays: str) -> str:
    """'0,1,2,3,4' -> '월~금' 또는 '월,수,금'."""
    days = [int(part) for part in weekdays.split(",") if part != ""]
    if not days:
        return "-"
    # 연속 구간이면 범위로 줄여서 읽기 쉽게 합니다.
    if len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        return f"{WEEKDAY_NAMES[days[0]]}~{WEEKDAY_NAMES[days[-1]]}"
    return ",".join(WEEKDAY_NAMES[day] for day in days)


def create_recurrence(
    content: str,
    weekdays,
    at_time: str,
    starts_on: str,
    ends_on: str | None = None,
    timezone: str = "Asia/Seoul",
    source: str = "telegram",
) -> dict:
    """반복 규칙 하나를 저장합니다."""
    days = ",".join(str(day) for day in parse_weekdays(weekdays))
    recurrence_id = new_id()
    created_at = now_string()
    content = content.strip()

    with transaction() as tx:
        seq = next_seq(tx, "recurrences")
        tx.execute(
            "INSERT INTO recurrences (id, seq, content, weekdays, at_time, starts_on,"
            " ends_on, timezone, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (recurrence_id, seq, content, days, at_time, starts_on, ends_on,
             timezone, source, created_at),
        )
        log_event(
            tx, "recurrence.created", entity="recurrence", entity_id=recurrence_id,
            source=source,
            payload={"seq": seq, "content": content, "weekdays": days,
                     "at_time": at_time, "starts_on": starts_on, "ends_on": ends_on},
        )

    return {
        "id": recurrence_id, "seq": seq, "content": content, "weekdays": days,
        "at_time": at_time, "starts_on": starts_on, "ends_on": ends_on,
        "timezone": timezone, "last_fired_on": None, "archived": 0,
        "source": source, "created_at": created_at, "updated_at": None,
    }


def list_recurrences(include_archived: bool = False) -> list[dict]:
    """저장된 반복 규칙을 번호순으로 반환합니다."""
    where = "" if include_archived else " WHERE archived = 0"
    rows = get_connection().execute(
        f"SELECT {_RECURRENCE_COLUMNS} FROM recurrences{where} ORDER BY seq"
    ).fetchall()
    return _rows_to_dicts(rows)


def get_recurrence(seq: int) -> dict | None:
    row = get_connection().execute(
        f"SELECT {_RECURRENCE_COLUMNS} FROM recurrences WHERE seq = ?", (int(seq),)
    ).fetchone()
    return dict(row) if row else None


def update_recurrence(seq: int, source: str = "telegram", **fields) -> dict:
    """반복 규칙의 일부 필드를 갱신하고, 다시 읽은 값을 돌려줍니다."""
    allowed = ("content", "weekdays", "at_time", "starts_on", "ends_on")
    changes = {key: value for key, value in fields.items()
               if key in allowed and value is not None}
    if "weekdays" in changes:
        changes["weekdays"] = ",".join(str(day) for day in parse_weekdays(changes["weekdays"]))
    if not changes:
        return {"updated": False, "verified": False, "seq": int(seq), "reason": "no_fields"}

    with transaction() as tx:
        row = tx.execute(
            "SELECT id FROM recurrences WHERE seq = ? AND archived = 0", (int(seq),)
        ).fetchone()
        if row is None:
            return {"updated": False, "verified": False, "seq": int(seq),
                    "reason": "not_found"}
        assignments = ", ".join(f"{key} = ?" for key in changes)
        tx.execute(
            f"UPDATE recurrences SET {assignments}, updated_at = ? WHERE id = ?",
            (*changes.values(), now_string(), row["id"]),
        )
        log_event(
            tx, "recurrence.updated", entity="recurrence", entity_id=row["id"],
            source=source, payload={"seq": int(seq), "changes": changes},
        )

    current = get_recurrence(seq)
    # 보낸 값이 실제로 남았는지 다시 읽어 확인합니다.
    verified = current is not None and all(
        str(current[key]) == str(value) for key, value in changes.items()
    )
    return {"updated": True, "verified": verified, "seq": int(seq), "record": current}


def delete_recurrence(seq: int, source: str = "telegram") -> dict:
    """반복 규칙을 조회에서 치웁니다(보관 처리). 쓰기 뒤에 다시 읽어 확인합니다."""
    with transaction() as tx:
        row = tx.execute(
            "SELECT id, content FROM recurrences WHERE seq = ? AND archived = 0",
            (int(seq),),
        ).fetchone()
        if row is None:
            return {"deleted": False, "verified": False, "seq": int(seq),
                    "reason": "not_found"}
        tx.execute(
            "UPDATE recurrences SET archived = 1, archived_at = ? WHERE id = ?",
            (now_string(), row["id"]),
        )
        log_event(
            tx, "recurrence.deleted", entity="recurrence", entity_id=row["id"],
            source=source, payload={"seq": int(seq), "content": row["content"]},
        )
        content = row["content"]

    current = get_recurrence(seq)
    verified = current is not None and bool(current["archived"])
    return {"deleted": True, "verified": verified, "seq": int(seq), "content": content}


def _occurs_on(rule: dict, day: date) -> bool:
    """이 규칙이 그날 울리는지 판정합니다."""
    if rule["archived"]:
        return False
    if day.isoformat() < rule["starts_on"]:
        return False
    if rule["ends_on"] and day.isoformat() > rule["ends_on"]:
        return False
    return day.weekday() in {int(part) for part in rule["weekdays"].split(",") if part != ""}


def next_occurrence(rule: dict, after: datetime, limit_days: int = 400) -> datetime | None:
    """`after` 이후 이 규칙이 처음 울릴 시각. 없으면 None."""
    hour, _, minute = rule["at_time"].partition(":")
    for offset in range(limit_days):
        day = after.date() + timedelta(days=offset)
        if not _occurs_on(rule, day):
            continue
        moment = after.replace(
            year=day.year, month=day.month, day=day.day,
            hour=int(hour), minute=int(minute), second=0, microsecond=0,
        )
        if moment > after:
            return moment
    return None


def get_due_recurrences(current: datetime, window_minutes: int = 120) -> list[dict]:
    """지금 울려야 하는 반복 규칙을 반환합니다.

    `window_minutes` 는 봇이 꺼져 있다가 한참 뒤에 켜졌을 때를 위한 것입니다.
    예정 시각을 그만큼 넘겼으면 보내지 않습니다. 오후 3시에 "05:10 보고서
    확인" 알림이 오는 것은 알림이 아니라 소음입니다.
    """
    today = current.date().isoformat()
    due = []
    for rule in list_recurrences():
        if rule["last_fired_on"] == today:
            continue
        if not _occurs_on(rule, current.date()):
            continue
        hour, _, minute = rule["at_time"].partition(":")
        scheduled = current.replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0
        )
        if current < scheduled:
            continue
        if (current - scheduled).total_seconds() > window_minutes * 60:
            continue
        due.append(rule)
    return due


def mark_recurrence_fired(recurrence_id: str, day: str) -> bool:
    """그날의 발송을 기록합니다. 같은 날 두 번 울리지 않게 하는 자물쇠입니다."""
    with transaction() as tx:
        cursor = tx.execute(
            "UPDATE recurrences SET last_fired_on = ?, updated_at = ?"
            " WHERE id = ? AND (last_fired_on IS NULL OR last_fired_on <> ?)",
            (day, now_string(), recurrence_id, day),
        )
        if cursor.rowcount == 0:
            return False
        log_event(
            tx, "recurrence.fired", entity="recurrence", entity_id=recurrence_id,
            source="reminder_loop", payload={"date": day},
        )
        return True


def skip_stale_recurrences(current: datetime, window_minutes: int = 120) -> int:
    """예정 시각을 한참 넘긴 오늘치 규칙을 '건너뜀'으로 기록합니다.

    이렇게 표시해 두지 않으면 봇이 낮에 재시작될 때마다 같은 규칙을 다시
    검사하게 되고, 무엇을 놓쳤는지도 남지 않습니다.
    """
    today = current.date().isoformat()
    skipped = 0
    for rule in list_recurrences():
        if rule["last_fired_on"] == today or not _occurs_on(rule, current.date()):
            continue
        hour, _, minute = rule["at_time"].partition(":")
        scheduled = current.replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0
        )
        if current <= scheduled:
            continue
        if (current - scheduled).total_seconds() <= window_minutes * 60:
            continue
        with transaction() as tx:
            tx.execute(
                "UPDATE recurrences SET last_fired_on = ?, updated_at = ? WHERE id = ?",
                (today, now_string(), rule["id"]),
            )
            log_event(
                tx, "recurrence.skipped", entity="recurrence", entity_id=rule["id"],
                source="reminder_loop",
                payload={"date": today, "reason": "window_passed"},
            )
        skipped += 1
    return skipped


# ------------------------------------------------------------------ 원본 출력
#
# 목록의 기본값은 각색이 아니라 레코드 필드 그대로입니다. 예전에는 요약만
# 보여줘서, 단발 한 건이 "[반복 알림 규칙]"으로 렌더링되는 것을 사용자가
# 알아챌 방법이 없었습니다.

def format_recurrences_raw(rules: list[dict] | None = None) -> str:
    """반복 규칙을 저장된 필드 그대로 보여줍니다."""
    rules = list_recurrences() if rules is None else rules
    if not rules:
        return "저장된 반복 규칙이 없습니다."
    lines = []
    for rule in rules:
        ends = rule["ends_on"] or "종료일 없음"
        fired = rule["last_fired_on"] or "발송 이력 없음"
        lines.append(
            f"[R{rule['seq']}] recurrence | {format_weekdays(rule['weekdays'])}"
            f" ({rule['weekdays']}) | {rule['at_time']} {rule['timezone']}"
            f" | {rule['starts_on']} ~ {ends} | 마지막 발송: {fired}\n"
            f"      {rule['content']}"
        )
    return "\n".join(lines)


def format_schedule_raw() -> str:
    """미완료 일정과 반복 규칙을 레코드 필드 그대로 보여줍니다."""
    items = get_active_schedules()
    blocks = [f"반복 규칙 {len(list_recurrences())}건", format_recurrences_raw(), ""]
    blocks.append(f"단발 일정 {len(items)}건")
    if not items:
        blocks.append("저장된 단발 일정이 없습니다.")
    else:
        for item in items:
            blocks.append(
                f"[{item['seq']}] {item['type']} | 일정일: {item['schedule_date'] or '-'}"
                f" | 알림: {item['reminder_at'] or '-'}"
                f" | 완료: {'예' if item['done'] else '아니오'}"
                f" | 저장: {item['created_at']}\n"
                f"      {item['content']}"
            )
    return "\n".join(blocks).strip()
