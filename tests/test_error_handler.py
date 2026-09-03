"""오류 핸들러 회귀 테스트.

이게 없으면 명령 핸들러에서 난 예외가 로그에만 남고 보낸 사람에게는
침묵으로 보입니다. 봇이 씹은 것과 구별이 안 되는 상태가 제일 나쁩니다.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("MARVIS_DB_FILE", str(Path(_TMP_DIR.name) / "errors.db"))
os.environ.setdefault(
    "MARVIS_SECRETARY_DIR", str(Path(_TMP_DIR.name) / "no-secretary")
)
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ["MARVIS_ROUTER"] = "regex"

_genai = types.ModuleType("google.generativeai")
_genai.configure = lambda **kwargs: None
_genai.GenerativeModel = lambda name: None
_google = types.ModuleType("google")
_google.generativeai = _genai
sys.modules.setdefault("google", _google)
sys.modules.setdefault("google.generativeai", _genai)

from telegram.error import NetworkError, TimedOut  # noqa: E402

from marvis.handlers import on_error  # noqa: E402


class FakeMessage:
    """reply_text만 흉내 냅니다. 실패하도록 만들 수도 있어야 합니다."""

    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def reply_text(self, text):
        if self.fail:
            raise RuntimeError("텔레그램에 보낼 수 없음")
        self.sent.append(text)


class FakeUpdate:
    def __init__(self, message):
        self.effective_message = message


class FakeContext:
    def __init__(self, error):
        self.error = error


class ErrorHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_사용자에게_실패를_알린다(self):
        message = FakeMessage()
        with self.assertLogs(level="ERROR"):
            await on_error(FakeUpdate(message), FakeContext(ValueError("깨짐")))

        self.assertEqual(len(message.sent), 1)
        # 저장됐다고 단정하면 안 됩니다. 실제로 뭐가 저장됐는지 모릅니다.
        self.assertIn("오류", message.sent[0])
        self.assertIn("저장되지 않았을 수 있습니다", message.sent[0])

    async def test_일시적_통신오류는_한_줄로만_남긴다(self):
        # polling 루프에서 나는 502입니다. Update가 아니라 답할 곳도 없습니다.
        for error in (NetworkError("Bad Gateway"), TimedOut()):
            with self.subTest(error=type(error).__name__):
                with self.assertLogs(level="WARNING") as logs:
                    await on_error(object(), FakeContext(error))
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(logs.records[0].levelname, "WARNING")
                self.assertIsNone(logs.records[0].exc_info)

    async def test_대화방이_있으면_통신오류도_알린다(self):
        # 사용자 메시지를 처리하다 난 NetworkError는 침묵시키면 안 됩니다.
        message = FakeMessage()
        with self.assertLogs(level="ERROR"):
            await on_error(FakeUpdate(message), FakeContext(NetworkError("끊김")))
        self.assertEqual(len(message.sent), 1)

    async def test_답장할_곳이_없으면_조용히_끝낸다(self):
        with self.assertLogs(level="ERROR"):
            await on_error(object(), FakeContext(ValueError("깨짐")))

    async def test_안내_전송이_실패해도_예외를_올리지_않는다(self):
        # 여기서 예외가 새면 오류 핸들러가 자기 자신을 다시 부릅니다.
        message = FakeMessage(fail=True)
        with self.assertLogs(level="ERROR"):
            await on_error(FakeUpdate(message), FakeContext(ValueError("깨짐")))
        self.assertEqual(message.sent, [])


if __name__ == "__main__":
    unittest.main()
