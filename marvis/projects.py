"""사이드 프로젝트 진행 상태를 저장하고 조회/수정합니다."""

import re

from .settings import PROJECTS_FILE
from .storage import load_json_file, memory_lock, save_json_file
from .time_utils import now_string

STATUS_IN_PROGRESS = "진행중"
STATUS_STOPPED = "중단"
STATUSES = (STATUS_IN_PROGRESS, STATUS_STOPPED)

# 중단 상태를 더 구체적으로 설명할 때 붙일 수 있는 태그입니다.
SUB_STATUS_OPTIONS = ("test중", "중단", "log data 수집중", "일시정지")

ACTION_ADD = "add"
ACTION_STOP = "stop"
ACTION_PAUSE = "pause"
ACTION_NEXT_STEPS = "next_steps"

# '프로젝트'와 붙어 있는 단어를 이름으로 오인하지 않도록 걸러내는 동작어입니다.
_NAME_STOPWORDS = {
    "시작할거야", "시작해", "시작", "새로", "추가해줘", "추가",
    "중단으로", "중단해줘", "중단할게", "중단",
    "수정해줘", "수정",
    "다음할일은", "다음", "할일은", "할일",
    "잠깐", "잠시", "멈출게", "멈춰", "일시정지",
}


def load_projects() -> list:
    data = load_json_file(PROJECTS_FILE, [])
    return data if isinstance(data, list) else []


def save_projects(items: list) -> None:
    save_json_file(PROJECTS_FILE, items)


def get_project(project_id: int) -> dict | None:
    for item in load_projects():
        if item.get("id") == project_id:
            return item
    return None


def get_active_projects() -> list:
    return [item for item in load_projects() if item.get("status") == STATUS_IN_PROGRESS]


def get_stopped_projects() -> list:
    return [item for item in load_projects() if item.get("status") == STATUS_STOPPED]


def find_project_by_name(name: str) -> dict | None:
    """이름으로 프로젝트를 찾습니다. 후보가 여럿이면 특정할 수 없어 None을 반환합니다."""
    normalized = name.strip().lower()
    projects = load_projects()

    for item in projects:
        if item["name"].strip().lower() == normalized:
            return item

    matches = [
        item
        for item in projects
        if normalized in item["name"].strip().lower()
        or item["name"].strip().lower() in normalized
    ]
    return matches[0] if len(matches) == 1 else None


def extract_project_name(text: str) -> str | None:
    """'프로젝트'와 붙어 있는 단어를 프로젝트 이름 후보로 추출합니다."""
    idx = text.find("프로젝트")
    if idx == -1:
        return None

    before = text[:idx].strip()
    after = text[idx + len("프로젝트"):].strip()
    before_word = before.split()[-1] if before else ""
    after_word = after.split()[0] if after else ""

    if before_word and before_word not in _NAME_STOPWORDS:
        return before_word
    if after_word and after_word not in _NAME_STOPWORDS:
        return after_word
    return None


def detect_project_action(text: str) -> str | None:
    """프로젝트 추가/중단/일시정지/다음 할 일 갱신 의도를 판별합니다."""
    if "프로젝트" not in text:
        return None

    normalized = " ".join(text.lower().split())
    compact = normalized.replace(" ", "")

    pause_time_words = ("잠깐", "잠시")
    pause_stop_words = ("멈추", "멈출", "멈춰", "정지")
    if any(t in normalized for t in pause_time_words) and any(s in normalized for s in pause_stop_words):
        return ACTION_PAUSE
    if "다음할일은" in compact:
        return ACTION_NEXT_STEPS
    if "중단" in normalized and (
        "수정해" in normalized or "으로 바꿔" in normalized or "할게" in normalized or normalized.endswith("중단")
    ):
        return ACTION_STOP
    if "추가" in normalized and ("새로" in normalized or "시작" in normalized):
        return ACTION_ADD
    return None


def extract_next_steps_content(text: str) -> str | None:
    """'다음할일은 ~이야' 형태에서 실제 다음 할 일 내용만 뽑아냅니다."""
    match = re.search(r"다음\s*할\s*일은\s*(.+)", text)
    if not match:
        return None
    content = match.group(1).strip()
    content = re.sub(r"(이야|이에요|예요|야)[.!]?$", "", content).strip()
    return content or None


def format_active_projects() -> str:
    """진행중 프로젝트와 다음 할 일만 간단히 나열합니다(아침 브리핑용)."""
    active = get_active_projects()
    if not active:
        return "현재 진행중인 프로젝트가 없습니다."
    lines = []
    for item in active:
        next_steps = item.get("next_steps") or "다음 할 일 미정"
        lines.append(f"- {item['name']}: {next_steps}")
    return "\n".join(lines)


def format_all_projects() -> str:
    """전체 프로젝트를 진행중/중단으로 묶어 사람이 읽기 쉬운 텍스트로 만듭니다."""
    projects = load_projects()
    if not projects:
        return "등록된 프로젝트가 없습니다."

    active = [item for item in projects if item.get("status") == STATUS_IN_PROGRESS]
    stopped = [item for item in projects if item.get("status") == STATUS_STOPPED]

    lines = [f"진행중 ({len(active)}개)"]
    if active:
        for item in active:
            next_steps = item.get("next_steps") or "다음 할 일 미정"
            lines.append(f"[{item['id']}] {item['name']} - 다음 할 일: {next_steps}")
    else:
        lines.append("없음")

    lines.append("")
    lines.append(f"중단 ({len(stopped)}개)")
    if stopped:
        for item in stopped:
            sub_status = item.get("sub_status")
            sub_status_part = f" ({sub_status})" if sub_status else ""
            note = item.get("note") or ""
            lines.append(f"[{item['id']}] {item['name']}{sub_status_part}: {note}")
    else:
        lines.append("없음")

    return "\n".join(lines)


def add_project(
    name: str,
    status: str = STATUS_IN_PROGRESS,
    next_steps: str | None = None,
    note: str | None = None,
    sub_status: str | None = None,
) -> dict:
    with memory_lock:
        projects = load_projects()
        item = {
            "id": len(projects) + 1,
            "name": name.strip(),
            "status": status,
            "sub_status": sub_status,
            "next_steps": next_steps,
            "note": note,
            "created_at": now_string(),
            "updated_at": now_string(),
        }
        projects.append(item)
        save_projects(projects)
        return item


def update_project(
    project_id: int,
    status: str | None = None,
    sub_status: str | None = None,
    next_steps: str | None = None,
    note: str | None = None,
) -> bool:
    """번호로 프로젝트를 찾아 전달된 필드만 갱신합니다."""
    with memory_lock:
        projects = load_projects()
        for item in projects:
            if item.get("id") != project_id:
                continue
            if status is not None:
                item["status"] = status
                # 다시 진행중으로 바뀌면 중단 사유 태그는 의미가 없어집니다.
                if status == STATUS_IN_PROGRESS:
                    item["sub_status"] = None
            if sub_status is not None:
                item["sub_status"] = sub_status
            if next_steps is not None:
                item["next_steps"] = next_steps
            if note is not None:
                item["note"] = note
            item["updated_at"] = now_string()
            save_projects(projects)
            return True
        return False
