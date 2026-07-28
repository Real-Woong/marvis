"""Marvis가 모든 날짜와 시간을 한국 표준시로 처리하도록 돕습니다."""

from datetime import datetime

from .settings import KST


def now_kst() -> datetime:
    """시간대 정보가 포함된 현재 한국 시간을 반환합니다."""
    return datetime.now(KST)


def today_kst_date():
    """한국 시간을 기준으로 오늘 날짜를 반환합니다."""
    return now_kst().date()


def now_string() -> str:
    """JSON 저장에 사용하는 고정 형식의 현재 시각 문자열을 반환합니다."""
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


def iso_datetime_kst(dt: datetime) -> str:
    """datetime을 한국 시간의 저장 형식으로 변환합니다."""
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
