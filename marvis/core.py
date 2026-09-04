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
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass

from .agent import run_turn
from .ai import ask_gemini
from .db import log_event, transaction
from .memory import (
    add_memory,
    archive_past_schedules,
    create_recurrence,
    delete_item,
    delete_recurrence,
    format_recurrences_raw,
    format_schedule_by_date,
    format_schedule_raw,
    format_weekdays,
    list_recurrences,
)
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
    add_project_note,
    find_project_by_name,
    format_all_projects,
    get_project,
    parse_project_note,
    update_project,
)
from .schedule_parser import (
    INSTRUCTION_DELETE,
    INSTRUCTION_SHOW_RAW,
    detect_instruction,
    detect_message_intent,
    parse_item_refs,
    parse_recurrence_refs,
    parse_recurrence_request,
)
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


def _handle_project_note(note: tuple, text: str, source: str) -> Reply:
    """갑자기 떠오른 착상을 그 프로젝트의 _STATUS.md 에 적어둡니다."""
    matches, content = note

    if len(matches) > 1:
        names = ", ".join(item["name"] for item in matches[:5])
        _log_turn(text, source, "project.note", {"ambiguous": names})
        return Reply(f"어느 프로젝트인지 하나로 좁혀주세요: {names}")

    project = matches[0]
    try:
        entry = add_project_note(project["seq"], content, source=source)
    except WriteBackError as error:
        logging.warning("_STATUS.md 메모 실패 (%s): %s", project["name"], error)
        _log_turn(text, source, "project.note", {"error": str(error)})
        return Reply(f"'{project['name']}'에 적지 못했습니다. {error}")

    _log_turn(text, source, "project.note", {"seq": project["seq"], "entry": entry})
    # 무엇이 실제로 파일에 적혔는지 그대로 보여줍니다. "적어뒀습니다"라는 말만
    # 돌려주면 안 적혔을 때 알 방법이 없습니다.
    return Reply(f"'{project['name']}' _STATUS.md에 적었습니다.\n{entry}")


def _handle_instruction(instruction: str, text: str, source: str) -> Iterator[Reply]:
    """저장된 것에 대한 지시를 처리합니다. 이 경로는 새 일정을 만들지 않습니다.

    여기서 LLM을 부르지 않는 것이 핵심입니다. ask_gemini에는 도구가 하나도
    없어서 무엇도 지우거나 고칠 수 없는데, 프롬프트에 기억 전문이 들어가 있어
    "[12]와 [13]을 삭제했습니다" 같은 문장을 자연스럽게 만들어 냅니다.
    실제로는 아무 일도 일어나지 않은 채로요.
    """
    if instruction == INSTRUCTION_SHOW_RAW:
        _log_turn(text, source, "instruction.show_raw")
        yield Reply(format_schedule_raw())
        return

    item_refs = parse_item_refs(text)
    recurrence_refs = parse_recurrence_refs(text)

    if instruction == INSTRUCTION_DELETE:
        if not item_refs and not recurrence_refs:
            _log_turn(text, source, "instruction.delete", {"refs": []})
            yield Reply(
                "어느 항목을 지울지 번호로 알려주세요. 예: [12] 지워줘\n"
                "번호는 !원본 으로 확인할 수 있습니다."
            )
            return

        lines = []
        for seq in item_refs:
            result = delete_item(seq)
            if not result["deleted"]:
                lines.append(f"[{seq}] 찾지 못했습니다. 이미 지웠거나 번호가 다릅니다.")
            elif result["verified"]:
                lines.append(f"[{seq}] 지웠습니다: {result['content'].splitlines()[0][:60]}")
            else:
                # 썼는데 다시 읽어 확인하지 못했습니다. 됐다고 말하지 않습니다.
                lines.append(f"[{seq}] 지우려 했으나 반영을 확인하지 못했습니다.")
        for seq in recurrence_refs:
            result = delete_recurrence(seq, source=source)
            if not result["deleted"]:
                lines.append(f"[R{seq}] 반복 규칙을 찾지 못했습니다.")
            elif result["verified"]:
                lines.append(f"[R{seq}] 반복 규칙을 지웠습니다: {result['content'][:60]}")
            else:
                lines.append(f"[R{seq}] 지우려 했으나 반영을 확인하지 못했습니다.")

        _log_turn(text, source, "instruction.delete",
                  {"items": item_refs, "recurrences": recurrence_refs})
        yield Reply("\n".join(lines))
        return

    # 수정 요청. 내용을 통째로 고치는 기능은 아직 없습니다. 없다고 말합니다.
    _log_turn(text, source, "instruction.edit",
              {"items": item_refs, "recurrences": recurrence_refs})
    referenced = ", ".join([f"[{seq}]" for seq in item_refs]
                           + [f"[R{seq}]" for seq in recurrence_refs])
    header = (
        f"{referenced}에 대한 수정 요청으로 읽었습니다. 아무것도 바꾸지 않았습니다.\n\n"
        if referenced else "수정 요청으로 읽었습니다. 아무것도 바꾸지 않았습니다.\n\n"
    )
    yield Reply(
        header
        + "지금 할 수 있는 것:\n"
        "- 지우기: [번호] 지워줘\n"
        "- 완료 표시: /done 번호\n"
        "- 새로 저장: 고친 내용을 한 건으로 다시 보내주세요\n"
        "- 반복 규칙 확인: !반복\n"
        "- 저장된 원본 확인: !원본\n\n"
        "저장된 내용의 본문을 그 자리에서 고치는 기능은 아직 없습니다. "
        "지우고 다시 넣는 쪽이 확실합니다."
    )


def _handle_recurrence_request(recurrence: dict, text: str, source: str) -> Reply:
    """반복 알림 요청을 규칙 한 건으로 저장합니다."""
    content = _recurrence_content(text)
    if not content:
        _log_turn(text, source, "recurrence.unclear", recurrence)
        return Reply(
            f"{format_weekdays(','.join(str(day) for day in recurrence['weekdays']))} "
            f"{recurrence['at_time']} 반복으로 읽었는데, 무엇을 알릴지 못 찾았습니다.\n"
            "알림에 실을 내용을 한 줄로 알려주세요."
        )

    rule = create_recurrence(
        content=content,
        weekdays=recurrence["weekdays"],
        at_time=recurrence["at_time"],
        starts_on=recurrence["starts_on"],
        ends_on=recurrence["ends_on"],
        source=source,
    )
    _log_turn(text, source, "recurrence.created", {"seq": rule["seq"]})

    # 저장한 값이 아니라 저장소에서 다시 읽은 값을 보여줍니다. 둘이 다르면
    # 사용자가 바로 알아챌 수 있어야 합니다.
    saved = [item for item in list_recurrences() if item["seq"] == rule["seq"]]
    if not saved:
        return Reply(
            f"반복 규칙을 저장했으나(R{rule['seq']}) 다시 읽어 확인하지 못했습니다. "
            "!반복 으로 확인해주세요."
        )
    return Reply("반복 규칙으로 저장했습니다.\n\n" + format_recurrences_raw(saved))


# 반복 요청 문장에서 규칙을 나타내는 부분을 걷어내고 남는 것이 알림 내용입니다.
_RECURRENCE_NOISE = re.compile(
    r"(매일|매주|평일|주중|주말|날마다|요일마다|반복(\s*알림)?|정기적으로"
    r"|[월화수목금토일]\s*[~\-]\s*[월화수목금토일]|[월화수목금토일]요일"
    r"|\d{1,2}:\d{2}|\d{1,2}\s*시(\s*\d{1,2}\s*분)?|오전|오후|아침|저녁|밤"
    r"|20\d{2}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[./]\d{1,2}"
    r"|\d{1,2}월\s*\d{1,2}일|부터|까지|에|으로|로|알려줘|알림)"
)


def _recurrence_content(text: str) -> str:
    """반복 요청 문장에서 '무엇을 알릴지'만 남깁니다."""
    stripped = _RECURRENCE_NOISE.sub(" ", text)
    stripped = re.sub(r"[·,\-—:]+", " ", stripped)
    return " ".join(stripped.split()).strip()


def _answer_failure_message(error: Exception) -> str:
    """답변 생성이 실패한 이유를 사용자가 대응할 수 있는 말로 바꿉니다.

    한도 초과와 진짜 고장은 사용자가 할 일이 다릅니다. 전자는 기다리면 되고,
    후자는 로그를 봐야 합니다. 둘 다 "오류가 발생했습니다"로 뭉뚱그리면
    무엇을 해야 할지 알 수 없습니다. 무료 티어는 모델당 하루 20요청이라
    실제로 자주 닿습니다.
    """
    detail = str(error)
    if "RESOURCE_EXHAUSTED" in detail or "429" in detail:
        return (
            "LLM 사용 한도에 걸려 답변을 만들지 못했습니다. "
            "저장과 조회는 그대로 됩니다 — !원본, !반복, !스케쥴 을 쓰세요.\n"
            "한도는 잠시 뒤 풀립니다."
        )
    return "답변을 생성하는 중 오류가 발생했습니다. 저장된 내용은 그대로입니다."


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
    """LLM 판단을 기록하고, 정규식과 다르면 표시해 둡니다.

    `dry_run=True` 가 이 함수의 전부입니다. shadow는 관찰이지 실행이 아닙니다.

    이게 왜 명시적이어야 하는지: 예전에는 이 함수가 run_turn을 그냥 불렀고,
    run_turn은 모델이 요청한 도구를 진짜로 실행했습니다. 문서에는 "로그에만
    남는다"고 적혀 있었지만 실제로는 모든 메시지가 두 번 저장됐습니다 —
    정규식이 원문 한 건, shadow LLM이 분해한 N건. 2026-09-04 하루에만
    shadow가 89건을 썼습니다.
    """
    from .llm.factory import get_client

    try:
        result = run_turn(get_client(), text, source=f"{source}:shadow", dry_run=True)
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
    "command.raw": "list_schedule",
    "command.recurrences": "list_recurring_schedules",
    "project": "update_project",
    "recurrence": "save_recurring_schedule",
    # 정규식이 "저장하지 않고 되묻는다"로 판단한 자리입니다. LLM이 무엇을
    # 했는지가 바로 이 경로의 관찰 대상이라, 고정된 정답을 두지 않습니다.
    "ambiguous": "__unknown__",
}


def _classify_regex_route(text: str) -> str:
    """정규식이 이 문장을 어느 경로로 보낼지, 상태를 바꾸지 않고 알아냅니다.

    _handle_with_regex 의 분기 순서와 같아야 합니다. 어긋나면 shadow 비교가
    실제로 일어난 일이 아니라 다른 것을 견주게 됩니다.
    """
    if text == "!스케쥴":
        return "command.schedule"
    if text == "!프로젝트":
        return "command.projects"
    if text in ("!원본", "!raw"):
        return "command.raw"
    if text in ("!반복", "!recur"):
        return "command.recurrences"
    if parse_project_note(text):
        return "project.note"
    if detect_project_action(text):
        return "project"
    instruction = detect_instruction(text)
    if instruction:
        return f"instruction.{instruction}"
    if parse_recurrence_request(text):
        return "recurrence"
    return detect_message_intent(text)


def _routes_agree(regex_route: str, llm_tool: str | None) -> bool:
    expected = _ROUTE_TO_TOOL.get(regex_route, "__unknown__")
    if regex_route == "instruction.delete":
        return llm_tool in ("delete_item", "delete_recurring_schedule")
    if regex_route == "instruction.show_raw":
        return llm_tool in ("list_schedule", "list_memories",
                            "list_recurring_schedules")
    if regex_route == "instruction.edit":
        # 정규식은 "못 고친다"고 답합니다. LLM이 갱신 도구를 골랐다면 그게
        # 바로 옮겨갈 만한 자리라는 신호입니다.
        return llm_tool in ("update_recurring_schedule", "update_project", "clarify")
    if regex_route == "project.note":
        return llm_tool == "add_project_note"
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

    # 각색 없이 레코드 필드를 그대로 보는 통로입니다. 요약이 원본과 어긋나는지
    # 사용자가 확인할 수 있어야 합니다.
    if text in ("!원본", "!raw"):
        _log_turn(text, source, "command.raw")
        yield Reply(format_schedule_raw())
        return

    if text in ("!반복", "!recur"):
        _log_turn(text, source, "command.recurrences")
        yield Reply(format_recurrences_raw())
        return

    note = parse_project_note(text)
    if note:
        yield _handle_project_note(note, text, source)
        return

    project_action = detect_project_action(text)
    if project_action:
        reply = _handle_project_action(project_action, text, source)
        _log_turn(text, source, "project", {"action": project_action})
        yield reply
        return

    # 지시문은 저장 대상이 아닙니다. 여기서 처리하고 끝냅니다. 아래로 흘려보내면
    # ask_gemini가 도구도 없이 "삭제했습니다"라고 답하게 됩니다.
    instruction = detect_instruction(text)
    if instruction:
        yield from _handle_instruction(instruction, text, source)
        return

    # 반복 요청은 단발로 저장하기 전에 붙잡습니다. 여기서 놓치면 "평일 05:10
    # 반복"이 11/1 단발 한 건으로 남고, 사용자에게는 반복이 등록된 것처럼
    # 들리는 답이 나갑니다.
    recurrence = parse_recurrence_request(text)
    if recurrence:
        yield _handle_recurrence_request(recurrence, text, source)
        return

    intent = detect_message_intent(text)

    if intent == "ambiguous":
        # 여러 항목이 섞인 메시지를 통째로 한 건으로 저장하지 않습니다.
        # 오탐 하나가 캘린더에 영구히 남는 비용이, 되묻는 비용보다 큽니다.
        _log_turn(text, source, "ambiguous")
        yield Reply(
            "여러 일정이 섞여 있는 것 같아 아직 저장하지 않았습니다.\n"
            "한 건씩 보내주시면 그대로 저장하겠습니다. "
            "되풀이되는 일정이면 '평일 06:10 보고서 확인'처럼 요일과 시각을 "
            "적어주시면 반복 규칙으로 만듭니다."
        )
        return

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
    except Exception as error:
        logging.exception("Error while generating an answer")
        yield Reply(_answer_failure_message(error))
