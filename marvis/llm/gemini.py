"""google-genai 어댑터.

레거시 `google.generativeai`(지원 종료)를 대체합니다. 자동 함수 호출은
꺼 둡니다 — 도구 실행과 검증은 우리 루프가 직접 해야 로그와 재시도를
통제할 수 있습니다.
"""

import logging

from google import genai
from google.genai import types

from ..settings import GEMINI_API_KEY, GEMINI_MODEL
from .base import LLMResponse, Message, ToolCall, Usage


class GeminiClient:
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or GEMINI_MODEL
        key = api_key or GEMINI_API_KEY
        if not key:
            raise ValueError("GEMINI_API_KEY is missing in .env")
        self._client = genai.Client(api_key=key)

    # ------------------------------------------------------------ 변환

    def _to_contents(self, messages: list[Message]) -> list[types.Content]:
        contents: list[types.Content] = []
        for message in messages:
            if message.role == "user":
                contents.append(
                    types.Content(role="user", parts=[types.Part.from_text(text=message.text)])
                )
            elif message.role == "assistant":
                parts = []
                if message.text:
                    parts.append(types.Part.from_text(text=message.text))
                for call in message.tool_calls:
                    parts.append(
                        types.Part.from_function_call(name=call.name, args=call.args)
                    )
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif message.role == "tool":
                parts = [
                    types.Part.from_function_response(
                        name=result.name, response=result.content
                    )
                    for result in message.tool_results
                ]
                if parts:
                    contents.append(types.Content(role="user", parts=parts))
        return contents

    def _to_tools(self, tools: list[dict]) -> list[types.Tool]:
        if not tools:
            return []
        declarations = [
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool["description"],
                parameters_json_schema=tool["parameters"],
            )
            for tool in tools
        ]
        return [types.Tool(function_declarations=declarations)]

    # ------------------------------------------------------------ 호출

    def complete(self, system: str, messages: list[Message], tools: list[dict]) -> LLMResponse:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=self._to_tools(tools),
            # 우리가 도구를 직접 실행합니다. SDK가 대신 부르면 검증도 로그도 건너뜁니다.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # 라우팅은 창의성이 필요한 일이 아닙니다.
            temperature=0.2,
        )

        response = self._client.models.generate_content(
            model=self.model,
            contents=self._to_contents(messages),
            config=config,
        )

        calls = [
            ToolCall(name=call.name, args=dict(call.args or {}), call_id=call.id or call.name)
            for call in (response.function_calls or [])
        ]

        text = ""
        if not calls:
            try:
                text = (response.text or "").strip()
            except Exception:
                # 안전 필터 등으로 후보가 없을 때 .text가 예외를 던집니다.
                logging.warning("Gemini returned no usable text candidate")

        usage = Usage()
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage = Usage(
                input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
                output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
            )

        return LLMResponse(text=text, tool_calls=calls, usage=usage, model=self.model)

    def generate_text(self, prompt: str) -> str:
        """도구 없이 한 번 호출합니다."""
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.4),
        )
        try:
            return (response.text or "").strip()
        except Exception:
            logging.warning("Gemini returned no usable text candidate")
            return ""
