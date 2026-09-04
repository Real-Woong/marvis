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


# 텔레그램 sendMessage 한 건의 상한입니다. 넘기면 API가 400 Bad Request
# ("Message is too long")를 돌려주고, 그 예외가 핸들러까지 올라가 사용자에게는
# "처리하는 중 오류가 발생했습니다"만 갑니다. 실제로는 답변이 만들어져
# 있었는데 전달만 못 한 것입니다.
TELEGRAM_MESSAGE_LIMIT = 4096

# 음성으로 만들 최대 길이. 이동 중에 듣는 용도라 그 이상은 의미가 없습니다.
MAX_VOICE_CHARS = 1200


def split_for_telegram(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """긴 답변을 전송 가능한 조각으로 나눕니다.

    줄 경계를 우선 지킵니다. 일정 목록이 줄 단위라, 한 줄이 두 메시지에
    걸쳐 잘리면 읽기 어려워집니다. 한 줄이 혼자 상한을 넘으면 그때만
    글자 수로 자릅니다.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        # +1 은 줄바꿈 한 글자입니다.
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def send_long_text(update: Update, text: str) -> None:
    """상한을 넘는 답변도 잘리지 않고 전부 전달합니다."""
    chunks = split_for_telegram(text)
    for index, chunk in enumerate(chunks):
        if len(chunks) > 1:
            chunk = f"({index + 1}/{len(chunks)})\n{chunk}"
        await update.message.reply_text(chunk)


async def send_text_and_voice(update: Update, text: str) -> None:
    """동일한 답변을 Telegram 텍스트와 오디오로 차례대로 보냅니다."""
    await send_long_text(update, text)

    voice_path = None
    try:
        # 긴 목록을 통째로 읽어 주면 몇 분짜리 음성이 됩니다. 들을 만한
        # 길이까지만 만들고, 전문은 위 텍스트에 이미 다 가 있습니다.
        voice_path = make_voice_file(text[:MAX_VOICE_CHARS])
        with voice_path.open("rb") as audio:
            await update.message.reply_audio(audio=audio, title="Marvis reply")
    except Exception:
        # 음성 생성이 실패해도 텍스트 답변은 이미 전달됐으므로 조용히 넘어갑니다.
        logging.exception("Failed to send the voice reply")
    finally:
        if voice_path is not None:
            voice_path.unlink(missing_ok=True)
