"""로그 설정. 비밀값이 로그 파일에 남지 않게 하는 것이 이 모듈의 목적입니다.

왜 따로 두는가:

`logging.basicConfig(level=INFO)` 는 루트 로거를 INFO 로 엽니다. 그러면
httpx 가 자기 INFO 로그로 **요청 URL 전체**를 찍는데, 텔레그램 Bot API 는
토큰을 경로에 담습니다.

    HTTP Request: POST https://api.telegram.org/bot<토큰>/getUpdates "200 OK"

폴링이 10초마다 도니까 이 줄이 하루에 8,000번 넘게 쌓입니다. 실제로
bot.error.log 2.5MB 가 거의 전부 이것이었고, 토큰이 15,000번 가까이
평문으로 들어 있었습니다.

두 겹으로 막습니다.

1. 시끄러운 서드파티 로거의 레벨을 올려서 애초에 안 찍히게 한다.
2. 그래도 새는 것에 대비해, 포매터가 최종 문자열에서 비밀값을 지운다.

2번이 있어야 하는 이유: 레벨을 올려도 WARNING·ERROR 는 여전히 나가고,
예외 트레이스백 안에 URL 이 들어 있을 수 있습니다. 필터가 아니라 포매터에
붙이는 것도 같은 이유입니다 — 포매터는 메시지·인자·트레이스백이 모두 합쳐진
뒤의 문자열을 보므로, 어디로 들어온 비밀값이든 한 곳에서 지울 수 있습니다.
"""

import logging
import re

from .settings import (
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    SIRI_SHORTCUT_SECRET,
    TELEGRAM_BOT_TOKEN,
)

REDACTED = "***REDACTED***"

# 요청 URL 을 통째로 찍는 라이브러리들. 이 로그가 알려주는 것은 "요청이
# 나갔다" 뿐인데, 그 대가로 토큰을 파일에 적습니다. 문제가 생기면 WARNING
# 이상으로 올라오므로 진단에도 지장이 없습니다.
NOISY_LOGGERS = ("httpx", "httpcore", "google_genai", "urllib3")

# 설정에서 읽은 값과 무관하게, 텔레그램 토큰 모양을 한 것은 전부 지웁니다.
# 토큰을 바꾼 직후처럼 .env 의 값과 로그에 흐르는 값이 다를 수 있습니다.
_TOKEN_SHAPED = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")


class RedactingFormatter(logging.Formatter):
    """포맷이 끝난 문자열에서 알려진 비밀값을 지웁니다."""

    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        # 짧은 값은 지우지 않습니다. 흔한 문자열을 통째로 가려서 로그를
        # 못 읽게 만드는 편이 더 나쁩니다.
        self._secrets = sorted(
            {secret for secret in secrets if secret and len(secret) >= 12},
            key=len,
            reverse=True,
        )

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return _TOKEN_SHAPED.sub(f"bot{REDACTED}", text)


def redact(text: str) -> str:
    """로그가 아닌 곳(사용자 메시지, 예외 문구)에서 쓸 수 있는 같은 규칙."""
    for secret in (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, ANTHROPIC_API_KEY,
                   SIRI_SHORTCUT_SECRET):
        if secret and len(secret) >= 12 and secret in text:
            text = text.replace(secret, REDACTED)
    return _TOKEN_SHAPED.sub(f"bot{REDACTED}", text)


def configure_logging(level: int = logging.INFO) -> None:
    """루트 로거를 세우고 비밀값 지우기를 건다.

    basicConfig 대신 직접 핸들러를 답니다. basicConfig 는 포매터를
    갈아끼울 자리를 주지 않습니다.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            [TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, ANTHROPIC_API_KEY,
             SIRI_SHORTCUT_SECRET],
        )
    )

    root = logging.getLogger()
    # 재실행(테스트)에서 핸들러가 겹쳐 쌓이지 않게 합니다.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
