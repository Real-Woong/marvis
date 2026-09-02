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

# Phase 0부터 모든 상태는 SQLite에 저장합니다. 아래 JSON 경로들은 최초 1회
# 마이그레이션(marvis/migrate.py)에서 읽기 전용으로만 사용합니다.
# MARVIS_DB_FILE로 다른 경로를 지정할 수 있습니다(테스트, 별도 인스턴스 운영).
DB_FILE = Path(os.getenv("MARVIS_DB_FILE") or (BASE_DIR / "marvis.db"))

LEGACY_MEMORY_FILE = BASE_DIR / "marvis_memory.json"
LEGACY_CONFIG_FILE = BASE_DIR / "marvis_config.json"
LEGACY_PROJECTS_FILE = BASE_DIR / "marvis_projects.json"

VOICE_DIR = BASE_DIR / "voices"

# SECRETARY(프로젝트 상태 수집기)는 같은 맥미니의 형제 디렉터리에 있습니다.
#   ~/Desktop/project/SECRETARY
#   ~/Desktop/project/Project_AI/LLM/Marvis   <- BASE_DIR
# 다른 위치에 두거나 테스트에서 갈아끼울 때는 MARVIS_SECRETARY_DIR을 씁니다.
SECRETARY_DIR = Path(
    os.getenv("MARVIS_SECRETARY_DIR") or (BASE_DIR.parents[2] / "SECRETARY")
)
KST = ZoneInfo("Asia/Seoul")

# 프롬프트에 실어 보낼 최근 기억 개수. (예전의 MAX_MEMORY_ITEMS /
# MAX_IDEA_ITEMS는 JSON 파일 크기를 줄이려고 오래된 항목을 삭제하는 용도라
# SQLite로 옮기면서 없앴습니다. 이제 아무것도 지우지 않습니다.)
MAX_RECENT_CONTEXT_ITEMS = 40
MAX_IDEA_CONTEXT_ITEMS = 20

# 라우터 모드.
#   regex  - 정규식만 (Phase 0까지의 동작)
#   shadow - 정규식이 사용자에게 응답하고, LLM은 나란히 돌며 로그에만 남긴다
#   llm    - LLM 라우터가 응답한다 (전환 완료)
# 기본값이 shadow인 이유: 배포해도 사용자 경험이 바뀌지 않고, 되돌리려면
# 환경 변수 하나만 바꾸면 되기 때문입니다.
ROUTER_MODE = os.getenv("MARVIS_ROUTER", "shadow").strip().lower()

LLM_PROVIDER = os.getenv("MARVIS_LLM_PROVIDER", "gemini").strip().lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5").strip()


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
