"""저장된 기억을 문맥으로 구성하고 답변을 생성합니다.

정규식 라우터가 쓰는 단발 프롬프트 경로입니다. 도구를 쓰지 않고 기억 전문을
프롬프트에 실어 보냅니다. MARVIS_ROUTER=llm으로 전환하면 ask_gemini는 쓰이지
않고 아침 브리핑만 남습니다.
"""

from .llm.factory import get_client
from .memory import (
    archive_past_schedules,
    format_memories,
    format_schedule_by_date,
    get_ideas,
    get_recent_memories,
)
from .projects import format_all_projects
from .secretary import sync as sync_secretary_projects
from .settings import MAX_IDEA_CONTEXT_ITEMS, MAX_RECENT_CONTEXT_ITEMS
from .time_utils import today_kst_date


def ask_gemini(user_text: str) -> str:
    """현재 일정과 최근 기억을 포함한 프롬프트로 개인화 답변을 요청합니다."""
    # 지난 일정이 AI 문맥에 포함되지 않도록 요청 직전에 보관 처리합니다.
    archive_past_schedules()
    active_schedules = format_schedule_by_date()
    recent_memories = format_memories(get_recent_memories(limit=MAX_RECENT_CONTEXT_ITEMS))
    ideas = format_memories(get_ideas()[-MAX_IDEA_CONTEXT_ITEMS:])
    sync_secretary_projects()
    projects = format_all_projects()
    prompt = f"""
너는 사용자의 개인 AI 비서 'Marvis'야.

Marvis의 핵심 역할:
- 사용자가 까먹기 쉬운 아이디어, 할 일, 일정, 생각을 기억하고 정리한다.
- 사용자가 저장된 내용을 물으면 기억을 기반으로 답한다.
- 사용자의 일정과 아이디어를 비서처럼 관리한다.
- 사용자가 진행 중인 사이드 프로젝트를 파악하고 있다가, "프로젝트 스케쥴" 또는
  "프로젝트 할일" 처럼 프로젝트 관련 질문을 받으면 아래 프로젝트 현황을 기반으로
  진행중/중단 상태와, 진행중 프로젝트는 다음에 할 일까지 정리해서 답한다.
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

프로젝트 현황(진행중/중단):
{projects}

사용자 메시지:
{user_text}
"""
    answer = get_client().generate_text(prompt)
    return answer or "죄송합니다. 답변을 생성하지 못했습니다."


def generate_morning_briefing() -> str:
    """사용자가 묻지 않아도 먼저 보내는 아침 브리핑 중 일정 요약 부분을 생성합니다.

    프로젝트 현황은 Siri "알림 읽어주기"가 긴 메시지를 요약해버리는 것을 피하려고
    이 함수에 포함하지 않고, 호출부(reminders.py)에서 프로젝트당 별도 메시지로 보낸다.
    """
    # ask_gemini와 달리 사용자 메시지가 없는 능동 발화이므로, 질문에 답하는
    # 대신 오늘 일정을 요약해서 먼저 브리핑하라고 역할을 명확히 지정합니다.
    archive_past_schedules()
    active_schedules = format_schedule_by_date()
    prompt = f"""
너는 사용자의 개인 비서 'Marvis'야.
지금은 한국 시간 {today_kst_date().isoformat()} 아침이고, 오늘 일정을 사람이 말하듯
한 문장으로 브리핑해야 해.

출력 형식을 반드시 그대로 지켜라:
- 오늘 일정이 하나도 없으면 정확히 "오늘 아침 일정은 없습니다." 라고만 답한다.
- 오늘 일정이 있으면 "오늘 아침 일정은 (내용)입니다." 형태의 문장으로, (내용) 자리에
  우선순위 순으로 정리한 일정을 자연스럽게 채워 넣는다.
- 인사말, 이모지, 부연 설명, 위 형식 이외의 문장은 절대 추가하지 않는다.
- 답변은 한국어로 한다.

오늘 날짜별 스케쥴:
{active_schedules}
"""
    briefing = get_client().generate_text(prompt)
    return briefing or "오늘 아침 일정은 확인하지 못했습니다."
