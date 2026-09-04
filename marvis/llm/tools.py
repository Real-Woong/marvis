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
    create_recurrence,
    delete_item,
    delete_recurrence,
    format_weekdays,
    get_item,
    get_schedules_between,
    list_recurrences,
    mark_done,
    parse_weekdays,
    search_memories,
    update_recurrence,
)
from ..projects import (
    STATUS_IN_PROGRESS,
    STATUS_STOPPED,
    STATUSES,
    add_project,
    add_project_note,
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
    def __init__(
        self, name: str, description: str, parameters: dict, handler: Callable,
        writes: bool = False,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler
        # 이 도구가 상태를 바꾸는가. shadow(관찰 전용) 실행에서 걸러낼 기준입니다.
        self.writes = writes

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict, writes: bool = False):
    def decorator(handler: Callable) -> Callable:
        REGISTRY[name] = ToolSpec(name, description, parameters, handler, writes=writes)
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
        "아이디어면 'idea', 그 외 메모는 'note'. 날짜와 시각은 반드시 절대값으로 계산해서 넣는다. "
        "반복되는 일정(매일·평일·매주)은 이 도구로 여러 건 펼쳐 넣지 말고 "
        "save_recurring_schedule을 쓴다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "저장할 내용. 경로·명령어·URL·코드·식별자는 사용자가 쓴 그대로, "
                    "한 글자도 바꾸거나 줄이지 말고 옮긴다. "
                    "'~/toss-api/apps/toss-ai-agent'를 '~/toss-api/app'으로 줄이면 "
                    "그 알림은 쓸모가 없어진다. 줄일 것은 설명문이지 리터럴이 아니다. "
                    "경로와 명령을 한 줄로 이어 붙이지도 않는다 — 실행할 수 없는 문자열이 된다."
                ),
            },
            "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
            "schedule_date": {"type": "string", "description": "YYYY-MM-DD. 일정일이 있을 때만."},
            "reminder_at": {
                "type": "string",
                "description": "YYYY-MM-DD HH:MM:SS. 먼저 알려줘야 할 때만.",
            },
        },
        "required": ["content", "kind"],
    },
    writes=True,
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
    description=(
        "번호로 항목을 '완료'로 표시한다. 번호를 모르면 먼저 조회한다. "
        "이것은 삭제가 아니다 — 항목은 저장소에 그대로 남고 완료 표시만 붙는다. "
        "사용자가 지워 달라고 했으면 delete_item을 쓴다. 이 도구를 부르고 "
        "'삭제했습니다'라고 답하면 안 된다."
    ),
    parameters={
        "type": "object",
        "properties": {"seq": {"type": "integer", "description": "목록에 보이는 번호"}},
        "required": ["seq"],
    },
    writes=True,
)
def _complete_item(seq, source="telegram", **_):
    if not mark_done(int(seq), source=source):
        raise ToolError(
            "item_not_found",
            f"{seq}번 항목을 찾지 못했습니다.",
            "list_schedule이나 list_memories로 번호를 확인하세요.",
        )
    # 쓰기 뒤에 다시 읽습니다. 모델에게도 "완료 표시일 뿐 남아 있다"는 사실이
    # 결과로 보여야, 답변에서 삭제라고 부르지 않습니다.
    after = get_item(int(seq))
    if after is None or not after["done"]:
        raise ToolError(
            "complete_not_verified",
            f"{seq}번을 완료 처리했으나 반영을 확인하지 못했습니다.",
            "사용자에게 '시도했으나 확인하지 못했다'고 그대로 알리세요.",
        )
    return {
        "completed": True, "verified": True, "seq": int(seq),
        "still_stored": True,
        "note": "완료 표시만 붙었고 항목은 저장소에 남아 있습니다. 삭제가 아닙니다.",
    }


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
    writes=True,
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
            "next_steps": {
                "type": "string",
                "description": (
                    "다음에 할 일 한 줄. 기존 목록 맨 앞에 놓는다. 나머지 항목은 그대로 남는다. "
                    "빈 문자열을 주면 목록을 비운다."
                ),
            },
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
    writes=True,
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
    name="add_project_note",
    description=(
        "사용자가 갑자기 떠올린 착상을 그 프로젝트의 _STATUS.md 안 "
        "'[텔레그램 전송]' 목록에 덧붙인다. 덧붙이기만 하고 아무것도 지우지 않는다. "
        "아직 다듬어지지 않은 생각일 때 쓴다. 하기로 정해진 다음 할 일이면 "
        "update_project 의 next_steps 를 쓴다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "프로젝트 이름(부분 일치 가능)"},
            "note": {"type": "string", "description": "적어 둘 내용. 사용자의 말을 그대로 옮긴다."},
        },
        "required": ["name", "note"],
    },
    writes=True,
)
def _add_project_note(name, note, source="telegram", **_):
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
    try:
        entry = add_project_note(project["seq"], note, source=source)
    except WriteBackError as error:
        raise ToolError(
            "status_file_write_failed",
            f"'{project['name']}'의 _STATUS.md에 적지 못했습니다.",
            "사용자에게 그대로 알리세요. 적히지 않았습니다.",
            detail=str(error),
        ) from error

    # 실제로 파일에 적힌 줄을 그대로 돌려줍니다.
    return {"added": True, "name": project["name"], "entry": entry}



@tool(
    name="delete_item",
    description=(
        "번호로 항목을 목록에서 지운다. 사용자가 '지워줘/삭제해줘'라고 했을 때 쓴다. "
        "complete_item(완료 처리)과 혼동하지 마라 — 완료 처리한 항목은 여전히 저장소에 "
        "남아 있으므로, 완료 처리해 놓고 '삭제했습니다'라고 답하면 거짓말이 된다. "
        "결과의 verified가 true일 때만 지웠다고 말한다."
    ),
    parameters={
        "type": "object",
        "properties": {"seq": {"type": "integer", "description": "목록에 보이는 번호"}},
        "required": ["seq"],
    },
    writes=True,
)
def _delete_item(seq, **_):
    result = delete_item(int(seq))
    if not result["deleted"]:
        raise ToolError(
            "item_not_found",
            f"{seq}번 항목을 찾지 못했습니다. 이미 지웠거나 번호가 다릅니다.",
            "list_schedule이나 list_memories로 번호를 확인하세요.",
        )
    if not result["verified"]:
        # 쓰기는 했는데 다시 읽어 확인하지 못했습니다. 됐다고 말하면 안 됩니다.
        raise ToolError(
            "delete_not_verified",
            f"{seq}번을 지우려 했으나 반영을 확인하지 못했습니다.",
            "사용자에게 '시도했으나 확인하지 못했다'고 그대로 알리세요.",
        )
    return {"deleted": True, "verified": True, "seq": result["seq"],
            "content": result["content"]}


@tool(
    name="save_recurring_schedule",
    description=(
        "반복되는 알림을 규칙 한 건으로 저장한다. '평일 아침 6시', '매주 월수금 20시' "
        "처럼 되풀이되는 것은 전부 이 도구를 쓴다. 단발 여러 건으로 펼쳐서 "
        "save_memory를 반복 호출하지 마라 — 나중에 시각 하나를 바꾸려면 펼쳐 놓은 "
        "것을 전부 찾아 고쳐야 하고, 사용자가 말한 규칙 자체가 사라진다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "알림에 실을 내용. 경로·명령어·URL은 사용자가 쓴 그대로 옮긴다."
                ),
            },
            "weekdays": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "울릴 요일. 월=0, 화=1 … 일=6. 평일이면 [0,1,2,3,4].",
            },
            "at_time": {"type": "string", "description": "HH:MM (24시간제)"},
            "starts_on": {"type": "string", "description": "YYYY-MM-DD. 첫 발송 가능일."},
            "ends_on": {
                "type": "string",
                "description": "YYYY-MM-DD. 마지막 발송일. 끝이 없으면 넣지 않는다.",
            },
        },
        "required": ["content", "weekdays", "at_time", "starts_on"],
    },
    writes=True,
)
def _save_recurring_schedule(
    content, weekdays, at_time, starts_on, ends_on=None, source="telegram", **_
):
    _parse_date(starts_on, "starts_on")
    if ends_on:
        if _parse_date(ends_on, "ends_on") < _parse_date(starts_on, "starts_on"):
            raise ToolError(
                "invalid_range",
                f"종료일({ends_on})이 시작일({starts_on})보다 앞섭니다.",
                "두 날짜를 다시 계산하세요.",
            )
    try:
        days = parse_weekdays(weekdays)
    except (ValueError, TypeError) as error:
        raise ToolError(
            "invalid_weekdays", str(error), "월=0 … 일=6 정수 배열로 보내세요."
        ) from error
    try:
        hour, _, minute = at_time.partition(":")
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise ToolError(
            "invalid_time", f"at_time('{at_time}')을 HH:MM으로 해석할 수 없습니다.",
            "24시간제 HH:MM으로 보내세요. 예: 06:10",
        )

    rule = create_recurrence(
        content=content, weekdays=days, at_time=at_time, starts_on=starts_on,
        ends_on=ends_on, source=source,
    )
    return {
        "saved": True, "recurrence_seq": rule["seq"],
        "weekdays": format_weekdays(rule["weekdays"]), "at_time": rule["at_time"],
        "starts_on": rule["starts_on"], "ends_on": rule["ends_on"],
    }


@tool(
    name="list_recurring_schedules",
    description=(
        "저장된 반복 규칙을 조회한다. 사용자가 반복 알림을 묻거나 고쳐 달라고 하면 "
        "먼저 이걸 불러서 실제로 무엇이 저장돼 있는지 확인한다. 기억으로 답하지 마라."
    ),
    parameters={"type": "object", "properties": {}},
)
def _list_recurring_schedules(**_):
    rules = list_recurrences()
    return {
        "count": len(rules),
        "recurrences": [
            {
                "recurrence_seq": rule["seq"],
                "content": rule["content"],
                "weekdays": format_weekdays(rule["weekdays"]),
                "weekday_numbers": rule["weekdays"],
                "at_time": rule["at_time"],
                "starts_on": rule["starts_on"],
                "ends_on": rule["ends_on"],
                "timezone": rule["timezone"],
                "last_fired_on": rule["last_fired_on"],
            }
            for rule in rules
        ],
    }


@tool(
    name="update_recurring_schedule",
    description=(
        "반복 규칙의 시각·요일·기간·내용을 고친다. 번호는 list_recurring_schedules의 "
        "recurrence_seq다. 바꾸려는 필드만 넣는다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recurrence_seq": {"type": "integer"},
            "content": {"type": "string"},
            "weekdays": {"type": "array", "items": {"type": "integer"}},
            "at_time": {"type": "string", "description": "HH:MM"},
            "starts_on": {"type": "string", "description": "YYYY-MM-DD"},
            "ends_on": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["recurrence_seq"],
    },
    writes=True,
)
def _update_recurring_schedule(recurrence_seq, source="telegram", **fields):
    fields.pop("source", None)
    result = update_recurrence(int(recurrence_seq), source=source, **fields)
    if not result["updated"]:
        raise ToolError(
            "recurrence_not_found",
            f"반복 규칙 R{recurrence_seq}을 찾지 못했습니다.",
            "list_recurring_schedules로 번호를 확인하세요.",
        )
    if not result["verified"]:
        raise ToolError(
            "update_not_verified",
            f"R{recurrence_seq}을 고치려 했으나 반영을 확인하지 못했습니다.",
            "사용자에게 '시도했으나 확인하지 못했다'고 그대로 알리세요.",
        )
    record = result["record"]
    return {
        "updated": True, "verified": True, "recurrence_seq": record["seq"],
        "content": record["content"],
        "weekdays": format_weekdays(record["weekdays"]),
        "at_time": record["at_time"], "starts_on": record["starts_on"],
        "ends_on": record["ends_on"],
    }


@tool(
    name="delete_recurring_schedule",
    description="반복 규칙을 지운다. 번호는 list_recurring_schedules의 recurrence_seq다.",
    parameters={
        "type": "object",
        "properties": {"recurrence_seq": {"type": "integer"}},
        "required": ["recurrence_seq"],
    },
    writes=True,
)
def _delete_recurring_schedule(recurrence_seq, source="telegram", **_):
    result = delete_recurrence(int(recurrence_seq), source=source)
    if not result["deleted"]:
        raise ToolError(
            "recurrence_not_found",
            f"반복 규칙 R{recurrence_seq}을 찾지 못했습니다.",
            "list_recurring_schedules로 번호를 확인하세요.",
        )
    if not result["verified"]:
        raise ToolError(
            "delete_not_verified",
            f"R{recurrence_seq}을 지우려 했으나 반영을 확인하지 못했습니다.",
            "사용자에게 '시도했으나 확인하지 못했다'고 그대로 알리세요.",
        )
    return {"deleted": True, "verified": True, "recurrence_seq": result["seq"],
            "content": result["content"]}


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


def execute(
    name: str, args: dict, source: str = "telegram", dry_run: bool = False
) -> dict:
    """도구를 검증하고 실행합니다. 실패도 dict로 돌려줍니다(예외 아님).

    dry_run=True 면 쓰기 도구는 인자 검증까지만 하고 실행하지 않습니다.
    검증은 그대로 도는 것이 중요합니다 — shadow 로그에 "이 인자로는 어차피
    실패했을 것"이라는 정보까지 남아야 골든셋으로 쓸 수 있습니다.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return {
            "error": "unknown_tool",
            "message": f"'{name}'이라는 도구는 없습니다.",
            "hint": f"사용 가능한 도구: {', '.join(REGISTRY)}",
        }
    try:
        cleaned = _validate_against_schema(spec, args or {})
        if dry_run and spec.writes:
            return {"dry_run": True, "would_call": name, "args": cleaned}
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
        "recurrences": len(list_recurrences()),
        "project_names": [p["name"] for p in load_projects()],
    }
