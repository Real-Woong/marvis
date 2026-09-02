"""Telegram 애플리케이션을 구성하고 Marvis 서비스를 실행합니다."""

import logging

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from .handlers import (
    done_command,
    forget_all_command,
    handle_text,
    help_command,
    ideas_command,
    memory_command,
    project_update_command,
    projects_command,
    schedule_command,
    start,
)
from .migrate import run_migration_if_needed
from .secretary import format_sync_result
from .secretary import sync as sync_secretary_projects
from .reminders import start_reminder_thread
from .settings import TELEGRAM_BOT_TOKEN, validate_required_settings
from .webhook import start_webhook_server


def main() -> None:
    """설정을 검증하고 알림 작업과 Telegram polling을 시작합니다."""
    validate_required_settings()
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    # 스키마를 만들고, 예전 JSON 데이터가 남아 있으면 1회만 옮깁니다.
    run_migration_if_needed()
    # 켜지자마자 SECRETARY의 _STATUS.md 상태를 한 번 당겨옵니다. 브리핑을
    # 기다리지 않고도 프로젝트 질문에 최신으로 답할 수 있고, 로그 한 줄로
    # 연동이 살아 있는지 바로 보입니다.
    try:
        logging.info("%s", format_sync_result(sync_secretary_projects(force=True)))
    except Exception as error:
        logging.exception("SECRETARY 동기화 실패, 이전 상태로 시작합니다: %s", error)

    start_reminder_thread()
    start_webhook_server()
    # 명령어 핸들러를 먼저 등록하고 마지막에 일반 텍스트를 처리합니다.
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("ideas", ideas_command))
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("project_update", project_update_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("forget_all_marvis", forget_all_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Marvis bot is running with memory, schedule, reminder, and optimization mode...")
    app.run_polling()
