"""환경 변수, 파일 경로, 서비스 공통 상수를 관리합니다."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


# 어느 작업 디렉터리에서 실행해도 같은 데이터 파일을 사용하도록 루트를 고정합니다.
# settings.py 파일 위치를 기준으로 marvis-bot 루트를 찾습니다.
BASE_DIR = Path(__file__).resolve().parent.parent

# /home/ubuntu/marvis-bot/.env
ENV_FILE = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_FILE)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ENV_TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_ALLOWED_USER_ID = os.getenv(
    "TELEGRAM_ALLOWED_USER_ID"
)

# iPhone 단축어(Siri)가 텍스트를 보낼 로컬 전용 웹훅. Cloudflare Tunnel이
# 이 포트만 외부에 중계하므로 기본값은 localhost 바인딩입니다.
SIRI_SHORTCUT_SECRET = os.getenv("SIRI_SHORTCUT_SECRET")
SIRI_WEBHOOK_HOST = os.getenv("SIRI_WEBHOOK_HOST", "127.0.0.1")
SIRI_WEBHOOK_PORT = int(os.getenv("SIRI_WEBHOOK_PORT", "8081"))

MEMORY_FILE = BASE_DIR / "marvis_memory.json"
CONFIG_FILE = BASE_DIR / "marvis_config.json"
PROJECTS_FILE = BASE_DIR / "marvis_projects.json"
VOICE_DIR = BASE_DIR / "voices"
KST = ZoneInfo("Asia/Seoul")

MAX_MEMORY_ITEMS = 500
MAX_IDEA_ITEMS = 150
MAX_RECENT_CONTEXT_ITEMS = 40

# 아침 브리핑을 먼저 보낼 한국 시간 기준 시각입니다.
MORNING_BRIEFING_HOUR = 8
MORNING_BRIEFING_MINUTE = 0


def validate_required_settings() -> None:
    """봇 실행에 반드시 필요한 인증 정보가 있는지 확인합니다."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is missing in .env"
        )

    if not GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY is missing in .env"
        )

    if not TELEGRAM_ALLOWED_USER_ID:
        raise ValueError(
            "TELEGRAM_ALLOWED_USER_ID is missing in .env"
        )

    if not TELEGRAM_ALLOWED_USER_ID.isdigit():
        raise ValueError(
            "TELEGRAM_ALLOWED_USER_ID must be a number"
        )
