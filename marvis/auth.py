"""개인용 Telegram 봇의 사용자 접근 권한을 검사합니다."""

import logging
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from .settings import TELEGRAM_ALLOWED_USER_ID


def owner_only(handler):
    """허용된 Telegram 사용자만 핸들러를 실행할 수 있게 합니다."""

    @wraps(handler)
    async def wrapped_handler(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        user = update.effective_user

        if user is None:
            logging.warning(
                "Telegram 사용자를 확인할 수 없는 요청입니다."
            )
            return

        if str(user.id) != TELEGRAM_ALLOWED_USER_ID:
            logging.warning(
                "허용되지 않은 Telegram 접근: user_id=%s",
                user.id,
            )

            if update.effective_message:
                await update.effective_message.reply_text(
                    "이 봇은 개인용으로 설정되어 있습니다."
                )

            return

        return await handler(update, context)

    return wrapped_handler