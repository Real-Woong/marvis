"""저장된 기억을 문맥으로 구성하고 Gemini 답변을 생성합니다."""

import google.generativeai as genai

from .memory import format_memories, format_schedule_by_date, get_ideas, get_recent_memories, prune_past_schedules
from .settings import GEMINI_API_KEY, MAX_RECENT_CONTEXT_ITEMS
from .time_utils import today_kst_date


_model = None


def get_model():
    """Gemini 클라이언트를 최초 요청 시 한 번만 생성해 재사용합니다."""
    global _model
    if _model is None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is missing in .env")
        genai.configure(api_key=GEMINI_API_KEY)
        _model = genai.GenerativeModel("gemini-2.5-flash")
    return _model


def ask_gemini(user_text: str) -> str:
    """현재 일정과 최근 기억을 포함한 프롬프트로 개인화 답변을 요청합니다."""
    # 지난 일정이 AI 문맥에 포함되지 않도록 요청 직전에 정리합니다.
    prune_past_schedules()
    active_schedules = format_schedule_by_date()
    recent_memories = format_memories(get_recent_memories(limit=MAX_RECENT_CONTEXT_ITEMS))
    ideas = format_memories(get_ideas()[-20:])
    prompt = f"""
너는 사용자의 개인 AI 비서 'Marvis'야.

Marvis의 핵심 역할:
- 사용자가 까먹기 쉬운 아이디어, 할 일, 일정, 생각을 기억하고 정리한다.
- 사용자가 저장된 내용을 물으면 기억을 기반으로 답한다.
- 사용자의 일정과 아이디어를 비서처럼 관리한다.
- 답변은 한국어로 한다.
- 너무 길지 않게 핵심부터 말한다.
- 이동 중 에어팟으로 들어도 이해하기 쉽게 말한다.
- 필요한 경우 우선순위를 정리한다.
- 오늘 날짜 기준은 한국 시간 {today_kst_date().isoformat()} 이다.
- 지난 날짜의 스케쥴은 자동 정리된 상태라고 가정한다.
- 알림 시간이 있는 스케쥴은 시간이 되면 Marvis가 먼저 텔레그램으로 알림을 보낸다.
- 답변에서 알림을 보냈다고 거짓말하지 않는다.

현재 날짜별 스케쥴:
{active_schedules}

최근 저장된 기억:
{recent_memories}

최근 아이디어:
{ideas}

사용자 메시지:
{user_text}
"""
    response = get_model().generate_content(prompt)
    if not response.text:
        return "죄송합니다. 답변을 생성하지 못했습니다."
    return response.text.strip()
