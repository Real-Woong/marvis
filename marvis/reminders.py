"""저장된 알림 시각을 감시하고 Telegram 능동 알림을 발송합니다."""

import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .db import log_event, transaction
from .memory import (
    get_due_recurrences,
    get_due_reminders,
    get_schedules_between,
    mark_recurrence_fired,
    mark_reminded,
    skip_stale_recurrences,
)
from .memory import format_weekdays
from .projects import get_briefing_projects
from .secretary import format_sync_result
from .secretary import sync as sync_secretary_projects
from .settings import TELEGRAM_BOT_TOKEN
from .storage import get_chat_id, get_last_briefing_date, save_last_briefing_date
from .time_utils import now_kst, now_string
from .voice import split_for_telegram

# 브리핑은 한 통입니다.
#
# 예전에는 인사 한 통에 이어 프로젝트마다 한 통씩(8개면 아홉 통) 5초 간격으로
# 보냈습니다. Siri "알림 읽어주기"가 메시지 하나를 끊지 않고 읽어주게 하려던
# 것인데, 음성을 쓰지 않게 되면서 남은 것은 아침마다 쌓이는 알림 아홉 개뿐이라
# 정작 무엇이 왔는지 알아보기 어려웠습니다.
_WEEKDAY_NAMES = "월화수목금토일"

# 알림 시각이 없는 일정. 시각 칸을 비우면 줄이 어긋나서 눈에 안 들어옵니다.
_NO_TIME = "--:--"

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


def send_proactive_long_message(text: str) -> bool:
    """상한을 넘는 브리핑도 잘리지 않게 나눠 보냅니다.

    sendMessage는 4096자를 넘으면 400을 돌려주고, 그러면 그날 브리핑은
    통째로 사라집니다. 프로젝트가 서른 개까지 늘어난 지금은 상한에 닿을 수
    있는 길이입니다. 첫 조각이 실패하면 보낸 것으로 치지 않습니다.
    """
    chunks = split_for_telegram(text)
    if not chunks:
        return False
    if not send_proactive_telegram_message(chunks[0]):
        return False
    for chunk in chunks[1:]:
        send_proactive_telegram_message(chunk)
    return True


def _one_line(text: str) -> str:
    """여러 줄짜리 내용을 브리핑 한 줄에 담습니다."""
    return " ".join((text or "").split())


def format_today_schedule_lines(today) -> list[str]:
    """오늘 날짜로 저장된 일정을 시각 순으로 늘어놓습니다.

    LLM에게 한 문장으로 요약시키지 않는 이유: 요약은 저장된 것과 달라질 수
    있고, 모델이 503을 내면 브리핑 자체가 나가지 않았습니다(2026-09-10).
    저장된 값을 그대로 옮기면 둘 다 일어나지 않습니다.
    """
    stamp = today.isoformat()
    items = get_schedules_between(stamp, stamp)
    if not items:
        return ["  오늘 일정 없음"]

    lines = []
    for item in items:
        reminder = item.get("reminder_at")
        at = reminder[11:16] if reminder else _NO_TIME
        lines.append(f"  {at}  {_one_line(item['content'])}")
    return lines


def format_briefing_project_lines() -> list[str]:
    """브리핑에 실을 진행중 프로젝트와 다음 할 일."""
    projects = get_briefing_projects()
    if not projects:
        return ["  진행중인 프로젝트 없음"]
    return [
        f"  - {project['name']}: {_one_line(project.get('next_steps')) or '다음 할 일 미정'}"
        for project in projects
    ]


def build_briefing_message(current: datetime) -> str:
    """하루치 브리핑 전체를 메시지 한 통으로 만듭니다."""
    today = current.date()
    weekday = _WEEKDAY_NAMES[today.weekday()]
    return "\n".join(
        [f"📋 {today.isoformat()} ({weekday}) 브리핑", "", "[오늘 일정]"]
        + format_today_schedule_lines(today)
        + ["", "[프로젝트]"]
        + format_briefing_project_lines()
    )


def briefing_time_for(current: datetime) -> tuple[int, int] | None:
    """요일별 브리핑 예정 시각. 브리핑을 보내지 않는 날이면 None."""
    weekday = current.weekday()  # 월=0 ... 일=6
    if weekday == 6:
        return None  # 일요일은 브리핑 없음
    if weekday == 5:
        return (10, 0)  # 토요일
    return (8, 30)  # 월~금


def send_morning_briefing_if_due(current: datetime) -> None:
    """예정 시각이 지났고 오늘 아직 안 보냈다면 아침 브리핑을 한 통 보냅니다."""
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

    projects = get_briefing_projects()
    if not send_proactive_long_message(build_briefing_message(current)):
        # 보내지 못했으면 오늘 보낸 것으로 적지 않습니다. 다음 순회에서
        # (창 안이라면) 다시 시도합니다.
        return

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

            # 반복 규칙. 단발 일정과 달리 한 줄이 매번 다시 울립니다.
            for rule in get_due_recurrences(current):
                message = (
                    "🔔 Marvis Reminder (반복)\n\n"
                    f"- {rule['content']}\n\n"
                    f"규칙: [R{rule['seq']}] {format_weekdays(rule['weekdays'])}"
                    f" {rule['at_time']}"
                )
                if not send_proactive_telegram_message(message):
                    continue
                # 보낸 뒤에 표시합니다. 전송이 실패했는데 표시부터 하면
                # 그날 알림은 영영 오지 않습니다.
                mark_recurrence_fired(rule["id"], current.date().isoformat())

            # 봇이 꺼져 있던 사이에 지나간 오늘치 규칙은 '건너뜀'으로 적어
            # 둡니다. 오후 3시에 05:10 알림을 보내지 않기 위해서입니다.
            skip_stale_recurrences(current)
        except Exception as error:
            logging.exception("Reminder loop error: %s", error)
        time.sleep(30)


def start_reminder_thread() -> None:
    """봇 종료를 막지 않는 데몬 스레드에서 알림 루프를 시작합니다."""
    threading.Thread(target=reminder_loop, daemon=True).start()
