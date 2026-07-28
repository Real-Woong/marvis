"""Siri 단축어 등 외부 입력을 텔레그램 메시지와 동일하게 처리하는 로컬 웹훅입니다."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .ai import ask_gemini
from .memory import add_memory
from .reminders import send_proactive_telegram_message
from .schedule_parser import detect_message_intent
from .settings import SIRI_SHORTCUT_SECRET, SIRI_WEBHOOK_HOST, SIRI_WEBHOOK_PORT


def _handle_captured_text(text: str) -> str:
    """handlers.handle_text와 동일한 분류·저장·응답 흐름을 헤드리스로 수행합니다."""
    if detect_message_intent(text) == "save":
        add_memory(text)
    try:
        return ask_gemini(text)
    except Exception:
        logging.exception("Error while handling Siri capture")
        return "답변을 생성하는 중 오류가 발생했습니다."


class _CaptureHandler(BaseHTTPRequestHandler):
    def log_message(self, log_format: str, *args) -> None:
        logging.info("webhook: " + log_format, *args)

    def _respond(self, status: int, payload: dict | None = None) -> None:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # 인증 없이 살아있는지만 확인하는 용도라 비밀 정보를 노출하지 않습니다.
        # 나중에 외부 업타임 모니터링(UptimeRobot 등)을 붙일 때 사용합니다.
        if self.path == "/health":
            self._respond(200, {"ok": True})
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/capture":
            self._respond(404, {"error": "not found"})
            return

        if not SIRI_SHORTCUT_SECRET or self.headers.get("X-Marvis-Secret") != SIRI_SHORTCUT_SECRET:
            logging.warning("Rejected webhook request with invalid or missing secret.")
            self._respond(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(length) if length else b""

        text = ""
        try:
            payload = json.loads(raw_body) if raw_body else {}
            if isinstance(payload, dict):
                text = str(payload.get("text", "")).strip()
        except json.JSONDecodeError:
            text = raw_body.decode("utf-8", errors="ignore").strip()

        if not text:
            self._respond(400, {"error": "text is required"})
            return

        answer = _handle_captured_text(text)
        send_proactive_telegram_message(f"🎙️ Siri로 받은 메시지\n\n{text}\n\n{answer}")
        self._respond(200, {"ok": True, "reply": answer})


def start_webhook_server() -> None:
    """SIRI_SHORTCUT_SECRET이 설정된 경우에만 로컬 전용 웹훅 서버를 시작합니다."""
    if not SIRI_SHORTCUT_SECRET:
        logging.info("SIRI_SHORTCUT_SECRET is not set; Siri capture webhook is disabled.")
        return
    server = ThreadingHTTPServer((SIRI_WEBHOOK_HOST, SIRI_WEBHOOK_PORT), _CaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Siri capture webhook listening on %s:%s", SIRI_WEBHOOK_HOST, SIRI_WEBHOOK_PORT)
