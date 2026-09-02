"""Marvis가 모든 날짜와 시간을 한국 표준시로 처리하도록 돕습니다."""

from datetime import datetime

from .settings import KST

# eval과 테스트가 시각을 고정할 때만 채워집니다. 운영 중에는 항상 None입니다.
# 골든셋이 "2026-09-08" 같은 절대 날짜를 정답으로 쓰려면, 그 케이스를 재생할 때
# '오늘'이 고정돼야 하기 때문입니다.
_frozen_now: datetime | None = None


def freeze_clock(moment: datetime | None) -> None:
    """현재 시각을 고정합니다. None을 주면 실제 시계로 되돌립니다."""
    global _frozen_now
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    _frozen_now = moment


def now_kst() -> datetime:
    """시간대 정보가 포함된 현재 한국 시간을 반환합니다."""
    if _frozen_now is not None:
        return _frozen_now
    return datetime.now(KST)


def today_kst_date():
    """한국 시간을 기준으로 오늘 날짜를 반환합니다."""
    return now_kst().date()


def now_string() -> str:
    """저장에 사용하는 고정 형식의 현재 시각 문자열을 반환합니다."""
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


def iso_datetime_kst(dt: datetime) -> str:
    """datetime을 한국 시간의 저장 형식으로 변환합니다."""
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
