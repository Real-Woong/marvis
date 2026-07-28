"""기억과 설정 데이터를 JSON 파일로 읽고 저장합니다."""

import json
from pathlib import Path

from .settings import CONFIG_FILE, ENV_TELEGRAM_CHAT_ID, MEMORY_FILE
from .time_utils import now_string


def load_json_file(path: Path, default):
    """파일이 없거나 손상됐을 때 기본값을 반환하며 JSON을 읽습니다."""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def save_json_file(path: Path, data) -> None:
    """한글을 그대로 보존해 JSON 데이터를 저장합니다."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_raw_memory() -> list:
    """가공하지 않은 전체 기억 목록을 불러옵니다."""
    data = load_json_file(MEMORY_FILE, [])
    return data if isinstance(data, list) else []


def save_raw_memory(items: list) -> None:
    save_json_file(MEMORY_FILE, items)


def load_config() -> dict:
    data = load_json_file(CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def save_config(config: dict) -> None:
    save_json_file(CONFIG_FILE, config)


def save_chat_id(chat_id: int) -> None:
    """능동 알림을 보낼 최근 Telegram 채팅 ID를 저장합니다."""
    config = load_config()
    config["telegram_chat_id"] = str(chat_id)
    config["updated_at"] = now_string()
    save_config(config)


def get_chat_id() -> str | None:
    """환경 변수 값을 우선으로 사용하고 없으면 저장된 채팅 ID를 반환합니다."""
    if ENV_TELEGRAM_CHAT_ID:
        return ENV_TELEGRAM_CHAT_ID
    chat_id = load_config().get("telegram_chat_id")
    return str(chat_id) if chat_id else None
