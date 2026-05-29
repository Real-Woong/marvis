import os
import re
import json
import time
import logging
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
ENV_TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
model = genai.GenerativeModel("gemini-2.5-flash")


# -----------------------------
# File / timezone / memory config
# -----------------------------
MEMORY_FILE = Path("marvis_memory.json")
CONFIG_FILE = Path("marvis_config.json")
VOICE_DIR = Path("voices")
KST = ZoneInfo("Asia/Seoul")

MAX_MEMORY_ITEMS = 500
MAX_IDEA_ITEMS = 150
MAX_RECENT_CONTEXT_ITEMS = 40


# -----------------------------
# Time helpers
# -----------------------------
def now_kst() -> datetime:
    return datetime.now(KST)


def today_kst_date():
    return now_kst().date()


def now_string() -> str:
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


def iso_datetime_kst(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


# -----------------------------
# JSON helpers
# -----------------------------
def load_json_file(path: Path, default):
    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data
    except Exception:
        return default


def save_json_file(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_memory() -> list:
    data = load_json_file(MEMORY_FILE, [])
    return data if isinstance(data, list) else []


def save_memory(items: list) -> None:
    items = optimize_memory_items(items)
    save_json_file(MEMORY_FILE, items)


def load_config() -> dict:
    data = load_json_file(CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def save_config(config: dict) -> None:
    save_json_file(CONFIG_FILE, config)


def save_chat_id(chat_id: int) -> None:
    config = load_config()
    config["telegram_chat_id"] = str(chat_id)
    config["updated_at"] = now_string()
    save_config(config)


def get_chat_id() -> str | None:
    if ENV_TELEGRAM_CHAT_ID:
        return ENV_TELEGRAM_CHAT_ID

    config = load_config()
    chat_id = config.get("telegram_chat_id")

    if chat_id:
        return str(chat_id)

    return None


# -----------------------------
# Schedule classification and parsing
# -----------------------------
def classify_memory(text: str) -> str:
    lowered = text.lower()

    schedule_keywords = [
        "일정", "스케쥴", "스케줄", "오늘", "내일", "모레", "이번주", "다음주",
        "해야", "해야해", "해야 돼", "할 일", "할일", "약속", "회의", "미팅",
        "마감", "기한", "리마인드", "알림", "알려줘", "챙겨", "예약", "방문",
        "제출", "업로드", "정리하기", "확인하기", "처리하기", "전화", "연락",
        "운동", "병원", "공부", "출근", "퇴근", "준비"
    ]

    idea_keywords = [
        "아이디어", "생각났", "구상", "기획", "만들고 싶", "프로젝트",
        "서비스", "앱", "개발", "컨셉", "문제의식", "사업", "기능",
        "구현", "설계", "고도화", "BM", "비즈니스", "창업"
    ]

    if any(keyword in lowered for keyword in schedule_keywords):
        return "schedule"

    if any(keyword in lowered for keyword in idea_keywords):
        return "idea"

    return "note"


def extract_schedule_date(text: str) -> str | None:
    today = today_kst_date()

    if "모레" in text:
        return (today + timedelta(days=2)).isoformat()

    if "내일" in text:
        return (today + timedelta(days=1)).isoformat()

    if "오늘" in text:
        return today.isoformat()

    # 2026-05-25, 2026.05.25, 2026/05/25
    match = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", text)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))

        try:
            return datetime(year, month, day, tzinfo=KST).date().isoformat()
        except ValueError:
            return None

    # 5.25, 5/25
    match = re.search(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?!\d)", text)
    if match:
        year = today.year
        month = int(match.group(1))
        day = int(match.group(2))

        try:
            schedule_date = datetime(year, month, day, tzinfo=KST).date()

            if schedule_date < today:
                schedule_date = datetime(year + 1, month, day, tzinfo=KST).date()

            return schedule_date.isoformat()
        except ValueError:
            return None

    # 5월 25일
    match = re.search(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if match:
        year = today.year
        month = int(match.group(1))
        day = int(match.group(2))

        try:
            schedule_date = datetime(year, month, day, tzinfo=KST).date()

            if schedule_date < today:
                schedule_date = datetime(year + 1, month, day, tzinfo=KST).date()

            return schedule_date.isoformat()
        except ValueError:
            return None

    return None


def extract_reminder_datetime(text: str, schedule_date: str | None) -> str | None:
    current = now_kst()

    # 10분 뒤, 30분 후
    match = re.search(r"(\d{1,3})\s*분\s*(뒤|후)", text)
    if match:
        minutes = int(match.group(1))
        return iso_datetime_kst(current + timedelta(minutes=minutes))

    # 1시간 뒤, 2시간 후
    match = re.search(r"(\d{1,2})\s*시간\s*(뒤|후)", text)
    if match:
        hours = int(match.group(1))
        return iso_datetime_kst(current + timedelta(hours=hours))

    if schedule_date:
        try:
            base_date = datetime.fromisoformat(schedule_date).date()
        except ValueError:
            base_date = today_kst_date()
    else:
        base_date = today_kst_date()

    hour = None
    minute = 0

    # 15:30, 9:00
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))

    # 오후 3시 30분, 오전 9시
    if hour is None:
        match = re.search(r"(오전|오후|아침|낮|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", text)
        if match:
            meridiem = match.group(1)
            hour = int(match.group(2))
            minute = int(match.group(3)) if match.group(3) else 0

            if meridiem in ["오후", "저녁", "밤"] and hour < 12:
                hour += 12

            if meridiem in ["오전", "아침"] and hour == 12:
                hour = 0

    if hour is None:
        return None

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None

    reminder = datetime(
        base_date.year,
        base_date.month,
        base_date.day,
        hour,
        minute,
        0,
        tzinfo=KST,
    )

    # 시간만 있고 이미 지난 시간이면 내일로 처리
    if not schedule_date and reminder <= current:
        reminder = reminder + timedelta(days=1)

    return iso_datetime_kst(reminder)


# -----------------------------
# Memory optimization
# -----------------------------
def parse_schedule_date(item: dict):
    schedule_date = item.get("schedule_date")
    if not schedule_date:
        return None

    try:
        return datetime.fromisoformat(schedule_date).date()
    except ValueError:
        return None


def renumber_memories(memories: list) -> list:
    for index, item in enumerate(memories, start=1):
        item["id"] = index
    return memories


def optimize_memory_items(items: list) -> list:
    today = today_kst_date()

    kept = []

    for item in items:
        item_type = item.get("type")

        if item_type == "schedule":
            schedule_date = parse_schedule_date(item)

            # 지난 날짜의 스케쥴은 삭제
            if schedule_date and schedule_date < today:
                continue

        kept.append(item)

    # 아이디어가 너무 많으면 오래된 아이디어 일부 정리
    ideas = [item for item in kept if item.get("type") == "idea"]
    if len(ideas) > MAX_IDEA_ITEMS:
        idea_ids_to_keep = set(item.get("id") for item in ideas[-MAX_IDEA_ITEMS:])
        kept = [
            item for item in kept
            if item.get("type") != "idea" or item.get("id") in idea_ids_to_keep
        ]

    # 전체 메모리가 너무 많으면 note부터 오래된 순서로 삭제
    while len(kept) > MAX_MEMORY_ITEMS:
        removed = False

        for idx, item in enumerate(kept):
            if item.get("type") == "note":
                kept.pop(idx)
                removed = True
                break

        if not removed:
            kept = kept[-MAX_MEMORY_ITEMS:]
            break

    return renumber_memories(kept)


def prune_past_schedules() -> int:
    before = load_json_file(MEMORY_FILE, [])
    if not isinstance(before, list):
        before = []

    after = optimize_memory_items(before)
    save_json_file(MEMORY_FILE, after)

    return max(0, len(before) - len(after))


# -----------------------------
# Memory manipulation
# -----------------------------
def add_memory(text: str) -> dict:
    memories = load_memory()

    memory_type = classify_memory(text)
    schedule_date = extract_schedule_date(text)
    reminder_at = extract_reminder_datetime(text, schedule_date)

    if schedule_date or reminder_at:
        memory_type = "schedule"

    if reminder_at and not schedule_date:
        try:
            schedule_date = datetime.strptime(reminder_at, "%Y-%m-%d %H:%M:%S").date().isoformat()
        except ValueError:
            schedule_date = None

    item = {
        "id": len(memories) + 1,
        "type": memory_type,
        "content": text.strip(),
        "schedule_date": schedule_date,
        "reminder_at": reminder_at,
        "reminded": False,
        "created_at": now_string(),
        "done": False,
    }

    memories.append(item)
    save_memory(memories)

    # reload to reflect renumbering/optimization
    final_memories = load_memory()
    return final_memories[-1] if final_memories else item


def get_recent_memories(limit: int = 30) -> list:
    memories = load_memory()
    return memories[-limit:]


def get_active_schedules() -> list:
    prune_past_schedules()
    memories = load_memory()

    return [
        item for item in memories
        if item.get("type") == "schedule" and not item.get("done", False)
    ]


def get_ideas() -> list:
    memories = load_memory()

    return [
        item for item in memories
        if item.get("type") == "idea"
    ]


def format_memories(items: list) -> str:
    if not items:
        return "저장된 기억이 없습니다."

    lines = []

    for item in items:
        status = "완료" if item.get("done") else "미완료"
        schedule_date = item.get("schedule_date")
        reminder_at = item.get("reminder_at")

        date_part = f", 일정일: {schedule_date}" if schedule_date else ""
        reminder_part = f", 알림: {reminder_at}" if reminder_at else ""

        lines.append(
            f"[{item['id']}] ({item.get('type', 'note')}, {status}{date_part}{reminder_part}) "
            f"{item['content']} "
            f"- 저장시각: {item['created_at']}"
        )

    return "\n".join(lines)


def format_schedule_by_date() -> str:
    prune_past_schedules()

    schedules = get_active_schedules()

    if not schedules:
        return "현재 남아있는 스케쥴이 없습니다."

    dated = {}
    undated = []

    for item in schedules:
        schedule_date = item.get("schedule_date")

        if schedule_date:
            dated.setdefault(schedule_date, []).append(item)
        else:
            undated.append(item)

    lines = ["현재 남아있는 스케쥴입니다.\n"]

    for schedule_date in sorted(dated.keys()):
        lines.append(f"{schedule_date}")

        for item in dated[schedule_date]:
            reminder_at = item.get("reminder_at")
            reminder_text = f" / 알림: {reminder_at[11:16]}" if reminder_at else ""
            lines.append(f"{item['id']}. {item['content']}{reminder_text}")

        lines.append("")

    if undated:
        lines.append("날짜 미정")

        for item in undated:
            lines.append(f"{item['id']}. {item['content']}")

    return "\n".join(lines).strip()


def mark_done(memory_id: int) -> bool:
    memories = load_memory()

    for item in memories:
        if item.get("id") == memory_id:
            item["done"] = True
            item["done_at"] = now_string()
            save_memory(memories)
            return True

    return False


def delete_all_memory() -> None:
    save_json_file(MEMORY_FILE, [])


# -----------------------------
# Proactive Telegram reminder
# -----------------------------
def send_proactive_telegram_message(text: str) -> bool:
    chat_id = get_chat_id()

    if not chat_id:
        logging.warning("No Telegram chat_id is saved. Cannot send proactive reminder.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
    }).encode("utf-8")

    try:
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        return True
    except Exception as e:
        logging.exception(f"Failed to send proactive reminder: {e}")
        return False


def reminder_loop():
    logging.info("Reminder loop started.")

    while True:
        try:
            memories = load_memory()
            current = now_kst()
            changed = False

            for item in memories:
                if item.get("type") != "schedule":
                    continue

                if item.get("done", False):
                    continue

                if item.get("reminded", False):
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
                        f"지금 예정된 일정입니다.\n"
                        f"- {item.get('content')}\n\n"
                        f"알림 시각: {reminder_at}"
                    )

                    sent = send_proactive_telegram_message(message)

                    if sent:
                        item["reminded"] = True
                        item["reminded_at"] = now_string()
                        changed = True

            if changed:
                save_memory(memories)

        except Exception as e:
            logging.exception(f"Reminder loop error: {e}")

        time.sleep(30)


def start_reminder_thread():
    thread = threading.Thread(target=reminder_loop, daemon=True)
    thread.start()


# -----------------------------
# Gemini response
# -----------------------------
def ask_gemini(user_text: str) -> str:
    prune_past_schedules()

    active_schedule_context = format_schedule_by_date()
    recent_memory_context = format_memories(get_recent_memories(limit=MAX_RECENT_CONTEXT_ITEMS))
    idea_context = format_memories(get_ideas()[-20:])

    prompt = f"""
너는 사용자의 개인 AI 비서 'Marvis'야.

Marvis의 핵심 역할:
- 사용자가 까먹기 쉬운 아이디어, 할 일, 일정, 생각을 기억하고 정리한다.
- 사용자가 "내가 오늘 뭐 해야 해?", "내가 뭐 하랬지?", "내 아이디어 뭐였지?", "!스케쥴"이라고 물으면 저장된 기억을 기반으로 답한다.
- 사용자의 일정과 아이디어를 비서처럼 관리한다.
- 답변은 한국어로 한다.
- 너무 길지 않게 핵심부터 말한다.
- 이동 중 에어팟으로 들어도 이해하기 쉽게 말한다.
- 필요한 경우 우선순위를 정리한다.
- 사용자가 방금 말한 내용도 기억된 것으로 간주하고 답한다.
- 오늘 날짜 기준은 한국 시간 {today_kst_date().isoformat()} 이다.
- 지난 날짜의 스케쥴은 자동 정리된 상태라고 가정한다.
- 알림 시간이 있는 스케쥴은 시간이 되면 Marvis가 먼저 텔레그램으로 알림을 보낸다.
- 단, 답변에서 알림을 보냈다고 거짓말하지 말고, 저장된 경우에만 "기억해둘게" 정도로 답한다.

현재 날짜별 스케쥴:
{active_schedule_context}

최근 저장된 기억:
{recent_memory_context}

최근 아이디어:
{idea_context}

사용자 메시지:
{user_text}
"""

    response = model.generate_content(prompt)

    if not response.text:
        return "죄송합니다. 답변을 생성하지 못했습니다."

    return response.text.strip()


def make_voice_file(text: str) -> str:
    VOICE_DIR.mkdir(exist_ok=True)

    file_path = VOICE_DIR / "reply.mp3"

    tts = gTTS(text=text, lang="ko")
    tts.save(str(file_path))

    return str(file_path)


async def send_text_and_voice(update: Update, text: str):
    await update.message.reply_text(text)

    voice_path = make_voice_file(text)

    with open(voice_path, "rb") as audio:
        await update.message.reply_audio(audio=audio, title="Marvis reply")


# -----------------------------
# Telegram handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)

    message = (
        "안녕하세요. 저는 Marvis입니다.\n\n"
        "이제부터 사용자가 보내는 메시지를 기억하고, "
        "일정/아이디어/할 일을 비서처럼 정리해드릴게요.\n\n"
        "시간이 포함된 일정은 제가 먼저 알림도 보내드립니다.\n\n"
        "예시:\n"
        "- 내일 오후 3시에 병원 예약 확인해야 해\n"
        "- 10분 뒤 물 마시라고 알려줘\n"
        "- 5.25 09:00 GitHub README 정리하기\n"
        "- 방금 새 프로젝트 아이디어가 생각났어\n"
        "- 내가 오늘 뭐 해야 해?\n\n"
        "명령어:\n"
        "!스케쥴 - 날짜별 스케쥴 확인\n"
        "/memory - 최근 기억 확인\n"
        "/schedule - 저장된 일정/할 일 확인\n"
        "/ideas - 저장된 아이디어 확인\n"
        "/done 1 - 1번 항목 완료 처리\n"
        "/forget_all_marvis - 전체 기억 삭제"
    )
    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)

    message = (
        "Marvis 사용 방법:\n\n"
        "1. 그냥 말하면 됩니다.\n"
        "예: 내일 병원 예약 확인해야 해\n\n"
        "2. 시간 알림도 가능합니다.\n"
        "예: 오늘 오후 8시에 운동하라고 알려줘\n"
        "예: 30분 뒤 물 마시라고 알려줘\n"
        "예: 5.25 09:00 GitHub README 정리하기\n\n"
        "3. 날짜별 스케쥴 확인\n"
        "!스케쥴\n"
        "또는 /schedule\n\n"
        "4. 저장된 아이디어 확인\n"
        "/ideas\n\n"
        "5. 최근 기억 확인\n"
        "/memory\n\n"
        "6. 완료 처리\n"
        "/done 1\n\n"
        "7. 전체 기억 삭제\n"
        "/forget_all_marvis\n\n"
        "주의: 일반 메시지도 기억에 저장됩니다. "
        "지난 날짜의 스케쥴은 자동으로 정리됩니다."
    )
    await update.message.reply_text(message)


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)

    recent_items = get_recent_memories(limit=20)
    text = "최근 저장된 기억입니다:\n\n" + format_memories(recent_items)
    await send_text_and_voice(update, text)


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)

    text = format_schedule_by_date()
    await send_text_and_voice(update, text)


async def ideas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)

    idea_items = get_ideas()[-20:]
    text = "최근 저장된 아이디어입니다:\n\n" + format_memories(idea_items)
    await send_text_and_voice(update, text)


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text("완료 처리할 번호를 입력해주세요. 예: /done 1")
        return

    try:
        memory_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("번호는 숫자로 입력해주세요. 예: /done 1")
        return

    success = mark_done(memory_id)

    if success:
        await update.message.reply_text(f"{memory_id}번 항목을 완료 처리했습니다.")
    else:
        await update.message.reply_text(f"{memory_id}번 항목을 찾지 못했습니다.")


async def forget_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)

    delete_all_memory()
    await update.message.reply_text("Marvis의 전체 기억을 삭제했습니다.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)

    user_text = update.message.text.strip()

    if not user_text:
        return

    prune_past_schedules()

    if user_text == "!스케쥴":
        text = format_schedule_by_date()
        await send_text_and_voice(update, text)
        return

    saved_item = add_memory(user_text)

    if saved_item.get("reminder_at"):
        await update.message.reply_text(
            f"기억했습니다. 알림 시각: {saved_item['reminder_at']}"
        )
    else:
        await update.message.reply_text("기억하고 생각 중입니다...")

    try:
        answer = ask_gemini(user_text)
        await send_text_and_voice(update, answer)

    except Exception as e:
        logging.exception("Error while handling message")
        await update.message.reply_text(f"오류가 발생했습니다: {e}")


def main():
    start_reminder_thread()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("ideas", ideas_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("forget_all_marvis", forget_all_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Marvis bot is running with memory, schedule, reminder, and optimization mode...")
    app.run_polling()


if __name__ == "__main__":
    main()
