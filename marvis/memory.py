"""기억의 생성, 정리, 조회, 완료 처리와 출력 형식을 담당합니다."""

from datetime import datetime

from .schedule_parser import classify_memory, extract_reminder_datetime, extract_schedule_date
from .settings import MAX_IDEA_ITEMS, MAX_MEMORY_ITEMS, MEMORY_FILE
from .storage import load_json_file, load_raw_memory, memory_lock, save_raw_memory
from .time_utils import now_string, today_kst_date


def parse_schedule_date(item: dict):
    """기억 항목의 일정 날짜를 비교 가능한 date 객체로 변환합니다."""
    schedule_date = item.get("schedule_date")
    if not schedule_date:
        return None
    try:
        return datetime.fromisoformat(schedule_date).date()
    except ValueError:
        return None


def renumber_memories(memories: list) -> list:
    """삭제와 최적화 후 기억 ID가 연속되도록 다시 번호를 부여합니다."""
    for index, item in enumerate(memories, start=1):
        item["id"] = index
    return memories


def optimize_memory_items(items: list) -> list:
    """지난 일정과 오래된 항목을 정리해 기억 데이터 크기를 제한합니다."""
    today = today_kst_date()
    kept = []
    for item in items:
        if item.get("type") == "schedule":
            schedule_date = parse_schedule_date(item)
            if schedule_date and schedule_date < today:
                continue
        kept.append(item)

    # 아이디어는 최근 항목을 우선 보존합니다.
    ideas = [item for item in kept if item.get("type") == "idea"]
    if len(ideas) > MAX_IDEA_ITEMS:
        ids_to_keep = {item.get("id") for item in ideas[-MAX_IDEA_ITEMS:]}
        kept = [
            item for item in kept
            if item.get("type") != "idea" or item.get("id") in ids_to_keep
        ]

    # 전체 제한을 넘으면 일반 메모부터 오래된 순서로 제거합니다.
    while len(kept) > MAX_MEMORY_ITEMS:
        note_index = next(
            (index for index, item in enumerate(kept) if item.get("type") == "note"),
            None,
        )
        if note_index is None:
            kept = kept[-MAX_MEMORY_ITEMS:]
            break
        kept.pop(note_index)
    return renumber_memories(kept)


def load_memory() -> list:
    return load_raw_memory()


def save_memory(items: list) -> None:
    """최적화 규칙을 적용한 뒤 전체 기억을 저장합니다."""
    save_raw_memory(optimize_memory_items(items))


def prune_past_schedules() -> int:
    """지난 날짜의 일정을 제거하고 제거한 개수를 반환합니다."""
    with memory_lock:
        before = load_json_file(MEMORY_FILE, [])
        if not isinstance(before, list):
            before = []
        after = optimize_memory_items(before)
        save_raw_memory(after)
        return max(0, len(before) - len(after))


def add_memory(text: str) -> dict:
    """메시지를 분류하고 날짜·알림 정보를 붙여 새 기억으로 저장합니다."""
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

    with memory_lock:
        memories = load_memory()
        item = {
            "id": len(memories) + 1,
            "type": memory_type,
            "content": text.strip(),
            "schedule_date": schedule_date,
            "reminder_at": reminder_at,
            "reminded": False,
            "created_at": now_string(),
            "done": False,
        }
        memories.append(item)
        save_memory(memories)
        final_memories = load_memory()
        return final_memories[-1] if final_memories else item


def get_recent_memories(limit: int = 30) -> list:
    return load_memory()[-limit:]


def get_active_schedules() -> list:
    prune_past_schedules()
    return [
        item for item in load_memory()
        if item.get("type") == "schedule" and not item.get("done", False)
    ]


def get_ideas() -> list:
    return [item for item in load_memory() if item.get("type") == "idea"]


def format_memories(items: list) -> str:
    """기억 목록을 Telegram에서 읽기 쉬운 텍스트로 변환합니다."""
    if not items:
        return "저장된 기억이 없습니다."
    lines = []
    for item in items:
        status = "완료" if item.get("done") else "미완료"
        date_part = f", 일정일: {item['schedule_date']}" if item.get("schedule_date") else ""
        reminder_part = f", 알림: {item['reminder_at']}" if item.get("reminder_at") else ""
        lines.append(
            f"[{item['id']}] ({item.get('type', 'note')}, {status}{date_part}{reminder_part}) "
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
            lines.append(f"{item['id']}. {item['content']}{reminder_text}")
        lines.append("")
    if undated:
        lines.append("날짜 미정")
        lines.extend(f"{item['id']}. {item['content']}" for item in undated)
    return "\n".join(lines).strip()


def mark_done(memory_id: int) -> bool:
    """지정한 기억 항목을 완료 처리하고 성공 여부를 반환합니다."""
    with memory_lock:
        memories = load_memory()
        for item in memories:
            if item.get("id") == memory_id:
                item["done"] = True
                item["done_at"] = now_string()
                save_memory(memories)
                return True
        return False


def delete_all_memory() -> None:
    """저장된 전체 기억을 비웁니다."""
    with memory_lock:
        save_raw_memory([])
