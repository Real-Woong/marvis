"""모든 입력이 지나가는 단 하나의 처리 경로입니다.

예전에는 텔레그램(handlers.py)과 Siri 웹훅(webhook.py)이 같은 흐름을 각자
구현하고 있었고 이미 서로 어긋나 있었습니다(웹훅에는 프로젝트 명령도
`!스케쥴`도 없었습니다). 어댑터는 이제 전송 방법만 알고, 무엇을 할지는 전부
여기서 결정합니다.

Phase 0의 범위는 구조 통합입니다. 정규식 라우팅과 "저장 후 답변도 한 번 더"
동작은 의도적으로 그대로 뒀습니다. Phase 1에서 function calling으로 바꿀 때
한꺼번에 걷어냅니다.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from .ai import ask_gemini
from .db import log_event, transaction
from .memory import add_memory, archive_past_schedules, format_schedule_by_date
from .projects import (
    ACTION_ADD,
    ACTION_MUTE_BRIEFING,
    ACTION_NEXT_STEPS,
    ACTION_PAUSE,
    ACTION_STOP,
    ACTION_UNMUTE_BRIEFING,
    STATUS_IN_PROGRESS,
    STATUS_STOPPED,
    add_project,
    detect_project_action,
    extract_next_steps_content,
    extract_project_name,
    find_project_by_name,
    format_all_projects,
    update_project,
)
from .schedule_parser import detect_message_intent

SOURCE_TELEGRAM = "telegram"
SOURCE_SIRI = "siri"

_MEMORY_TYPE_NAMES = {"schedule": "일정", "idea": "아이디어", "note": "메모"}


@dataclass
class Reply:
    """어댑터가 보낼 메시지 하나."""

    text: str
    speak: bool = False   # 텍스트와 함께 음성으로도 보낼지
    ack: bool = False     # "생각 중입니다..." 같은 진행 상황 안내인지


def _handle_project_action(action: str, text: str, source: str) -> Reply:
    """프로젝트 관련 요청을 처리합니다. LLM은 호출하지 않습니다."""
    project_name = extract_project_name(text)
    if not project_name:
        return Reply("어떤 프로젝트인지 이름을 알려주세요.")

    if action == ACTION_ADD:
        add_project(project_name, status=STATUS_IN_PROGRESS, source=source)
        return Reply(f"'{project_name}' 프로젝트를 새로 등록했습니다.")

    project = find_project_by_name(project_name)
    if not project:
        return Reply(f"'{project_name}'과 일치하는 프로젝트를 찾지 못했습니다.")

    seq = project["seq"]
    if action == ACTION_STOP:
        update_project(seq, status=STATUS_STOPPED, source=source)
        return Reply(f"'{project['name']}' 프로젝트를 중단 상태로 변경했습니다.")
    if action == ACTION_PAUSE:
        update_project(seq, status=STATUS_STOPPED, sub_status="일시정지", source=source)
        return Reply(f"'{project['name']}' 프로젝트를 일시정지로 변경했습니다.")
    if action == ACTION_NEXT_STEPS:
        content = extract_next_steps_content(text)
        if not content:
            return Reply("다음 할 일 내용을 인식하지 못했습니다.")
        update_project(seq, next_steps=content, source=source)
        return Reply(f"'{project['name']}'의 다음 할 일을 갱신했습니다: {content}")
    if action == ACTION_MUTE_BRIEFING:
        update_project(seq, muted_from_briefing=True, source=source)
        return Reply(f"'{project['name']}'을(를) 아침 브리핑에서 제외했습니다.")
    if action == ACTION_UNMUTE_BRIEFING:
        update_project(seq, muted_from_briefing=False, source=source)
        return Reply(f"'{project['name']}'을(를) 아침 브리핑에 다시 포함했습니다.")

    return Reply("요청을 이해하지 못했습니다.")


def _log_turn(text: str, source: str, route: str, detail: dict | None = None) -> None:
    """들어온 발화와 그 결과를 이벤트 로그에 남깁니다.

    이 로그가 Phase 1의 eval 골든셋 재료가 됩니다. "이 문장이 들어왔을 때
    실제로 무슨 경로를 탔는가"를 나중에 사람이 검토하며 정답을 붙일 수 있어야
    합니다.
    """
    payload = {"text": text, "route": route}
    if detail:
        payload.update(detail)
    try:
        with transaction() as tx:
            log_event(tx, "turn.handled", entity="turn", source=source, payload=payload)
    except Exception:
        # 로깅 실패가 사용자 응답을 막지 않도록 합니다.
        logging.exception("Failed to log turn")


def handle_message(text: str, source: str = SOURCE_TELEGRAM) -> Iterator[Reply]:
    """사용자 발화 하나를 처리하고 보낼 메시지를 순서대로 내놓습니다.

    제너레이터인 이유는, 진행 상황 안내를 먼저 보내고 나서 (느린) LLM 호출을
    하는 기존 UX를 어댑터가 그대로 재현할 수 있게 하기 위해서입니다.
    """
    text = (text or "").strip()
    if not text:
        return

    archive_past_schedules()

    if text == "!스케쥴":
        _log_turn(text, source, "command.schedule")
        yield Reply(format_schedule_by_date(), speak=True)
        return

    if text == "!프로젝트":
        _log_turn(text, source, "command.projects")
        yield Reply(format_all_projects(), speak=True)
        return

    project_action = detect_project_action(text)
    if project_action:
        reply = _handle_project_action(project_action, text, source)
        _log_turn(text, source, "project", {"action": project_action})
        yield reply
        return

    intent = detect_message_intent(text)

    if intent == "save":
        saved = add_memory(text, source=source)
        _log_turn(text, source, "save", {"item_id": saved["id"], "type": saved["type"]})
        if saved.get("reminder_at"):
            yield Reply(f"기억했습니다. 알림 시각: {saved['reminder_at']}", ack=True)
        else:
            type_name = _MEMORY_TYPE_NAMES.get(saved.get("type"), "기억")
            yield Reply(f"{type_name}으로 기억했습니다.", ack=True)
    elif intent == "query":
        _log_turn(text, source, "query")
        yield Reply("저장된 내용을 확인하고 있습니다...", ack=True)
    else:
        _log_turn(text, source, "chat")
        yield Reply("생각 중입니다...", ack=True)

    try:
        yield Reply(ask_gemini(text), speak=True)
    except Exception:
        logging.exception("Error while generating an answer")
        yield Reply("답변을 생성하는 중 오류가 발생했습니다.")
