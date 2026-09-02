"""저장된 알림 시각을 감시하고 Telegram 능동 알림을 발송합니다."""

import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .ai import generate_morning_briefing
from .db import log_event, transaction
from .memory import get_due_reminders, mark_reminded
from .projects import get_briefing_projects, to_speech_friendly_name
from .secretary import format_sync_result
from .secretary import sync as sync_secretary_projects
from .settings import TELEGRAM_BOT_TOKEN
from .storage import get_chat_id, get_last_briefing_date, save_last_briefing_date
from .time_utils import now_kst, now_string

# Siri "알림 읽어주기"가 메시지 하나를 다 읽어주도록, 프로젝트 현황은 스케쥴과
# 합치지 않고 프로젝트당 별도 메시지로 몇 초 간격을 두고 보낸다.
PROJECT_MESSAGE_INTERVAL_SECONDS = 5

# 브리핑 예정 시각을 이만큼 넘겨서 켜졌다면 "좋은 아침"을 보내지 않습니다.
BRIEFING_WINDOW = timedelta(hours=2)


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


def briefing_time_for(current: datetime) -> tuple[int, int] | None:
    """요일별 브리핑 예정 시각. 브리핑을 보내지 않는 날이면 None."""
    weekday = current.weekday()  # 월=0 ... 일=6
    if weekday == 6:
        return None  # 일요일은 브리핑 없음
    if weekday == 5:
        return (10, 0)  # 토요일
    return (8, 30)  # 월~금


def send_morning_briefing_if_due(current: datetime) -> None:
    """예정 시각이 지났고 오늘 아직 안 보냈다면 아침 브리핑을 생성해 보냅니다."""
    today = current.date().isoformat()
    if get_last_briefing_date() == today:
        return

    scheduled = briefing_time_for(current)
    if scheduled is None:
        return

    scheduled_at = current.replace(
        hour=scheduled[0], minute=scheduled[1], second=0, microsecond=0
    )
    if current < scheduled_at:
        return

    # 봇이 아침에 꺼져 있다가 한참 뒤에 켜진 경우입니다. 오후에 "좋은 아침입니다"를
    # 보내는 대신 오늘 브리핑은 건너뛴 것으로 기록합니다.
    if current > scheduled_at + BRIEFING_WINDOW:
        logging.info("Briefing window for %s has passed; skipping.", today)
        save_last_briefing_date(today)
        with transaction() as tx:
            log_event(
                tx, "briefing.skipped", entity="system", source="reminder_loop",
                payload={"date": today, "reason": "window_passed"},
            )
        return

    # 프로젝트 상태를 읽기 직전에 SECRETARY 색인에서 당겨옵니다. 28개 기준
    # 0.1초라 미리 만들어 둘 필요가 없고, 손으로 입력할 일도 없어집니다.
    try:
        logging.info("%s", format_sync_result(sync_secretary_projects()))
    except Exception as error:
        # 동기화가 실패해도 어제까지의 상태로 브리핑은 나가야 합니다.
        logging.exception("SECRETARY 동기화 실패, 이전 상태로 브리핑합니다: %s", error)

    try:
        briefing = generate_morning_briefing()
    except Exception as error:
        logging.exception("Failed to generate morning briefing: %s", error)
        return

    message = f"좋은 아침입니다 마비스 매니저입니다. {briefing}"
    if not send_proactive_telegram_message(message):
        return

    projects = get_briefing_projects()
    for project in projects:
        time.sleep(PROJECT_MESSAGE_INTERVAL_SECONDS)
        next_steps = project.get("next_steps") or "다음 할 일 미정"
        speech_name = to_speech_friendly_name(project["name"])
        send_proactive_telegram_message(f"{speech_name}: {next_steps}")

    save_last_briefing_date(today)
    with transaction() as tx:
        log_event(
            tx, "briefing.sent", entity="system", source="reminder_loop",
            payload={"date": today, "projects": len(projects)},
        )


def reminder_loop() -> None:
    """30초마다 미발송 일정과 아침 브리핑 조건을 검사하는 백그라운드 반복 작업입니다."""
    logging.info("Reminder loop started.")
    while True:
        try:
            current = now_kst()
            send_morning_briefing_if_due(current)

            for item in get_due_reminders(now_string()):
                message = (
                    "🔔 Marvis Reminder\n\n"
                    "지금 예정된 일정입니다.\n"
                    f"- {item['content']}\n\n"
                    f"알림 시각: {item['reminder_at']}"
                )
                if not send_proactive_telegram_message(message):
                    continue
                # UUID로 표시하므로, 그사이 다른 항목이 보관 처리돼도 엉뚱한
                # 항목에 표시가 찍히지 않습니다.
                mark_reminded(item["id"])
        except Exception as error:
            logging.exception("Reminder loop error: %s", error)
        time.sleep(30)


def start_reminder_thread() -> None:
    """봇 종료를 막지 않는 데몬 스레드에서 알림 루프를 시작합니다."""
    threading.Thread(target=reminder_loop, daemon=True).start()
