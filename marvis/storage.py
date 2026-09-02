"""설정 값(채팅 ID, 마지막 브리핑 날짜)을 SQLite에 읽고 씁니다.

예전에는 JSON 파일과 전역 `memory_lock`을 함께 썼습니다. 이제 동시성은
SQLite 트랜잭션이 처리하므로 락은 없습니다.
"""

from .db import get_connection, transaction
from .settings import ENV_TELEGRAM_CHAT_ID
from .time_utils import now_string


def get_setting(key: str, default: str | None = None) -> str | None:
    row = get_connection().execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with transaction() as tx:
        tx.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, str(value), now_string()),
        )


def save_chat_id(chat_id: int) -> None:
    """능동 알림을 보낼 최근 Telegram 채팅 ID를 저장합니다."""
    if get_setting("telegram_chat_id") == str(chat_id):
        return
    set_setting("telegram_chat_id", str(chat_id))


def get_chat_id() -> str | None:
    """환경 변수 값을 우선으로 사용하고 없으면 저장된 채팅 ID를 반환합니다."""
    if ENV_TELEGRAM_CHAT_ID:
        return ENV_TELEGRAM_CHAT_ID
    return get_setting("telegram_chat_id")


def get_last_briefing_date() -> str | None:
    """마지막으로 아침 브리핑을 처리한 날짜(YYYY-MM-DD)를 반환합니다."""
    return get_setting("last_briefing_date")


def save_last_briefing_date(date_str: str) -> None:
    """봇 재시작 후에도 같은 날 브리핑이 중복 발송되지 않도록 날짜를 기록합니다."""
    set_setting("last_briefing_date", date_str)
