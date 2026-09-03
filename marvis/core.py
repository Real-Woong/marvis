"""모든 입력이 지나가는 단 하나의 처리 경로입니다.

예전에는 텔레그램(handlers.py)과 Siri 웹훅(webhook.py)이 같은 흐름을 각자
구현하고 있었고 이미 서로 어긋나 있었습니다(웹훅에는 프로젝트 명령도
`!스케쥴`도 없었습니다). 어댑터는 이제 전송 방법만 알고, 무엇을 할지는 전부
여기서 결정합니다.

Phase 1에서 라우터가 둘이 됐습니다. MARVIS_ROUTER가 어느 쪽이 사용자에게
응답할지 정합니다.

    regex   정규식만. Phase 0까지의 동작.
    shadow  정규식이 응답하고, LLM은 나란히 돌며 로그에만 남는다.
            두 판단이 다르면 router.disagreed 이벤트가 찍힌다.
    llm     LLM 라우터가 응답한다.

shadow가 기본값입니다. 배포해도 사용자 경험이 그대로이고, 그 기간에 쌓이는
불일치 기록이 eval 골든셋의 재료가 됩니다.
"""

import logging
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass

from .agent import run_turn
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
    get_project,
    update_project,
)
from .schedule_parser import detect_message_intent
from .secretary import WriteBackError
from .settings import ROUTER_MODE

ROUTER_REGEX = "regex"
ROUTER_SHADOW = "shadow"
ROUTER_LLM = "llm"


def router_mode() -> str:
    """현재 라우터 모드. 호출 시점에 읽으므로 재시작 없이 바꿀 수 있습니다."""
    return (os.getenv("MARVIS_ROUTER") or ROUTER_MODE).strip().lower()

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
    try:
        if action == ACTION_STOP:
            update_project(seq, status=STATUS_STOPPED, source=source)
            return Reply(f"'{project['name']}' 프로젝트를 중단 상태로 변경했습니다.")
        if action == ACTION_PAUSE:
            update_project(seq, status=STATUS_STOPPED, sub_status="일시정지", source=source)
            # SECRETARY가 원본인 프로젝트는 '일시정지'라는 태그가 없어 '멈춤'으로
            # 적힙니다. 요청한 말이 아니라 실제로 남은 값을 알려줍니다.
            current = get_project(seq) or project
            landed = current.get("sub_status") or "중단"
            return Reply(f"'{project['name']}' 프로젝트를 {landed}(으)로 변경했습니다.")
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
    except WriteBackError as error:
        # 파일이 원본이라, 파일을 못 고쳤으면 아무것도 바뀌지 않았습니다.
        logging.warning("_STATUS.md 반영 실패 (%s): %s", project["name"], error)
        return Reply(
            f"'{project['name']}'의 _STATUS.md를 고치지 못해 아무것도 바꾸지 못했습니다. "
            f"파일을 직접 확인해주세요."
        )

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

    mode = router_mode()
    if mode == ROUTER_LLM:
        yield from _handle_with_llm(text, source)
        return

    if mode == ROUTER_SHADOW:
        # 사용자에게 가는 건 정규식 결과입니다. LLM은 뒤에서 조용히 돌립니다.
        replies = list(_handle_with_regex(text, source))
        _run_shadow_async(text, source, replies)
        yield from replies
        return

    yield from _handle_with_regex(text, source)


def _handle_with_llm(text: str, source: str) -> Iterator[Reply]:
    """LLM 라우터. 도구 호출 결과를 모델이 직접 마무리 문장으로 정리합니다.

    정규식 경로와 달리 "기억했습니다" 뒤에 잡담이 한 번 더 나가지 않습니다.
    저장했다는 사실을 모델이 알고 한 문장으로 답하기 때문입니다.
    """
    archive_past_schedules()
    yield Reply("생각 중입니다...", ack=True)

    from .llm.factory import get_client

    try:
        result = run_turn(get_client(), text, source=source)
    except Exception:
        logging.exception("LLM router failed")
        yield Reply("처리하는 중 오류가 발생했습니다.")
        return

    # 되묻는 중이면 음성까지 보낼 필요는 없습니다.
    yield Reply(result.text, speak=result.clarify is None)


def _shadow_compare(text: str, source: str, regex_replies: list[Reply]) -> None:
    """LLM 판단을 기록하고, 정규식과 다르면 표시해 둡니다."""
    from .llm.factory import get_client

    try:
        result = run_turn(get_client(), text, source=f"{source}:shadow")
    except Exception:
        logging.exception("Shadow router failed")
        return

    regex_route = _classify_regex_route(text)
    llm_tool = result.primary_tool
    agrees = _routes_agree(regex_route, llm_tool)

    try:
        with transaction() as tx:
            log_event(
                tx,
                "router.agreed" if agrees else "router.disagreed",
                entity="turn",
                source=source,
                payload={
                    "text": text,
                    "regex_route": regex_route,
                    "llm_tool": llm_tool,
                    "llm_args": result.tool_calls[0]["args"] if result.tool_calls else None,
                    "llm_reply": result.text,
                    "regex_reply": regex_replies[0].text if regex_replies else None,
                },
            )
    except Exception:
        logging.exception("Failed to log the shadow comparison")


def _run_shadow_async(text: str, source: str, regex_replies: list[Reply]) -> None:
    """사용자 응답을 붙잡아 두지 않도록 별도 스레드에서 비교합니다."""
    threading.Thread(
        target=_shadow_compare, args=(text, source, regex_replies), daemon=True
    ).start()


# 정규식 경로와 LLM 도구를 견줄 수 있게 맞춰 놓은 대응표입니다.
_ROUTE_TO_TOOL = {
    "save": "save_memory",
    "query": "list_schedule",
    "chat": None,
    "command.schedule": "list_schedule",
    "command.projects": "list_projects",
    "project": "update_project",
}


def _classify_regex_route(text: str) -> str:
    """정규식이 이 문장을 어느 경로로 보낼지, 상태를 바꾸지 않고 알아냅니다."""
    if text == "!스케쥴":
        return "command.schedule"
    if text == "!프로젝트":
        return "command.projects"
    if detect_project_action(text):
        return "project"
    return detect_message_intent(text)


def _routes_agree(regex_route: str, llm_tool: str | None) -> bool:
    expected = _ROUTE_TO_TOOL.get(regex_route, "__unknown__")
    if regex_route == "project":
        # 프로젝트 경로는 추가/갱신 어느 쪽이어도 같은 판단으로 봅니다.
        return llm_tool in ("update_project", "add_project")
    if regex_route == "query":
        return llm_tool in ("list_schedule", "list_memories", "list_projects")
    return expected == llm_tool


def _handle_with_regex(text: str, source: str) -> Iterator[Reply]:
    """Phase 0까지의 경로. 전환이 끝나면 이 함수와 schedule_parser를 지웁니다."""
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
