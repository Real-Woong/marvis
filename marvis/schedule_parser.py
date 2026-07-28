"""사용자 문장을 기억 유형으로 분류하고 날짜와 알림 시각을 추출합니다."""

import re
from datetime import datetime, timedelta

from .settings import KST
from .time_utils import iso_datetime_kst, now_kst, today_kst_date


def classify_memory(text: str) -> str:
    """키워드를 기반으로 메시지를 일정, 아이디어, 일반 메모로 분류합니다."""
    lowered = text.lower()
    schedule_keywords = [
        "일정", "스케쥴", "스케줄", "오늘", "내일", "모레", "이번주", "다음주",
        "해야", "해야해", "해야 돼", "할 일", "할일", "약속", "회의", "미팅",
        "마감", "기한", "리마인드", "알림", "알려줘", "챙겨", "예약", "방문",
        "제출", "업로드", "정리하기", "확인하기", "처리하기", "전화", "연락",
        "운동", "병원", "공부", "출근", "퇴근", "준비",
    ]
    idea_keywords = [
        "아이디어", "생각났", "구상", "기획", "만들고 싶", "프로젝트",
        "서비스", "앱", "개발", "컨셉", "문제의식", "사업", "기능",
        "구현", "설계", "고도화", "bm", "비즈니스", "창업",
    ]
    if any(keyword in lowered for keyword in schedule_keywords):
        return "schedule"
    if any(keyword in lowered for keyword in idea_keywords):
        return "idea"
    return "note"


def extract_schedule_date(text: str) -> str | None:
    """오늘·내일·숫자 날짜 표현을 YYYY-MM-DD 형식으로 변환합니다."""
    today = today_kst_date()
    if "모레" in text:
        return (today + timedelta(days=2)).isoformat()
    if "내일" in text:
        return (today + timedelta(days=1)).isoformat()
    if "오늘" in text:
        return today.isoformat()

    # 연도가 포함된 날짜(2026-08-15, 2026.08.15, 2026/08/15)를 처리합니다.
    match = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", text)
    if match:
        try:
            return datetime(*(int(value) for value in match.groups()), tzinfo=KST).date().isoformat()
        except ValueError:
            return None

    # 연도가 없는 날짜는 올해로 해석하되 이미 지났으면 다음 해로 넘깁니다.
    match = re.search(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?!\d)", text)
    if not match:
        match = re.search(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if match:
        month, day = (int(value) for value in match.groups())
        try:
            schedule_date = datetime(today.year, month, day, tzinfo=KST).date()
            if schedule_date < today:
                schedule_date = datetime(today.year + 1, month, day, tzinfo=KST).date()
            return schedule_date.isoformat()
        except ValueError:
            return None
    return None


def extract_reminder_datetime(text: str, schedule_date: str | None) -> str | None:
    """상대 시간 또는 시각 표현을 실제 한국 시간 알림 시각으로 변환합니다."""
    current = now_kst()
    # '10분 뒤', '2시간 후'와 같은 상대 시간 표현을 먼저 처리합니다.
    match = re.search(r"(\d{1,3})\s*분\s*(뒤|후)", text)
    if match:
        return iso_datetime_kst(current + timedelta(minutes=int(match.group(1))))
    match = re.search(r"(\d{1,2})\s*시간\s*(뒤|후)", text)
    if match:
        return iso_datetime_kst(current + timedelta(hours=int(match.group(1))))

    if schedule_date:
        try:
            base_date = datetime.fromisoformat(schedule_date).date()
        except ValueError:
            base_date = today_kst_date()
    else:
        base_date = today_kst_date()

    hour = None
    minute = 0
    # 15:30 또는 오후 3시 30분 같은 절대 시각 표현을 처리합니다.
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    if hour is None:
        match = re.search(r"(오전|오후|아침|낮|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", text)
        if match:
            meridiem = match.group(1)
            hour = int(match.group(2))
            minute = int(match.group(3)) if match.group(3) else 0
            if meridiem in ["오후", "저녁", "밤"] and hour < 12:
                hour += 12
            if meridiem in ["오전", "아침"] and hour == 12:
                hour = 0
    if hour is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None

    reminder = datetime(base_date.year, base_date.month, base_date.day, hour, minute, tzinfo=KST)
    if not schedule_date and reminder <= current:
        reminder += timedelta(days=1)
    return iso_datetime_kst(reminder)

def detect_message_intent(text: str) -> str:
    """메세지가 저장 요철, 조회질문, 일반 대화 중 무엇인지 판별"""
    normalized = " ".join(text.lower().split())

    # 저장된 기억이나 일정을 조회하는 표현입니다
    memory_query_patterns = [
        "뭐 해야",
        "뭐해야",
        "일정 뭐",
        "일정 알려",
        "일정 보여",
        "일정 있어",
        "스케줄 뭐",
        "스케쥴 뭐",
        "남은 일정",
        "뭐였지",
        "뭐였어",
        "기억나",
        "아이디어 뭐",
        "메모 뭐",
        "저장한 내용",
        "뭐였지",
        "기억해",
        "했었나"
    ]

    if any(pattern in normalized for pattern in memory_query_patterns):
        return "query"
    
    schedule_date = extract_schedule_date(text)
    reminder_at = extract_reminder_datetime(text, schedule_date)

    # 시간과 알림 요청이 함께 있으면 일정 저장으로 처리
    reminder_request_patterns = [
        "알려줘",
        "알림",
        "리마인드",
        "챙겨줘",
        "잊지 않게",
    ]

    if reminder_at and any(
        pattern in normalized for pattern in reminder_request_patterns
    ):
        return "save"
    
    # 사용자가 명시적으로 저장을 요청하는 표현입니다.
    explicit_save_patterns = [
        "기억해",
        "저장해",
        "메모해",
        "추가해",
        "등록해",
        "기록해",
        "해야 해",
        "해야해",
        "해야 돼",
        "할 일이야",
        "예정이야",
        "약속 있어",
        "예약했어",
        "추가해줘",
    ]

    if any(pattern in normalized for pattern in explicit_save_patterns):
        return "save"
    
     # 일반적인 질문 표현은 기억으로 저장하지 않습니다.
    question_patterns = [
        "뭐야",
        "뭐지",
        "어때",
        "어떻게",
        "왜",
        "추천해줘",
        "설명해줘",
        "알고 있어",
        "알아?",
    ]

    if text.rstrip().endswith("?"):
        return "query"
    
    if any(pattern in normalized for pattern in question_patterns):
        return "query"
    
    # 날짜가 포함된 평서문은 일정으로 간주합니다
    if schedule_date or reminder_at:
        return "save"

    # 아이디어를 말한 경우에도 저장합니다.
    if classify_memory(text) == "idea":
        return "save"

    return "chat"   