"""사이드 프로젝트 진행 상태를 저장하고 조회/수정합니다.

문장에서 의도를 뽑아내는 부분(_NAME_STOPWORDS 이하)은 Phase 1에서 function
calling으로 대체할 예정이라 이번 단계에서는 그대로 두고, 저장소만 SQLite로
옮겼습니다.
"""

import re

from .db import get_connection, log_event, new_id, next_seq, transaction
from .secretary import (
    WriteBackError,
    append_note,
    sync as sync_secretary_projects,
    write_back,
)
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
ACTION_MUTE_BRIEFING = "mute_briefing"
ACTION_UNMUTE_BRIEFING = "unmute_briefing"
ACTION_NOTE = "note"

# status_path가 채워진 행은 SECRETARY의 _STATUS.md가 원본입니다. 무엇을 고칠 수
# 있는지가 달라지므로 조회 결과에도 실어 보냅니다.
_COLUMNS = (
    "id, seq, name, status, sub_status, next_steps, note, muted_from_briefing,"
    " archived, status_path, created_at, updated_at"
)

# '프로젝트'와 붙어 있는 단어를 이름으로 오인하지 않도록 걸러내는 동작어입니다.
_NAME_STOPWORDS = {
    "시작할거야", "시작해", "시작", "새로", "추가해줘", "추가",
    "중단으로", "중단해줘", "중단할게", "중단",
    "수정해줘", "수정",
    "다음할일은", "다음", "할일은", "할일",
    "잠깐", "잠시", "멈출게", "멈춰", "일시정지",
    "브리핑에서", "브리핑", "빼줘", "제외해줘", "제외",
}


def load_projects() -> list[dict]:
    rows = get_connection().execute(
        f"SELECT {_COLUMNS} FROM projects WHERE archived = 0 ORDER BY seq"
    ).fetchall()
    return [dict(row) for row in rows]


def get_project(seq: int) -> dict | None:
    row = get_connection().execute(
        f"SELECT {_COLUMNS} FROM projects WHERE seq = ? AND archived = 0", (seq,)
    ).fetchone()
    return dict(row) if row else None


def get_active_projects() -> list[dict]:
    return [item for item in load_projects() if item["status"] == STATUS_IN_PROGRESS]


def get_stopped_projects() -> list[dict]:
    return [item for item in load_projects() if item["status"] == STATUS_STOPPED]


def get_briefing_projects() -> list[dict]:
    """아침 브리핑에 읽어줄 진행중 프로젝트만 반환합니다(음소거된 프로젝트는 제외)."""
    return [item for item in get_active_projects() if not item.get("muted_from_briefing")]


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


def find_projects_by_name(name: str) -> list[dict]:
    """이름으로 후보를 전부 반환합니다.

    find_project_by_name은 후보가 여럿이면 None을 주고 정보를 버렸습니다.
    LLM 라우터는 후보 목록을 받아 clarify로 되물을 수 있어야 합니다.
    """
    normalized = name.strip().lower()
    projects = load_projects()

    exact = [item for item in projects if item["name"].strip().lower() == normalized]
    if exact:
        return exact

    return [
        item
        for item in projects
        if normalized in item["name"].strip().lower()
        or item["name"].strip().lower() in normalized
    ]


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

    if "브리핑" in normalized:
        unmute_words = ("다시 넣어", "다시 포함", "포함해줘", "다시 알려", "다시 보내")
        mute_words = ("빼줘", "빼줄", "제외", "안 나오게", "그만 보내", "안 보내")
        if any(w in normalized for w in unmute_words):
            return ACTION_UNMUTE_BRIEFING
        if any(w in normalized for w in mute_words):
            return ACTION_MUTE_BRIEFING

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


# '<프로젝트> 아이디어 <내용>' 형태. 이름이 실제 프로젝트로 풀릴 때만 인정하므로
# "좋은 아이디어가 있어" 같은 평범한 문장은 여기 걸리지 않습니다. 이 경로는
# '프로젝트'라는 낱말을 요구하지 않습니다. 갑자기 떠올라 던지는 말이라
# 격식을 요구하면 쓰지 않게 됩니다.
_NOTE_PATTERN = re.compile(
    r"^(?P<name>[^\n]+?)\s*(?:프로젝트\s*)?(?:아이디어|메모)\s*[:：]?\s*(?P<content>.+)$",
    re.S,
)


def parse_project_note(text: str) -> tuple[list[dict], str] | None:
    """텔레그램 메모 의도라면 (프로젝트 후보들, 내용)을 돌려줍니다."""
    match = _NOTE_PATTERN.match(text.strip())
    if not match:
        return None

    name = match.group("name").strip(" :·-")
    content = " ".join(match.group("content").split())
    if not name or not content:
        return None

    matches = find_projects_by_name(name)
    return (matches, content) if matches else None


def add_project_note(seq: int, text: str, source: str = "telegram") -> str:
    """프로젝트의 _STATUS.md에 텔레그램 메모 한 줄을 덧붙입니다."""
    row = get_connection().execute(
        "SELECT id, status_path FROM projects WHERE seq = ? AND archived = 0", (seq,)
    ).fetchone()
    if row is None:
        raise WriteBackError(f"{seq}번 프로젝트를 찾지 못했습니다.")
    if not row["status_path"]:
        raise WriteBackError("손으로 등록한 프로젝트라 적어 둘 _STATUS.md가 없습니다.")

    entry = append_note(row["status_path"], text)
    with transaction() as tx:
        log_event(
            tx, "project.note_added", entity="project", entity_id=row["id"],
            source=source, payload={"seq": seq, "entry": entry},
        )
    return entry


def extract_next_steps_content(text: str) -> str | None:
    """'다음할일은 ~이야' 형태에서 실제 다음 할 일 내용만 뽑아냅니다."""
    match = re.search(r"다음\s*할\s*일은\s*(.+)", text)
    if not match:
        return None
    content = match.group(1).strip()
    content = re.sub(r"(이야|이에요|예요|야)[.!]?$", "", content).strip()
    return content or None


def to_speech_friendly_name(name: str) -> str:
    """BubbleBreak처럼 붙여 쓴 영문 이름에 띄어쓰기를 넣어 Siri가 자연스럽게 읽게 합니다."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


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

    active = [item for item in projects if item["status"] == STATUS_IN_PROGRESS]
    stopped = [item for item in projects if item["status"] == STATUS_STOPPED]

    lines = [f"진행중 ({len(active)}개)"]
    if active:
        for item in active:
            next_steps = item.get("next_steps") or "다음 할 일 미정"
            lines.append(f"[{item['seq']}] {item['name']} - 다음 할 일: {next_steps}")
    else:
        lines.append("없음")

    lines.append("")
    lines.append(f"중단 ({len(stopped)}개)")
    if stopped:
        for item in stopped:
            sub_status = item.get("sub_status")
            sub_status_part = f" ({sub_status})" if sub_status else ""
            note = item.get("note") or ""
            lines.append(f"[{item['seq']}] {item['name']}{sub_status_part}: {note}")
    else:
        lines.append("없음")

    return "\n".join(lines)


def add_project(
    name: str,
    status: str = STATUS_IN_PROGRESS,
    next_steps: str | None = None,
    note: str | None = None,
    sub_status: str | None = None,
    source: str = "telegram",
) -> dict:
    project_id = new_id()
    created_at = now_string()
    clean_name = name.strip()

    with transaction() as tx:
        seq = next_seq(tx, "projects")
        tx.execute(
            "INSERT INTO projects (id, seq, name, status, sub_status, next_steps, note,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, seq, clean_name, status, sub_status, next_steps, note,
             created_at, created_at),
        )
        log_event(
            tx, "project.created", entity="project", entity_id=project_id, source=source,
            payload={"seq": seq, "name": clean_name, "status": status},
        )

    return {
        "id": project_id,
        "seq": seq,
        "name": clean_name,
        "status": status,
        "sub_status": sub_status,
        "next_steps": next_steps,
        "note": note,
        "muted_from_briefing": 0,
        "archived": 0,
        # 손으로 추가한 프로젝트에는 대응하는 _STATUS.md가 없습니다.
        "status_path": None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def update_project(
    seq: int,
    status: str | None = None,
    sub_status: str | None = None,
    next_steps: str | None = None,
    note: str | None = None,
    blockers: list[str] | None = None,
    muted_from_briefing: bool | None = None,
    source: str = "telegram",
) -> bool:
    """번호(seq)로 프로젝트를 찾아 전달된 필드만 갱신합니다.

    SECRETARY가 원본인 프로젝트(status_path가 있는 행)는 DB를 직접 고치지 않고
    _STATUS.md를 고친 뒤 그 파일에서 다시 읽어옵니다. DB만 고치면 다음 동기화가
    파일 내용으로 덮어써서 방금 한 수정이 조용히 사라지기 때문입니다.
    파일을 고치지 못하면 WriteBackError를 올리고 DB는 건드리지 않습니다.

    muted_from_briefing은 SECRETARY가 모르는 Marvis 쪽 취향이라 어느 경우든
    DB에만 남습니다. blockers는 _STATUS.md에만 있는 항목이라 손으로 추가한
    프로젝트에서는 무시됩니다.
    """
    row = get_connection().execute(
        "SELECT id, status_path, sub_status FROM projects WHERE seq = ? AND archived = 0",
        (seq,),
    ).fetchone()
    if row is None:
        return False

    changes: dict = {}
    file_fields = {
        "status": status, "sub_status": sub_status, "next_steps": next_steps,
        "note": note, "blockers": blockers,
    }

    if row["status_path"] and any(value is not None for value in file_fields.values()):
        write_back(
            row["status_path"],
            status=status,
            # '중단'만으로는 관찰중/멈춤/종료 중 무엇인지 알 수 없습니다. 이번에
            # 따로 주지 않았다면 지금 값을 그대로 유지합니다.
            sub_status=sub_status if sub_status is not None else row["sub_status"],
            next_steps=next_steps,
            blockers=blockers,
            note=note,
        )
        # 원본은 파일입니다. 방금 쓴 내용을 파일에서 다시 읽어 DB를 맞춥니다.
        # write_back이 이미 색인을 새로 만들었으므로 render.py를 또 돌리지 않습니다.
        sync_secretary_projects(refresh=False)
        changes = {k: v for k, v in file_fields.items() if v is not None}

    db_fields: dict = {}
    if muted_from_briefing is not None:
        db_fields["muted_from_briefing"] = 1 if muted_from_briefing else 0
        changes["muted_from_briefing"] = bool(muted_from_briefing)

    if not row["status_path"]:
        # 손으로 추가한 프로젝트는 DB가 원본입니다. 예전 동작 그대로입니다.
        if status is not None:
            db_fields["status"] = status
            changes["status"] = status
            # 다시 진행중으로 바뀌면 중단 사유 태그는 의미가 없어집니다.
            if status == STATUS_IN_PROGRESS:
                db_fields["sub_status"] = None
                changes["sub_status"] = None
        if sub_status is not None:
            db_fields["sub_status"] = sub_status
            changes["sub_status"] = sub_status
        if next_steps is not None:
            db_fields["next_steps"] = next_steps
            changes["next_steps"] = next_steps
        if note is not None:
            db_fields["note"] = note
            changes["note"] = note

    if not changes:
        return True

    with transaction() as tx:
        if db_fields:
            assignments = ", ".join(f"{column} = ?" for column in db_fields)
            tx.execute(
                f"UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?",
                (*db_fields.values(), now_string(), row["id"]),
            )
        log_event(
            tx, "project.updated", entity="project", entity_id=row["id"], source=source,
            payload={"seq": seq, "changes": changes},
        )
    return True
