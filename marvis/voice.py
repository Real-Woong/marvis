"""AI 답변을 한국어 음성으로 변환하고 Telegram으로 전송합니다."""

import logging
import uuid
from pathlib import Path

from gtts import gTTS
from telegram import Update

from .settings import VOICE_DIR


def make_voice_file(text: str) -> Path:
    """응답 텍스트를 한국어 MP3 파일로 생성합니다.

    파일명에 UUID를 넣습니다. 예전에는 항상 `voices/reply.mp3` 한 파일에 써서,
    요청이 겹치면 다른 요청의 음성이 전송될 수 있었습니다.
    """
    VOICE_DIR.mkdir(exist_ok=True)
    file_path = VOICE_DIR / f"reply-{uuid.uuid4().hex}.mp3"
    gTTS(text=text, lang="ko").save(str(file_path))
    return file_path


async def send_text_and_voice(update: Update, text: str) -> None:
    """동일한 답변을 Telegram 텍스트와 오디오로 차례대로 보냅니다."""
    await update.message.reply_text(text)

    voice_path = None
    try:
        voice_path = make_voice_file(text)
        with voice_path.open("rb") as audio:
            await update.message.reply_audio(audio=audio, title="Marvis reply")
    except Exception:
        # 음성 생성이 실패해도 텍스트 답변은 이미 전달됐으므로 조용히 넘어갑니다.
        logging.exception("Failed to send the voice reply")
    finally:
        if voice_path is not None:
            voice_path.unlink(missing_ok=True)
