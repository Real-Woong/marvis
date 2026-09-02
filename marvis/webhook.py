"""Siri 단축어 등 외부 입력을 텔레그램 메시지와 동일하게 처리하는 로컬 웹훅입니다."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import SOURCE_SIRI, handle_message
from .reminders import send_proactive_telegram_message
from .settings import SIRI_SHORTCUT_SECRET, SIRI_WEBHOOK_HOST, SIRI_WEBHOOK_PORT


def _handle_captured_text(text: str) -> str:
    """텔레그램과 완전히 같은 core 경로를 헤드리스로 실행합니다.

    이제 Siri 경로에서도 `!스케쥴`과 프로젝트 명령이 동작합니다. 예전에는
    이 함수가 흐름을 따로 구현하고 있어서 두 경로가 서로 어긋나 있었습니다.
    "생각 중입니다..." 같은 진행 안내(ack)는 Siri가 읽을 필요가 없으므로
    빼고 실제 답변만 돌려줍니다.
    """
    try:
        replies = [reply for reply in handle_message(text, source=SOURCE_SIRI) if not reply.ack]
    except Exception:
        logging.exception("Error while handling Siri capture")
        return "답변을 생성하는 중 오류가 발생했습니다."

    if not replies:
        return "처리했습니다."
    return "\n\n".join(reply.text for reply in replies)


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
