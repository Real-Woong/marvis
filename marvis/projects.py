"""사이드 프로젝트 진행 상태를 저장하고 조회/수정합니다."""

from .settings import PROJECTS_FILE
from .storage import load_json_file, memory_lock, save_json_file
from .time_utils import now_string

STATUS_IN_PROGRESS = "진행중"
STATUS_STOPPED = "중단"
STATUSES = (STATUS_IN_PROGRESS, STATUS_STOPPED)

# 중단 상태를 더 구체적으로 설명할 때 붙일 수 있는 태그입니다.
SUB_STATUS_OPTIONS = ("test중", "중단", "log data 수집중", "일시정지")


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
