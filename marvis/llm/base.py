"""프로바이더에 상관없이 통하는 정규형입니다.

에이전트 루프(agent.py)는 이 타입들만 봅니다. Gemini든 Claude든 각자
어댑터가 자기 SDK 형식을 여기로 번역합니다. 이렇게 해 두면 프로바이더를
바꾸는 일이 설정 한 줄이 되고, 같은 골든셋으로 둘을 비교할 수 있습니다.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    """모델이 요청한 도구 호출 하나."""

    name: str
    args: dict[str, Any]
    call_id: str = ""


@dataclass
class ToolResult:
    """도구 실행 결과. 오류도 결과의 한 형태로 모델에게 돌아갑니다."""

    call_id: str
    name: str
    content: dict[str, Any]

    @property
    def is_error(self) -> bool:
        return "error" in self.content


@dataclass
class Message:
    """대화 한 줄.

    role: user | assistant | tool
    assistant는 text와 tool_calls를 함께 가질 수 있고,
    tool은 tool_results만 가집니다.
    """

    role: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """어댑터가 구현해야 하는 전부."""

    name: str

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """도구 목록과 함께 한 번 호출합니다.

        tools는 {"name", "description", "parameters"(JSON Schema)} 형태의
        평범한 dict 목록입니다. 어댑터가 자기 SDK 타입으로 옮깁니다.
        """
        ...
