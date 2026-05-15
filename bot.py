import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from gtts import gTTS
import google.generativeai as genai

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# -----------------------------
# Environment setup
# -----------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in .env")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in .env")


# -----------------------------
# Logging setup
# -----------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


# -----------------------------
# Gemini setup
# -----------------------------
genai.configure(api_key=GEMINI_API_KEY)

# Gemini 1.5 Flash returned a 404 error in the current API environment,
# so Gemini 2.5 Flash is used instead.
model = genai.GenerativeModel("gemini-2.5-flash")


# -----------------------------
# Core functions
# -----------------------------
def ask_gemini(user_text: str) -> str:
    prompt = f"""
너는 사용자의 개인 AI 비서 'Marvis'야.

답변 규칙:
- 한국어로 답변해.
- 너무 길지 않게 핵심부터 말해.
- 이동 중 에어팟으로 들어도 이해하기 쉽게 말해.
- 필요한 경우 단계별로 정리해.
- 사용자의 질문이 애매하면 가장 가능성 높은 방향으로 답해.

사용자 질문:
{user_text}
"""

    response = model.generate_content(prompt)

    if not response.text:
        return "죄송합니다. 답변을 생성하지 못했습니다."

    return response.text.strip()


def make_voice_file(text: str) -> str:
    output_dir = Path("voices")
    output_dir.mkdir(exist_ok=True)

    file_path = output_dir / "reply.mp3"

    tts = gTTS(text=text, lang="ko")
    tts.save(str(file_path))

    return str(file_path)


# -----------------------------
# Telegram handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "안녕하세요. 저는 Marvis입니다.\n\n"
        "텍스트로 질문을 보내면 Gemini로 답변을 만들고, "
        "음성 파일로도 보내드릴게요."
    )
    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "사용 방법:\n"
        "1. 텔레그램에 질문을 입력하세요.\n"
        "2. 제가 Gemini로 답변을 생성합니다.\n"
        "3. 텍스트 답변과 mp3 음성 답변을 함께 보내드립니다.\n\n"
        "예시:\n"
        "- 오늘 할 일 정리해줘\n"
        "- 이 개념 쉽게 설명해줘\n"
        "- 회의 전에 확인할 내용 정리해줘"
    )
    await update.message.reply_text(message)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text

    await update.message.reply_text("생각 중입니다...")

    try:
        answer = ask_gemini(user_text)

        # Send text answer
        await update.message.reply_text(answer)

        # Convert answer to voice file
        voice_path = make_voice_file(answer)

        # Send voice/audio file
        with open(voice_path, "rb") as audio:
            await update.message.reply_audio(audio=audio, title="Marvis reply")

    except Exception as e:
        logging.exception("Error while handling message")
        await update.message.reply_text(f"오류가 발생했습니다: {e}")


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Marvis bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()