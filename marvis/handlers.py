"""Telegram 명령어와 일반 텍스트 메시지의 사용자 흐름을 처리합니다."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .ai import ask_gemini
from .memory import (
    add_memory,
    delete_all_memory,
    format_memories,
    format_schedule_by_date,
    get_ideas,
    get_recent_memories,
    mark_done,
    prune_past_schedules,
)
from .projects import (
    ACTION_ADD,
    ACTION_MUTE_BRIEFING,
    ACTION_NEXT_STEPS,
    ACTION_PAUSE,
    ACTION_STOP,
    ACTION_UNMUTE_BRIEFING,
    STATUSES,
    STATUS_IN_PROGRESS,
    STATUS_STOPPED,
    SUB_STATUS_OPTIONS,
    add_project,
    detect_project_action,
    extract_next_steps_content,
    extract_project_name,
    find_project_by_name,
    format_all_projects,
    update_project,
)
from .schedule_parser import detect_message_intent
from .storage import save_chat_id
from .voice import send_text_and_voice
from .auth import owner_only

@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """채팅 ID를 등록하고 Marvis의 주요 사용 방법을 안내합니다."""
    save_chat_id(update.effective_chat.id)
    message = (
        "안녕하세요. 저는 Marvis입니다.\n\n"
        "이제부터 사용자가 보내는 메시지를 기억하고, 일정/아이디어/할 일을 비서처럼 정리해드릴게요.\n\n"
        "시간이 포함된 일정은 제가 먼저 알림도 보내드립니다.\n\n"
        "예시:\n"
        "- 내일 오후 3시에 병원 예약 확인해야 해\n"
        "- 10분 뒤 물 마시라고 알려줘\n"
        "- 5.25 09:00 GitHub README 정리하기\n"
        "- 방금 새 프로젝트 아이디어가 생각났어\n"
        "- 내가 오늘 뭐 해야 해?\n"
        "- OO프로젝트 시작할거야 새로 추가해줘\n"
        "- 프로젝트OO 중단으로 수정해줘\n"
        "- 프로젝트OO 다음할일은 배포 확인이야\n"
        "- 프로젝트OO 잠깐 멈출게\n\n"
        "명령어:\n"
        "!스케쥴 - 날짜별 스케쥴 확인\n"
        "/memory - 최근 기억 확인\n"
        "/schedule - 저장된 일정/할 일 확인\n"
        "/ideas - 저장된 아이디어 확인\n"
        "/projects - 프로젝트 진행 상태 확인\n"
        "/project_update - 프로젝트 상태/할 일 갱신\n"
        "/done 1 - 1번 항목 완료 처리\n"
        "/forget_all_marvis - 전체 기억 삭제"
    )
    await update.message.reply_text(message)

@owner_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """지원하는 입력 예시와 명령어를 안내합니다."""
    save_chat_id(update.effective_chat.id)
    message = (
        "Marvis 사용 방법:\n\n"
        "1. 그냥 말하면 됩니다.\n예: 내일 병원 예약 확인해야 해\n\n"
        "2. 시간 알림도 가능합니다.\n"
        "예: 오늘 오후 8시에 운동하라고 알려줘\n"
        "예: 30분 뒤 물 마시라고 알려줘\n\n"
        "3. 날짜별 스케쥴 확인\n!스케쥴 또는 /schedule\n\n"
        "4. 저장된 아이디어 확인\n/ideas\n\n"
        "5. 최근 기억 확인\n/memory\n\n"
        "6. 완료 처리\n/done 1\n\n"
        "7. 전체 기억 삭제\n/forget_all_marvis\n\n"
        "8. 프로젝트 진행 상태 확인\n/projects 또는 \"프로젝트 스케쥴\", \"프로젝트 할일\"이라고 물어보기\n\n"
        "9. 프로젝트 상태/할 일 갱신\n"
        "말로 하면 됩니다.\n"
        "예: OO프로젝트 시작할거야 새로 추가해줘\n"
        "예: 프로젝트OO 중단으로 수정해줘\n"
        "예: 프로젝트OO 다음할일은 배포 확인이야\n"
        "예: 프로젝트OO 잠깐 멈출게\n"
        "또는 /project_update 1 진행중 다음 할 일 내용\n\n"
        "주의: 일반 메시지도 기억에 저장되며 지난 날짜의 스케쥴은 자동 정리됩니다."
    )
    await update.message.reply_text(message)

@owner_only
async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """최근 기억 20개를 텍스트와 음성으로 전송합니다."""
    save_chat_id(update.effective_chat.id)
    text = "최근 저장된 기억입니다:\n\n" + format_memories(get_recent_memories(limit=20))
    await send_text_and_voice(update, text)

@owner_only
async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 남아 있는 일정을 날짜별로 전송합니다."""
    save_chat_id(update.effective_chat.id)
    await send_text_and_voice(update, format_schedule_by_date())

@owner_only
async def ideas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """최근 아이디어 20개를 전송합니다."""
    save_chat_id(update.effective_chat.id)
    text = "최근 저장된 아이디어입니다:\n\n" + format_memories(get_ideas()[-20:])
    await send_text_and_voice(update, text)

@owner_only
async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """등록된 프로젝트를 진행중/중단으로 묶어 전송합니다."""
    save_chat_id(update.effective_chat.id)
    await send_text_and_voice(update, format_all_projects())

@owner_only
async def project_update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """프로젝트 번호의 상태와 다음 할 일/메모를 갱신합니다."""
    save_chat_id(update.effective_chat.id)
    usage = (
        "사용법: /project_update <번호> <진행중|중단> [내용]\n"
        "예: /project_update 1 진행중 검증, Latency 개선\n"
        "예: /project_update 2 중단 일시정지 팀원 합류 대기중\n\n"
        f"중단 상태 태그(선택): {', '.join(SUB_STATUS_OPTIONS)}"
    )
    if len(context.args) < 2:
        await update.message.reply_text(usage)
        return

    try:
        project_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("번호는 숫자로 입력해주세요.\n\n" + usage)
        return

    status = context.args[1]
    if status not in STATUSES:
        await update.message.reply_text("상태는 '진행중' 또는 '중단'만 가능합니다.\n\n" + usage)
        return

    rest = context.args[2:]
    sub_status = None
    if status == STATUS_STOPPED and rest and rest[0] in SUB_STATUS_OPTIONS:
        sub_status = rest[0]
        rest = rest[1:]
    content = " ".join(rest) if rest else None

    if status == STATUS_STOPPED:
        updated = update_project(project_id, status=status, sub_status=sub_status, note=content)
    else:
        updated = update_project(project_id, status=status, next_steps=content)

    if updated:
        await update.message.reply_text(f"{project_id}번 프로젝트를 갱신했습니다.")
    else:
        await update.message.reply_text(f"{project_id}번 프로젝트를 찾지 못했습니다.")

@owner_only
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """명령어 인수로 받은 기억 번호를 완료 처리합니다."""
    save_chat_id(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("완료 처리할 번호를 입력해주세요. 예: /done 1")
        return
    try:
        memory_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("번호는 숫자로 입력해주세요. 예: /done 1")
        return
    if mark_done(memory_id):
        await update.message.reply_text(f"{memory_id}번 항목을 완료 처리했습니다.")
    else:
        await update.message.reply_text(f"{memory_id}번 항목을 찾지 못했습니다.")

@owner_only
async def forget_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """전체 기억 삭제 명령을 처리합니다."""
    save_chat_id(update.effective_chat.id)
    delete_all_memory()
    await update.message.reply_text("Marvis의 전체 기억을 삭제했습니다.")

@owner_only
async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """일반 메시지의 의도를 판별하고 필요한 경우에만 저장합니다."""
    save_chat_id(update.effective_chat.id)

    user_text = update.message.text.strip()

    if not user_text:
        return

    prune_past_schedules()

    if user_text == "!스케쥴":
        await send_text_and_voice(
            update,
            format_schedule_by_date(),
        )
        return

    if user_text == "!프로젝트":
        await send_text_and_voice(
            update,
            format_all_projects(),
        )
        return

    project_action = detect_project_action(user_text)
    if project_action:
        project_name = extract_project_name(user_text)
        if not project_name:
            await update.message.reply_text("어떤 프로젝트인지 이름을 알려주세요.")
            return

        if project_action == ACTION_ADD:
            add_project(project_name, status=STATUS_IN_PROGRESS)
            await update.message.reply_text(f"'{project_name}' 프로젝트를 새로 등록했습니다.")
            return

        project = find_project_by_name(project_name)
        if not project:
            await update.message.reply_text(f"'{project_name}'과 일치하는 프로젝트를 찾지 못했습니다.")
            return

        if project_action == ACTION_STOP:
            update_project(project["id"], status=STATUS_STOPPED)
            await update.message.reply_text(f"'{project['name']}' 프로젝트를 중단 상태로 변경했습니다.")
        elif project_action == ACTION_PAUSE:
            update_project(project["id"], status=STATUS_STOPPED, sub_status="일시정지")
            await update.message.reply_text(f"'{project['name']}' 프로젝트를 일시정지로 변경했습니다.")
        elif project_action == ACTION_NEXT_STEPS:
            content = extract_next_steps_content(user_text)
            if not content:
                await update.message.reply_text("다음 할 일 내용을 인식하지 못했습니다.")
                return
            update_project(project["id"], next_steps=content)
            await update.message.reply_text(f"'{project['name']}'의 다음 할 일을 갱신했습니다: {content}")
        elif project_action == ACTION_MUTE_BRIEFING:
            update_project(project["id"], muted_from_briefing=True)
            await update.message.reply_text(f"'{project['name']}'을(를) 아침 브리핑에서 제외했습니다.")
        elif project_action == ACTION_UNMUTE_BRIEFING:
            update_project(project["id"], muted_from_briefing=False)
            await update.message.reply_text(f"'{project['name']}'을(를) 아침 브리핑에 다시 포함했습니다.")
        return

    intent = detect_message_intent(user_text)

    if intent == "save":
        saved_item = add_memory(user_text)

        if saved_item.get("reminder_at"):
            await update.message.reply_text(
                f"기억했습니다. 알림 시각: "
                f"{saved_item['reminder_at']}"
            )
        else:
            memory_type_names = {
                "schedule": "일정",
                "idea": "아이디어",
                "note": "메모",
            }

            memory_type = memory_type_names.get(
                saved_item.get("type"),
                "기억",
            )

            await update.message.reply_text(
                f"{memory_type}으로 기억했습니다."
            )

    elif intent == "query":
        await update.message.reply_text(
            "저장된 내용을 확인하고 있습니다..."
        )

    else:
        await update.message.reply_text(
            "생각 중입니다..."
        )

    try:
        answer = ask_gemini(user_text)

        await send_text_and_voice(
            update,
            answer,
        )

    except Exception:
        logging.exception(
            "Error while handling message"
        )

        await update.message.reply_text(
            "답변을 생성하는 중 오류가 발생했습니다."
        )