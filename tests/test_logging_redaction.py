"""로그에 비밀값이 남지 않는지 확인합니다.

2026-09-04 에 bot.error.log 2.5MB 안에서 텔레그램 봇 토큰이 평문으로
15,000번 가까이 발견됐습니다. httpx 가 INFO 로 요청 URL 전체를 찍었고,
텔레그램 Bot API 는 토큰을 URL 경로에 담기 때문입니다.
"""

import io
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = tempfile.TemporaryDirectory()
os.environ["MARVIS_DB_FILE"] = str(Path(_TMP_DIR.name) / "logging.db")
os.environ["MARVIS_SECRETARY_DIR"] = str(Path(_TMP_DIR.name) / "no-secretary")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

_genai = types.ModuleType("google.generativeai")
_genai.configure = lambda **kwargs: None
_google = types.ModuleType("google")
_google.generativeai = _genai
sys.modules.setdefault("google", _google)

from marvis import logging_setup  # noqa: E402
from marvis.logging_setup import (  # noqa: E402
    NOISY_LOGGERS,
    REDACTED,
    RedactingFormatter,
    configure_logging,
    redact,
)

# 진짜 토큰과 같은 모양이지만 아무 데도 쓰이지 않는 값입니다.
FAKE_TOKEN = "8755506609:AAFimBqkPaOK21brVg7mLPV5bsx28wJ0Rqc"
LEAKING_LINE = (
    'HTTP Request: POST https://api.telegram.org/bot%s/getUpdates "HTTP/1.1 200 OK"'
)


def _capture(secrets, emit) -> str:
    """포매터를 걸어 둔 로거로 emit 을 실행하고 나온 문자열을 돌려줍니다."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(RedactingFormatter("%(levelname)s - %(message)s", secrets))
    logger = logging.getLogger(f"redaction-test-{id(emit)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    emit(logger)
    return buffer.getvalue()


class RedactingFormatterTest(unittest.TestCase):
    def test_the_exact_line_that_leaked_is_redacted(self):
        output = _capture(
            [FAKE_TOKEN], lambda log: log.info(LEAKING_LINE, FAKE_TOKEN)
        )
        self.assertNotIn(FAKE_TOKEN, output)
        self.assertIn(REDACTED, output)
        # 나머지 정보는 그대로 읽혀야 합니다.
        self.assertIn("getUpdates", output)
        self.assertIn("200 OK", output)

    def test_a_secret_inside_a_traceback_is_redacted(self):
        """필터가 아니라 포매터에 건 이유입니다. 필터는 트레이스백을 못 봅니다."""

        def emit(log):
            try:
                raise RuntimeError(f"calling https://api.telegram.org/bot{FAKE_TOKEN}/x")
            except RuntimeError:
                log.exception("호출 실패")

        output = _capture([FAKE_TOKEN], emit)
        self.assertNotIn(FAKE_TOKEN, output)
        self.assertIn("Traceback", output)

    def test_a_token_shaped_string_we_do_not_know_is_still_redacted(self):
        """토큰을 바꾼 직후, 설정값과 로그에 흐르는 값이 다를 수 있습니다."""
        other = "9999999999:AAAAbbbbCCCCddddEEEEffffGGGGhhhhiii"
        output = _capture(
            [FAKE_TOKEN],
            lambda log: log.warning("stale https://api.telegram.org/bot%s/getMe", other),
        )
        self.assertNotIn(other, output)
        self.assertIn(REDACTED, output)

    def test_secrets_in_log_arguments_are_redacted(self):
        output = _capture(
            [FAKE_TOKEN], lambda log: log.error("key=%s", FAKE_TOKEN)
        )
        self.assertNotIn(FAKE_TOKEN, output)

    def test_short_values_are_not_redacted(self):
        """흔한 짧은 문자열을 가리면 로그를 못 읽게 됩니다."""
        output = _capture(["abc"], lambda log: log.info("abc 는 그대로 보여야 합니다"))
        self.assertIn("abc", output)
        self.assertNotIn(REDACTED, output)

    def test_ordinary_lines_are_untouched(self):
        output = _capture(
            [FAKE_TOKEN], lambda log: log.info("알림 3건을 보냈습니다")
        )
        self.assertIn("알림 3건을 보냈습니다", output)
        self.assertNotIn(REDACTED, output)


class RedactHelperTest(unittest.TestCase):
    def test_redact_handles_token_shaped_text(self):
        text = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
        self.assertNotIn(FAKE_TOKEN, redact(text))

    def test_redact_leaves_clean_text_alone(self):
        self.assertEqual(redact("아무 비밀도 없는 문장"), "아무 비밀도 없는 문장")


class ConfigureLoggingTest(unittest.TestCase):
    def setUp(self):
        self._root_handlers = logging.getLogger().handlers[:]
        self._root_level = logging.getLogger().level
        self._noisy = {
            name: logging.getLogger(name).level for name in NOISY_LOGGERS
        }

    def tearDown(self):
        root = logging.getLogger()
        root.handlers = self._root_handlers
        root.setLevel(self._root_level)
        for name, level in self._noisy.items():
            logging.getLogger(name).setLevel(level)

    def test_url_logging_libraries_are_raised_to_warning(self):
        configure_logging()
        for name in NOISY_LOGGERS:
            self.assertGreaterEqual(
                logging.getLogger(name).getEffectiveLevel(), logging.WARNING, name
            )

    def test_httpx_info_produces_nothing(self):
        """레벨을 올리는 것이 1차 방어입니다. 아예 안 찍히는 게 가장 좋습니다."""
        configure_logging()
        buffer = io.StringIO()
        handler = logging.StreamHandler(buffer)
        handler.setFormatter(RedactingFormatter("%(message)s", []))
        logging.getLogger().handlers = [handler]

        logging.getLogger("httpx").info(LEAKING_LINE, FAKE_TOKEN)
        self.assertEqual(buffer.getvalue(), "")

    def test_our_own_logs_still_come_through(self):
        configure_logging()
        buffer = io.StringIO()
        handler = logging.StreamHandler(buffer)
        handler.setFormatter(RedactingFormatter("%(message)s", []))
        logging.getLogger().handlers = [handler]

        logging.getLogger("marvis.reminders").info("알림을 보냈습니다")
        self.assertIn("알림을 보냈습니다", buffer.getvalue())

    def test_calling_twice_does_not_stack_handlers(self):
        """launchd 가 재시작할 때마다 같은 줄이 여러 번 찍히면 안 됩니다."""
        configure_logging()
        first = len(logging.getLogger().handlers)
        configure_logging()
        self.assertEqual(len(logging.getLogger().handlers), first)


class AppUsesTheSafeSetupTest(unittest.TestCase):
    def test_app_does_not_call_basic_config(self):
        """basicConfig 로 되돌아가면 토큰이 다시 새기 시작합니다."""
        source = (Path(__file__).resolve().parent.parent / "marvis" / "app.py").read_text(
            encoding="utf-8"
        )
        # 호출을 찾습니다. 그냥 "basicConfig" 를 찾으면 왜 안 쓰는지 적어 둔
        # 주석에까지 걸립니다.
        self.assertNotIn("logging.basicConfig(", source)
        self.assertIn("configure_logging()", source)


if __name__ == "__main__":
    unittest.main()
