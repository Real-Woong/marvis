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

# ---------------------------------------------------------------- 지시문 판별
#
# "일정 고쳐줘", "[12] 지워줘" 같은 메시지는 일정이 아니라 일정에 대한 지시입니다.
# 예전에는 이런 문장에 날짜가 들어 있으면(그리고 지시문에는 거의 항상 들어
# 있습니다) 마지막 규칙인 "날짜가 있으면 저장"에 걸려서, 지시 메시지 전문이
# 새 일정 레코드가 됐습니다. 본문의 "11/1부터는 06:10"에서 알림 시각까지
# 뽑아 쓰면서요. 오탐 하나가 캘린더에 영구히 남는 비용은, 되묻는 비용보다
# 훨씬 큽니다.

INSTRUCTION_DELETE = "delete"
INSTRUCTION_EDIT = "edit"
INSTRUCTION_SHOW_RAW = "show_raw"

_DELETE_PATTERNS = [
    "지워줘", "지워 줘", "지워라", "삭제해", "삭제 해", "삭제해줘", "삭제하고",
    "없애줘", "없애 줘", "빼줘", "빼 줘", "취소해", "취소해줘",
]

_EDIT_PATTERNS = [
    "고쳐줘", "고쳐 줘", "고쳐라", "수정해", "수정 해", "수정해줘", "바꿔줘",
    "바꿔 줘", "변경해", "변경해줘", "업데이트해", "갱신해", "반영해",
]

_SHOW_RAW_PATTERNS = [
    "원본을 그대로", "원본 그대로", "저장된 원본", "정리 말고", "요약 말고",
    "저장된 그대로", "그대로 보여줘", "원본 보여줘",
]

# 대괄호 번호만 뽑습니다. `12번`과 달리 이 형태는 우리 목록에서 복사한
# 것이 확실합니다. 자연스러운 문장에 `[12]`가 우연히 들어갈 일은 없습니다.
_BRACKET_REF = re.compile(r"\[(\d{1,5})\]")


def parse_item_refs(text: str) -> list[int]:
    """문장에 적힌 항목 번호를 뽑습니다. `[12]`, `12번` 두 형태만 봅니다.

    맨숫자(`12`)는 일부러 제외합니다. 날짜·시각·개수와 구별되지 않아서,
    "세 가지 남았어" 같은 문장에서 엉뚱한 항목을 지우게 됩니다.
    """
    refs: list[int] = []
    for match in re.finditer(r"\[(\d{1,5})\]|(?<!\d)(\d{1,5})\s*번(?![호])", text):
        value = match.group(1) or match.group(2)
        number = int(value)
        if number not in refs:
            refs.append(number)
    return refs


def parse_recurrence_refs(text: str) -> list[int]:
    """반복 규칙 번호(`[R2]`, `R2`)를 뽑습니다."""
    refs: list[int] = []
    for match in re.finditer(r"\[?R(\d{1,5})\]?", text, re.IGNORECASE):
        number = int(match.group(1))
        if number not in refs:
            refs.append(number)
    return refs


def detect_instruction(text: str) -> str | None:
    """이 문장이 저장 대상이 아니라 '저장된 것에 대한 지시'인지 판별합니다.

    반환값은 INSTRUCTION_* 중 하나이거나 None입니다. None이 아니면 호출부는
    이 메시지를 새 일정으로 저장해서는 안 됩니다.
    """
    normalized = " ".join(text.lower().split())

    if any(pattern in normalized for pattern in _SHOW_RAW_PATTERNS):
        return INSTRUCTION_SHOW_RAW
    if any(pattern in normalized for pattern in _DELETE_PATTERNS):
        return INSTRUCTION_DELETE
    if any(pattern in normalized for pattern in _EDIT_PATTERNS):
        return INSTRUCTION_EDIT
    # 지시 동사가 없을 때는 대괄호 번호만 신호로 씁니다.
    #
    # `12번`까지 신호로 삼았더니 "12번 버스 타야 해", "3번 출구에서 만나기로
    # 했어" 같은 멀쩡한 일정이 지시문으로 분류돼 저장되지 않았습니다. 오탐을
    # 막으려다 정탐을 막은 셈이라, 여기서는 `[12]`처럼 우리 목록에서 복사해
    # 온 것이 분명한 형태만 봅니다. `12번`은 위의 삭제·수정 동사와 함께
    # 나왔을 때만 번호로 읽힙니다.
    if _BRACKET_REF.search(text) or parse_recurrence_refs(text):
        return INSTRUCTION_EDIT
    return None


def detect_message_intent(text: str) -> str:
    """메세지가 저장 요철, 조회질문, 일반 대화 중 무엇인지 판별"""
    normalized = " ".join(text.lower().split())

    # 지시문이 가장 먼저입니다. 아래 어떤 규칙에도 걸리지 않게 합니다.
    if detect_instruction(text):
        return "instruction"

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
        "했었나",
        "프로젝트 스케쥴",
        "프로젝트 스케줄",
        "프로젝트 할일",
        "프로젝트 할 일",
        "프로젝트 진행",
        "프로젝트 상황",
        "프로젝트 상태",
        "프로젝트 뭐",
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
    
    # 날짜가 포함된 평서문은 일정으로 간주합니다. 단, 한 항목으로 읽히는
    # 짧은 문장일 때만입니다. 여러 항목이 섞인 긴 메시지를 통째로 한 건으로
    # 저장하면, 사용자가 원한 5건 대신 전문이 담긴 1건이 남습니다.
    if schedule_date or reminder_at:
        if looks_like_multiple_items(text):
            return "ambiguous"
        return "save"

    # 아이디어를 말한 경우에도 저장합니다.
    if classify_memory(text) == "idea":
        return "save"

    return "chat"


# 한 줄짜리 일정으로 보기 어려운 길이. "5.25 09:00 GitHub README 정리하기"는
# 60자를 한참 밑돌고, 여러 항목을 늘어놓은 메시지는 여지없이 넘깁니다.
MAX_SINGLE_ITEM_LENGTH = 160


def looks_like_multiple_items(text: str) -> bool:
    """이 메시지가 항목 하나가 아니라 여러 개를 담고 있는지 봅니다.

    정규식은 어느 조각이 어느 항목인지 나눌 수 없습니다. 나눌 수 없다는 것을
    아는 것까지가 정규식이 할 수 있는 일이고, 그때는 저장하지 말고 물어야
    합니다.
    """
    stripped = text.strip()
    if len(stripped) > MAX_SINGLE_ITEM_LENGTH:
        return True
    if len([line for line in stripped.splitlines() if line.strip()]) > 2:
        return True
    # 번호를 매긴 목록("1)", "2.", "- ")이 두 개 이상이면 항목이 여럿입니다.
    if len(re.findall(r"(?m)^\s*(?:\d+[).]|[-·*])\s+", stripped)) >= 2:
        return True
    return False   

# ---------------------------------------------------------------- 반복 일정
#
# 반복은 예전에 표현할 방법이 아예 없었습니다. "평일 05:10 반복" 요청이
# 들어오면 그 문장이 11/1 단발 하나로 저장되고, 사용자에게는 "반복 규칙으로
# 등록했습니다"라고 답이 나갔습니다. 저장소에 없는 것을 있다고 말한 겁니다.

_WEEKDAY_GROUPS = {
    "평일": [0, 1, 2, 3, 4],
    "주중": [0, 1, 2, 3, 4],
    "주말": [5, 6],
    "매일": [0, 1, 2, 3, 4, 5, 6],
    "날마다": [0, 1, 2, 3, 4, 5, 6],
}

_RECURRENCE_MARKERS = [
    "반복", "매일", "매주", "평일", "주중", "주말", "날마다", "요일마다",
    "월~금", "월-금", "월요일부터", "정기적으로",
]


def _extract_weekdays(text: str) -> list[int] | None:
    """문장에서 요일 집합을 뽑습니다. 못 찾으면 None."""
    for keyword, days in _WEEKDAY_GROUPS.items():
        if keyword in text:
            return list(days)

    names = ["월", "화", "수", "목", "금", "토", "일"]
    # '월~금', '화-목' 같은 범위.
    match = re.search(r"([월화수목금토일])\s*[~\-]\s*([월화수목금토일])", text)
    if match:
        first, last = names.index(match.group(1)), names.index(match.group(2))
        if first <= last:
            return list(range(first, last + 1))
        return list(range(first, 7)) + list(range(0, last + 1))

    # '월요일', '수요일과 금요일' 처럼 낱개로 적은 경우.
    found = sorted({names.index(day) for day in re.findall(r"([월화수목금토일])요일", text)})
    return found or None


def _extract_time_of_day(text: str) -> str | None:
    """'05:10', '오전 6시 10분' 을 'HH:MM' 으로. 못 찾으면 None."""
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        match = re.search(
            r"(오전|오후|아침|낮|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", text
        )
        if not match:
            return None
        meridiem = match.group(1)
        hour = int(match.group(2))
        minute = int(match.group(3)) if match.group(3) else 0
        if meridiem in ("오후", "저녁", "밤") and hour < 12:
            hour += 12
        if meridiem in ("오전", "아침") and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _extract_boundary(text: str, suffix: str) -> str | None:
    """'2026-11-02부터', '10/30까지' 에서 날짜를 뽑습니다."""
    pattern = (
        r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})\s*" + suffix
        + r"|(?<!\d)(\d{1,2})[./](\d{1,2})(?!\d)\s*" + suffix
        + r"|(\d{1,2})월\s*(\d{1,2})일\s*" + suffix
    )
    match = re.search(pattern, text)
    if not match:
        return None
    groups = [value for value in match.groups() if value is not None]
    today = today_kst_date()
    try:
        if len(groups) == 3:
            year, month, day = (int(value) for value in groups)
        else:
            month, day = (int(value) for value in groups)
            year = today.year
            if datetime(year, month, day, tzinfo=KST).date() < today:
                year += 1
        return datetime(year, month, day, tzinfo=KST).date().isoformat()
    except ValueError:
        return None


def parse_recurrence_request(text: str) -> dict | None:
    """반복 일정 요청을 규칙으로 해석합니다.

    확실할 때만 dict를 돌려줍니다. 요일이나 시각을 하나라도 못 뽑으면
    None입니다. 호출부는 그때 추측하지 말고 되물어야 합니다.

    돌려주는 dict:
        weekdays   [0..6]   월=0
        at_time    'HH:MM'
        starts_on  'YYYY-MM-DD'  (없으면 오늘)
        ends_on    'YYYY-MM-DD' | None
    """
    if not any(marker in text for marker in _RECURRENCE_MARKERS):
        return None

    weekdays = _extract_weekdays(text)
    at_time = _extract_time_of_day(text)
    if not weekdays or not at_time:
        return None

    return {
        "weekdays": weekdays,
        "at_time": at_time,
        "starts_on": _extract_boundary(text, "부터") or today_kst_date().isoformat(),
        "ends_on": _extract_boundary(text, "까지"),
    }
