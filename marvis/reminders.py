"""저장된 알림 시각을 감시하고 Telegram 능동 알림을 발송합니다."""

import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from .ai import generate_morning_briefing
from .memory import load_memory, save_memory
from .projects import get_active_projects
from .settings import KST, MORNING_BRIEFING_HOUR, MORNING_BRIEFING_MINUTE, TELEGRAM_BOT_TOKEN
from .storage import get_chat_id, get_last_briefing_date, memory_lock, save_last_briefing_date
from .time_utils import now_kst, now_string

# Siri "알림 읽어주기"가 메시지 하나를 다 읽어주도록, 프로젝트 현황은 스케쥴과
# 합치지 않고 프로젝트당 별도 메시지로 몇 초 간격을 두고 보낸다.
PROJECT_MESSAGE_INTERVAL_SECONDS = 3


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


def send_morning_briefing_if_due(current: datetime) -> None:
    """설정된 시각이 지났고 오늘 아직 안 보냈다면 아침 브리핑을 생성해 보냅니다."""
    today = current.date().isoformat()
    if get_last_briefing_date() == today:
        return
    if (current.hour, current.minute) < (MORNING_BRIEFING_HOUR, MORNING_BRIEFING_MINUTE):
        return
    try:
        briefing = generate_morning_briefing()
    except Exception as error:
        logging.exception("Failed to generate morning briefing: %s", error)
        return

    message = f"좋은 아침입니다 마비스 매니저입니다. {briefing}"
    if not send_proactive_telegram_message(message):
        return

    for project in get_active_projects():
        time.sleep(PROJECT_MESSAGE_INTERVAL_SECONDS)
        next_steps = project.get("next_steps") or "다음 할 일 미정"
        send_proactive_telegram_message(f"{project['name']}: {next_steps}")

    save_last_briefing_date(today)


def reminder_loop() -> None:
    """30초마다 미발송 일정과 아침 브리핑 조건을 검사하는 백그라운드 반복 작업입니다."""
    logging.info("Reminder loop started.")
    while True:
        try:
            current = now_kst()
            send_morning_briefing_if_due(current)

            # 파일을 읽는 동안만 잠그고, 네트워크 전송(최대 10초)은 락 밖에서
            # 수행해 텔레그램 핸들러가 그동안 기억 파일에 접근하지 못하는
            # 상황을 피합니다.
            with memory_lock:
                memories = load_memory()
            due_items = []
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
                    due_items.append(item)

            for item in due_items:
                message = (
                    "🔔 Marvis Reminder\n\n"
                    "지금 예정된 일정입니다.\n"
                    f"- {item.get('content')}\n\n"
                    f"알림 시각: {item.get('reminder_at')}"
                )
                if not send_proactive_telegram_message(message):
                    continue
                # 전송에 성공한 항목만, 그 사이 다른 스레드가 저장했을 수도 있는
                # 최신 상태를 다시 읽어서 반영합니다.
                with memory_lock:
                    fresh_memories = load_memory()
                    for fresh_item in fresh_memories:
                        if fresh_item.get("id") == item.get("id"):
                            fresh_item["reminded"] = True
                            fresh_item["reminded_at"] = now_string()
                            break
                    save_memory(fresh_memories)
        except Exception as error:
            logging.exception("Reminder loop error: %s", error)
        time.sleep(30)


def start_reminder_thread() -> None:
    """봇 종료를 막지 않는 데몬 스레드에서 알림 루프를 시작합니다."""
    threading.Thread(target=reminder_loop, daemon=True).start()
