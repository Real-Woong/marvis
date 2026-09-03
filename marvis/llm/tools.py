"""도구 표면. 스키마와 구현을 한 자리에서 한 쌍으로 등록합니다.

둘을 붙여 두는 이유는 어긋날 수 없게 하기 위해서입니다. 프로바이더
어댑터는 여기서 나온 스키마를 자기 형식으로 번역만 합니다.

검증 규칙:
* 스키마 수준(필수 인자, 타입, enum)은 execute가 공통으로 봅니다.
* 의미 수준(날짜가 말이 되는가, 프로젝트가 하나로 특정되는가)은 각
  핸들러가 봅니다.
* 실패는 예외가 아니라 {"error", "message", "hint"} 결과로 돌려줍니다.
  모델이 읽고 고칠 수 있어야 하기 때문입니다.
"""

from datetime import datetime, timedelta
from typing import Any, Callable

from ..memory import (
    counts_summary,
    create_item,
    get_schedules_between,
    mark_done,
    search_memories,
)
from ..projects import (
    STATUS_IN_PROGRESS,
    STATUS_STOPPED,
    STATUSES,
    add_project,
    find_projects_by_name,
    load_projects,
    update_project,
)
from ..secretary import WriteBackError, sync as sync_secretary_projects
from ..settings import KST
from ..time_utils import now_kst, today_kst_date

DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

MEMORY_KINDS = ("schedule", "idea", "note")

# 도구가 한 번에 조회할 수 있는 최대 범위/개수.
MAX_RANGE_DAYS = 90
MAX_LIST_LIMIT = 50


class ToolError(Exception):
    """핸들러가 의미 검증에 실패했을 때 던집니다. execute가 결과로 바꿉니다."""

    def __init__(self, code: str, message: str, hint: str = "", **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra

    def to_result(self) -> dict:
        payload = {"error": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        payload.update(self.extra)
        return payload


class ToolSpec:
    def __init__(self, name: str, description: str, parameters: dict, handler: Callable):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict):
    def decorator(handler: Callable) -> Callable:
        REGISTRY[name] = ToolSpec(name, description, parameters, handler)
        return handler

    return decorator


# ---------------------------------------------------------------- 공통 검증

def _parse_date(value: str, field: str) -> datetime:
    try:
        return datetime.strptime(value, DATE_FORMAT).replace(tzinfo=KST)
    except (ValueError, TypeError):
        raise ToolError(
            "invalid_date",
            f"{field}('{value}')를 YYYY-MM-DD로 해석할 수 없습니다.",
            "절대 날짜를 YYYY-MM-DD 형식으로 계산해서 다시 보내세요.",
        )


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        return datetime.strptime(value, DATETIME_FORMAT).replace(tzinfo=KST)
    except (ValueError, TypeError):
        raise ToolError(
            "invalid_datetime",
            f"{field}('{value}')를 'YYYY-MM-DD HH:MM:SS'로 해석할 수 없습니다.",
            "초까지 포함한 절대 시각으로 다시 보내세요.",
        )


def _validate_against_schema(spec: ToolSpec, args: dict) -> dict:
    """필수 인자, 타입, enum만 봅니다. 의미 검증은 핸들러 몫입니다."""
    schema = spec.parameters
    properties = schema.get("properties", {})
    cleaned: dict[str, Any] = {}

    for key in schema.get("required", []):
        if args.get(key) in (None, ""):
            raise ToolError(
                "missing_argument",
                f"'{key}' 인자가 필요합니다.",
                f"{spec.name} 호출에 {key}를 포함하세요.",
            )

    for key, value in args.items():
        if key not in properties:
            continue  # 모르는 인자는 조용히 버립니다.
        if value is None:
            continue
        prop = properties[key]
        expected = prop.get("type")
        if expected == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ToolError("invalid_type", f"'{key}'는 정수여야 합니다.")
        elif expected == "boolean":
            value = bool(value)
        elif expected == "string":
            value = str(value)
        elif expected == "array" and not isinstance(value, list):
            raise ToolError("invalid_type", f"'{key}'는 배열이어야 합니다.")
        if "enum" in prop and value not in prop["enum"]:
            raise ToolError(
                "invalid_enum",
                f"'{key}'는 {prop['enum']} 중 하나여야 합니다. 받은 값: {value}",
            )
        cleaned[key] = value

    return cleaned


# ---------------------------------------------------------------- 도구 정의

@tool(
    name="save_memory",
    description=(
        "사용자가 기억해 두길 원하는 내용을 저장한다. 일정·할 일이면 kind='schedule'과 "
        "schedule_date를, 특정 시각에 알림이 필요하면 reminder_at까지 넣는다. "
        "아이디어면 'idea', 그 외 메모는 'note'. 날짜와 시각은 반드시 절대값으로 계산해서 넣는다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "저장할 내용. 사용자의 표현을 살린다."},
            "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
            "schedule_date": {"type": "string", "description": "YYYY-MM-DD. 일정일이 있을 때만."},
            "reminder_at": {
                "type": "string",
                "description": "YYYY-MM-DD HH:MM:SS. 먼저 알려줘야 할 때만.",
            },
        },
        "required": ["content", "kind"],
    },
)
def _save_memory(content, kind, schedule_date=None, reminder_at=None, source="telegram", **_):
    now = now_kst()

    if schedule_date:
        _parse_date(schedule_date, "schedule_date")

    if reminder_at:
        reminder = _parse_datetime(reminder_at, "reminder_at")
        if reminder < now - timedelta(minutes=1):
            raise ToolError(
                "reminder_at_in_past",
                f"알림 시각 {reminder_at}은 현재({now.strftime(DATETIME_FORMAT)})보다 과거입니다.",
                "사용자가 과거를 의도한 게 아니라면 날짜를 다시 계산하세요.",
            )
        if schedule_date and reminder.strftime(DATE_FORMAT) != schedule_date:
            raise ToolError(
                "reminder_outside_schedule_date",
                f"알림 시각({reminder_at})이 일정일({schedule_date})과 다른 날입니다.",
                "둘을 같은 날로 맞추거나, 알림이 필요 없으면 reminder_at을 빼세요.",
            )
        if not schedule_date:
            schedule_date = reminder.strftime(DATE_FORMAT)

    if kind != "schedule" and (schedule_date or reminder_at):
        kind = "schedule"

    item = create_item(
        content=content,
        kind=kind,
        schedule_date=schedule_date,
        reminder_at=reminder_at,
        source=source,
    )
    return {
        "saved": True,
        "seq": item["seq"],
        "kind": item["type"],
        "schedule_date": item["schedule_date"],
        "reminder_at": item["reminder_at"],
    }


@tool(
    name="list_schedule",
    description="저장된 미완료 일정을 조회한다. 기간을 주지 않으면 남아 있는 전부를 본다.",
    parameters={
        "type": "object",
        "properties": {
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
        },
    },
)
def _list_schedule(date_from=None, date_to=None, **_):
    if date_from and date_to:
        start = _parse_date(date_from, "date_from")
        end = _parse_date(date_to, "date_to")
        if end < start:
            raise ToolError("invalid_range", "date_to가 date_from보다 앞섭니다.")
        if (end - start).days > MAX_RANGE_DAYS:
            raise ToolError(
                "range_too_wide",
                f"조회 범위는 {MAX_RANGE_DAYS}일을 넘을 수 없습니다.",
                "기간을 좁혀서 다시 부르세요.",
            )
    elif date_from:
        _parse_date(date_from, "date_from")
    elif date_to:
        _parse_date(date_to, "date_to")

    items = get_schedules_between(date_from, date_to)
    return {
        "count": len(items),
        "items": [
            {
                "seq": i["seq"],
                "content": i["content"],
                "schedule_date": i["schedule_date"],
                "reminder_at": i["reminder_at"],
            }
            for i in items
        ],
    }


@tool(
    name="list_memories",
    description="저장된 기억·아이디어·메모를 조회한다. 키워드가 있으면 그것으로 좁힌다.",
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
            "query": {"type": "string", "description": "내용에 포함된 키워드"},
            "limit": {"type": "integer", "description": f"기본 20, 최대 {MAX_LIST_LIMIT}"},
        },
    },
)
def _list_memories(kind=None, query=None, limit=20, **_):
    if limit is not None and int(limit) > MAX_LIST_LIMIT:
        raise ToolError(
            "limit_too_large",
            f"limit은 {MAX_LIST_LIMIT} 이하여야 합니다.",
            "더 좁은 조건으로 다시 부르세요.",
        )
    items = search_memories(kind=kind, query=query, limit=limit or 20)
    return {
        "count": len(items),
        "items": [
            {
                "seq": i["seq"],
                "kind": i["type"],
                "content": i["content"],
                "schedule_date": i["schedule_date"],
                "done": bool(i["done"]),
            }
            for i in items
        ],
    }


@tool(
    name="complete_item",
    description="번호로 항목을 완료 처리한다. 번호를 모르면 먼저 조회한다.",
    parameters={
        "type": "object",
        "properties": {"seq": {"type": "integer", "description": "목록에 보이는 번호"}},
        "required": ["seq"],
    },
)
def _complete_item(seq, **_):
    if not mark_done(int(seq)):
        raise ToolError(
            "item_not_found",
            f"{seq}번 항목을 찾지 못했습니다.",
            "list_schedule이나 list_memories로 번호를 확인하세요.",
        )
    return {"completed": True, "seq": int(seq)}


@tool(
    name="list_projects",
    description="사이드 프로젝트 현황을 조회한다. 진행중/중단과 다음 할 일을 함께 준다.",
    parameters={
        "type": "object",
        "properties": {"status": {"type": "string", "enum": list(STATUSES)}},
    },
)
def _list_projects(status=None, **_):
    sync_secretary_projects()
    items = load_projects()
    if status:
        items = [i for i in items if i["status"] == status]
    return {
        "count": len(items),
        "projects": [
            {
                "name": i["name"],
                "status": i["status"],
                "sub_status": i["sub_status"],
                "next_steps": i["next_steps"],
                "muted_from_briefing": bool(i["muted_from_briefing"]),
            }
            for i in items
        ],
    }


@tool(
    name="add_project",
    description="새 사이드 프로젝트를 진행중 상태로 등록한다.",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)
def _add_project(name, source="telegram", **_):
    existing = find_projects_by_name(name)
    exact = [p for p in existing if p["name"].strip().lower() == name.strip().lower()]
    if exact:
        raise ToolError(
            "project_exists",
            f"'{exact[0]['name']}' 프로젝트가 이미 있습니다.",
            "새로 만들지 말고 update_project로 갱신하거나, 사용자에게 확인하세요.",
        )
    created = add_project(name, status=STATUS_IN_PROGRESS, source=source)
    return {"added": True, "name": created["name"]}


@tool(
    name="update_project",
    description=(
        "프로젝트의 상태나 다음 할 일을 갱신한다. 이름이 여러 프로젝트에 걸리면 "
        "오류가 돌아오므로, 그때는 clarify로 사용자에게 물어본다. "
        "대부분의 프로젝트는 SECRETARY의 _STATUS.md가 원본이라 이 도구가 그 파일을 "
        "직접 고친다. 되돌리기 어려우니 사용자가 실제로 요청한 것만 담아 부른다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "프로젝트 이름(부분 일치 가능)"},
            "status": {"type": "string", "enum": list(STATUSES)},
            "sub_status": {"type": "string", "description": "중단 사유 태그. 예: 일시정지"},
            "next_steps": {"type": "string", "description": "다음 할 일 한 줄. 기존 목록을 대체한다."},
            "note": {"type": "string", "description": "한 줄 요약"},
            "blockers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "막고 있는 것 목록. 기존 목록을 대체하므로, 다 풀렸으면 빈 배열을 준다. "
                    "브리핑은 막힌 게 있으면 다음 할 일 대신 그것부터 읽는다."
                ),
            },
            "muted_from_briefing": {"type": "boolean", "description": "아침 브리핑 제외 여부"},
        },
        "required": ["name"],
    },
)
def _update_project(
    name, status=None, sub_status=None, next_steps=None, note=None, blockers=None,
    muted_from_briefing=None, source="telegram", **_,
):
    matches = find_projects_by_name(name)
    if not matches:
        known = [p["name"] for p in load_projects()]
        raise ToolError(
            "project_not_found",
            f"'{name}'과 일치하는 프로젝트가 없습니다.",
            "아래 목록에서 고르도록 clarify로 물어보세요.",
            candidates=known[:10],
        )
    if len(matches) > 1:
        raise ToolError(
            "project_ambiguous",
            f"'{name}'에 {len(matches)}개가 걸립니다.",
            "clarify로 어느 쪽인지 물어보세요.",
            candidates=[p["name"] for p in matches],
        )

    project = matches[0]
    if status == STATUS_STOPPED and sub_status is None:
        sub_status = None  # 태그 없이 중단만 표시하는 것도 허용합니다.

    if blockers is not None and not project["status_path"]:
        raise ToolError(
            "not_a_status_file_project",
            f"'{project['name']}'은 손으로 등록한 프로젝트라 막힌 것을 적을 곳이 없습니다.",
            "blockers 없이 next_steps나 note로 다시 시도하세요.",
        )

    try:
        update_project(
            project["seq"],
            status=status,
            sub_status=sub_status,
            next_steps=next_steps,
            note=note,
            blockers=blockers,
            muted_from_briefing=muted_from_briefing,
            source=source,
        )
    except WriteBackError as error:
        # 파일을 못 고쳤으면 DB도 안 바뀌었습니다. 저장된 척하면 안 됩니다.
        raise ToolError(
            "status_file_write_failed",
            f"'{project['name']}'의 _STATUS.md를 고치지 못해 아무것도 저장하지 않았습니다.",
            "사용자에게 그대로 알리고, 파일을 직접 확인하도록 안내하세요.",
            detail=str(error),
        ) from error

    # 파일에서 다시 읽힌 값을 돌려줍니다. 우리가 보낸 값이 아니라 실제로 남은 값입니다.
    saved = find_projects_by_name(project["name"])
    current = saved[0] if saved else project
    return {
        "updated": True,
        "name": current["name"],
        "status": current["status"],
        "sub_status": current["sub_status"],
        "next_steps": current["next_steps"],
    }


@tool(
    name="clarify",
    description=(
        "요청이 모호해서 추측하면 틀릴 수 있을 때 사용자에게 되묻는다. "
        "이 도구를 부르면 그 질문이 그대로 사용자에게 가고 턴이 끝난다. "
        "추측해서 잘못 저장하느니 묻는 편이 낫다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "사용자에게 보낼 질문"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "고를 수 있는 후보. 최대 5개.",
            },
        },
        "required": ["question"],
    },
)
def _clarify(question, options=None, **_):
    if options and len(options) > 5:
        options = options[:5]
    return {"clarify": True, "question": question, "options": options or []}


# ---------------------------------------------------------------- 실행

TERMINAL_TOOLS = {"clarify"}


def schemas() -> list[dict]:
    """어댑터에 넘길 도구 스키마 목록."""
    return [spec.schema() for spec in REGISTRY.values()]


def execute(name: str, args: dict, source: str = "telegram") -> dict:
    """도구를 검증하고 실행합니다. 실패도 dict로 돌려줍니다(예외 아님)."""
    spec = REGISTRY.get(name)
    if spec is None:
        return {
            "error": "unknown_tool",
            "message": f"'{name}'이라는 도구는 없습니다.",
            "hint": f"사용 가능한 도구: {', '.join(REGISTRY)}",
        }
    try:
        cleaned = _validate_against_schema(spec, args or {})
        return spec.handler(source=source, **cleaned)
    except ToolError as error:
        return error.to_result()
    except Exception as error:  # 핸들러 버그가 턴 전체를 죽이지 않게 합니다.
        return {
            "error": "tool_failed",
            "message": f"{name} 실행 중 오류: {error}",
            "hint": "다른 방식으로 시도하거나 clarify로 사용자에게 물어보세요.",
        }


def context_summary() -> dict:
    """시스템 프롬프트에 넣을 요약. 항목 내용은 담지 않습니다."""
    counts = counts_summary()
    return {
        "today": today_kst_date().isoformat(),
        "weekday": ["월", "화", "수", "목", "금", "토", "일"][now_kst().weekday()],
        "now": now_kst().strftime(DATETIME_FORMAT),
        "open_schedules": counts["open_schedules"],
        "ideas": counts["ideas"],
        "project_names": [p["name"] for p in load_projects()],
    }
