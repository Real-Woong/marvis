"""도구를 쓰는 에이전트 루프.

한 번 부르고 끝내지 않고, 도구 결과를 대화에 넣어 이어갑니다. 검증 실패는
오류 객체로 모델에게 돌려주되 재시도는 한 번만 허용하고, 그래도 안 되면
사용자에게 되묻습니다. 무한히 도는 것보다 물어보는 쪽이 낫습니다.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from .db import log_event, transaction
from .llm.base import LLMResponse, Message, ToolResult
from .llm.tools import TERMINAL_TOOLS, context_summary, execute, schemas

# 도구 결과를 넣고 모델을 다시 부르는 최대 횟수.
MAX_STEPS = 3
# 같은 도구가 검증에 실패했을 때 다시 시도해 볼 횟수.
MAX_RETRIES_PER_TOOL = 1
# 한 턴에 허용하는 도구 호출 총량.
#
# 상한이 없던 시절, "월~금 반복 알림을 만들어 줘" 한 마디에 모델이 단발
# 84건을 펼쳐 넣고 MAX_STEPS를 소진해 "요청을 처리했지만 정리해서
# 말씀드리지 못했습니다"로 끝난 적이 있습니다. 사용자는 무엇이 저장됐는지
# 듣지 못했고, DB에는 84건이 남았습니다. 이만큼 부르고 있다면 도구 선택이
# 틀린 것이므로, 계속 쓰게 두는 것보다 멈추고 묻는 편이 낫습니다.
MAX_TOOL_CALLS = 20

SYSTEM_PROMPT = """너는 사용자의 개인 AI 비서 'Marvis'다.

역할: 사용자가 까먹기 쉬운 일정·할 일·아이디어를 도구로 저장하고, 물으면
저장된 내용을 근거로 답한다.

행동 규칙:
- 상태를 바꾸거나 저장된 내용을 확인하려면 반드시 도구를 쓴다. 기억에 의존해
  지어내지 않는다.
- 날짜와 시각은 아래 '지금' 정보를 기준으로 직접 계산해서 절대값으로 넣는다.
  "다음주 화요일", "모레", "30분 뒤" 같은 표현을 도구 인자에 그대로 넘기지 않는다.
- 저장·조회할 것이 없는 평범한 대화면 도구를 부르지 말고 그냥 답한다.
- 어느 프로젝트인지, 어떤 항목인지 특정할 수 없으면 추측하지 말고 clarify를 쓴다.
- 도구가 error를 돌려주면 message와 hint를 읽고 고쳐서 한 번 더 시도한다.
  그래도 안 되면 clarify로 사용자에게 묻는다.
- 답변은 한국어로, 핵심부터 짧게. 이동 중 에어팟으로 들어도 이해할 수 있게 말한다.
- 알림을 보냈다고 거짓말하지 않는다. 알림은 저장된 시각이 되면 시스템이 보낸다.

완료 보고 규칙 (어기면 사용자가 저장소를 직접 열어보기 전까지 알 수 없다):
- 도구를 부르지 않았으면 아무것도 하지 않은 것이다. "고쳤습니다",
  "업데이트했습니다", "삭제했습니다"라고 쓰기 전에, 이번 턴에 그 도구를
  실제로 불렀고 결과가 성공이었는지 확인한다.
- 도구 결과에 verified: true 가 있을 때만 완료형으로 말한다. 없으면
  "시도했으나 반영을 확인하지 못했습니다"라고 그대로 말한다.
- 완료 보고에는 영향받은 번호를 함께 적는다. 예: "[12]를 지웠습니다."
- 도구가 없는 기능은 "지원하지 않습니다"라고 말한다. 없는 동작을
  미래형("자동으로 변경될 예정입니다")으로 서술하지 않는다. 저장소에 없는
  규칙은 존재하지 않는 규칙이다.
- 조회 결과를 옮길 때 레코드의 종류를 바꾸지 않는다. 단발 일정 한 건을
  "반복 규칙"이라고 부르지 않는다.

반복 일정:
- 되풀이되는 알림은 save_recurring_schedule 로 규칙 한 건을 만든다.
  save_memory 를 여러 번 불러 단발로 펼치지 않는다.
- 반복 알림을 묻거나 고쳐 달라고 하면 먼저 list_recurring_schedules 로
  실제로 저장된 것을 확인하고 답한다.

지금:
- 오늘: {today} ({weekday}요일)
- 현재 시각: {now} (KST)
- 미완료 일정 {open_schedules}건, 아이디어 {ideas}건, 반복 규칙 {recurrences}건
- 등록된 프로젝트: {project_names}

프로젝트 목록은 이름 확인용이다. 내용이 필요하면 list_projects를 불러라."""


@dataclass
class AgentResult:
    """한 턴의 결과."""

    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    clarify: dict | None = None
    steps: int = 0
    error: str | None = None

    @property
    def primary_tool(self) -> str | None:
        """이 턴에서 상태를 결정한 첫 도구 이름. eval 채점의 기준입니다."""
        return self.tool_calls[0]["name"] if self.tool_calls else None


def build_system_prompt() -> str:
    summary = context_summary()
    names = ", ".join(summary["project_names"]) or "없음"
    return SYSTEM_PROMPT.format(
        today=summary["today"],
        weekday=summary["weekday"],
        now=summary["now"],
        open_schedules=summary["open_schedules"],
        ideas=summary["ideas"],
        recurrences=summary["recurrences"],
        project_names=names,
    )


def _log_trace(source: str, user_text: str, result: AgentResult, elapsed_ms: int, usage) -> None:
    """턴 하나의 전체 궤적을 남깁니다. '왜 저랬지'를 열어볼 수 있어야 합니다."""
    try:
        with transaction() as tx:
            log_event(
                tx,
                "turn.trace",
                entity="turn",
                source=source,
                payload={
                    "text": user_text,
                    "steps": result.steps,
                    "tool_calls": result.tool_calls,
                    "clarify": result.clarify,
                    "reply": result.text,
                    "error": result.error,
                    "elapsed_ms": elapsed_ms,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
            )
    except Exception:
        logging.exception("Failed to log the agent trace")


def run_turn(
    client, user_text: str, source: str = "telegram", dry_run: bool = False
) -> AgentResult:
    """사용자 발화 하나를 도구를 써서 처리합니다.

    dry_run=True 면 상태를 바꾸는 도구는 실행하지 않고, 무엇을 하려 했는지만
    결과로 돌려줍니다. shadow 모드(관찰 전용)가 이걸 씁니다. 조회 도구는
    그대로 돌아서 모델이 현실을 보고 판단하게 둡니다.
    """
    started = time.monotonic()
    system = build_system_prompt()
    messages: list[Message] = [Message(role="user", text=user_text)]
    tools = schemas()

    result = AgentResult()
    total_usage = None
    failure_counts: dict[str, int] = {}

    for step in range(MAX_STEPS):
        result.steps = step + 1
        try:
            response: LLMResponse = client.complete(system, messages, tools)
        except Exception as error:
            logging.exception("LLM call failed")
            result.error = f"llm_call_failed: {error}"
            break

        if total_usage is None:
            total_usage = response.usage
        else:
            total_usage.input_tokens += response.usage.input_tokens
            total_usage.output_tokens += response.usage.output_tokens

        if not response.wants_tools:
            result.text = response.text
            break

        messages.append(
            Message(role="assistant", text=response.text, tool_calls=response.tool_calls)
        )

        tool_results: list[ToolResult] = []
        stop = False

        for call in response.tool_calls:
            if len(result.tool_calls) >= MAX_TOOL_CALLS:
                result.clarify = {
                    "question": (
                        f"한 번에 {MAX_TOOL_CALLS}건이 넘는 작업을 하려고 해서 "
                        "멈췄습니다. 반복되는 일정이라면 반복 규칙 하나로 만들 수 "
                        "있습니다. 어떻게 할까요?"
                    ),
                    "options": [],
                }
                result.text = result.clarify["question"]
                result.error = "tool_call_limit"
                stop = True
                break
            content = execute(call.name, call.args, source=source, dry_run=dry_run)
            result.tool_calls.append(
                {"name": call.name, "args": call.args, "ok": "error" not in content}
            )
            tool_results.append(
                ToolResult(call_id=call.call_id, name=call.name, content=content)
            )

            if "error" in content:
                failure_counts[call.name] = failure_counts.get(call.name, 0) + 1
                if failure_counts[call.name] > MAX_RETRIES_PER_TOOL:
                    # 재시도를 다 썼습니다. 계속 시키는 대신 사용자에게 넘깁니다.
                    result.clarify = {
                        "question": content.get("message", "요청을 처리하지 못했습니다.")
                        + " 어떻게 할까요?",
                        "options": content.get("candidates", []),
                    }
                    result.text = result.clarify["question"]
                    stop = True
                    break
                continue

            if call.name in TERMINAL_TOOLS:
                result.clarify = {
                    "question": content["question"],
                    "options": content.get("options", []),
                }
                result.text = content["question"]
                stop = True
                break

        if stop:
            break

        messages.append(Message(role="tool", tool_results=tool_results))
    else:
        # MAX_STEPS를 다 썼는데도 마무리 문장이 없는 경우입니다.
        if not result.text:
            result.text = "요청을 처리했지만 정리해서 말씀드리지 못했습니다."
            result.error = "max_steps_exhausted"

    if not result.text and not result.error:
        result.text = "죄송합니다. 답변을 생성하지 못했습니다."

    if result.clarify and result.clarify["options"]:
        options = "\n".join(f"- {option}" for option in result.clarify["options"])
        result.text = f"{result.clarify['question']}\n{options}"

    elapsed_ms = int((time.monotonic() - started) * 1000)
    from .llm.base import Usage

    _log_trace(source, user_text, result, elapsed_ms, total_usage or Usage())
    return result
