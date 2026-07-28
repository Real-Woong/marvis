"""저장된 알림 시각을 감시하고 Telegram 능동 알림을 발송합니다."""

import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from .memory import load_memory, save_memory
from .settings import KST, TELEGRAM_BOT_TOKEN
from .storage import get_chat_id
from .time_utils import now_kst, now_string


def send_proactive_telegram_message(text: str) -> bool:
    """Telegram Bot API를 직접 호출해 저장된 채팅방으로 메시지를 보냅니다."""
    chat_id = get_chat_id()
    if not chat_id:
        logging.warning("No Telegram chat_id is saved. Cannot send proactive reminder.")
        return False
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("No Telegram bot token is configured.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    try:
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        return True
    except Exception as error:
        logging.exception("Failed to send proactive reminder: %s", error)
        return False


def reminder_loop() -> None:
    """30초마다 미발송 일정을 검사하는 백그라운드 반복 작업입니다."""
    logging.info("Reminder loop started.")
    while True:
        try:
            memories = load_memory()
            current = now_kst()
            changed = False
            # 완료됐거나 이미 알린 일정은 중복 발송하지 않습니다.
            for item in memories:
                if item.get("type") != "schedule" or item.get("done") or item.get("reminded"):
                    continue
                reminder_at = item.get("reminder_at")
                if not reminder_at:
                    continue
                try:
                    reminder_dt = datetime.strptime(reminder_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                except ValueError:
                    continue
                if reminder_dt <= current:
                    message = (
                        "🔔 Marvis Reminder\n\n"
                        "지금 예정된 일정입니다.\n"
                        f"- {item.get('content')}\n\n"
                        f"알림 시각: {reminder_at}"
                    )
                    if send_proactive_telegram_message(message):
                        item["reminded"] = True
                        item["reminded_at"] = now_string()
                        changed = True
            if changed:
                save_memory(memories)
        except Exception as error:
            logging.exception("Reminder loop error: %s", error)
        time.sleep(30)


def start_reminder_thread() -> None:
    """봇 종료를 막지 않는 데몬 스레드에서 알림 루프를 시작합니다."""
    threading.Thread(target=reminder_loop, daemon=True).start()
