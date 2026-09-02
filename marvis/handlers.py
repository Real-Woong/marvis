"""Telegram 어댑터입니다. 무엇을 할지는 core가 정하고, 여기서는 보내기만 합니다."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .auth import owner_only
from .core import SOURCE_TELEGRAM, handle_message
from .memory import (
    archive_all_memories,
    format_memories,
    format_schedule_by_date,
    get_ideas,
    get_recent_memories,
    mark_done,
)
from .projects import (
    STATUS_STOPPED,
    STATUSES,
    SUB_STATUS_OPTIONS,
    format_all_projects,
    update_project,
)
from .settings import MAX_IDEA_CONTEXT_ITEMS
from .storage import save_chat_id
from .voice import send_text_and_voice


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
        "/forget_all_marvis - 전체 기억 보관 처리"
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
        "7. 전체 기억 보관 처리\n/forget_all_marvis\n\n"
        "8. 프로젝트 진행 상태 확인\n/projects 또는 \"프로젝트 스케쥴\", \"프로젝트 할일\"이라고 물어보기\n\n"
        "9. 프로젝트 상태/할 일 갱신\n"
        "말로 하면 됩니다.\n"
        "예: OO프로젝트 시작할거야 새로 추가해줘\n"
        "예: 프로젝트OO 중단으로 수정해줘\n"
        "예: 프로젝트OO 다음할일은 배포 확인이야\n"
        "예: 프로젝트OO 잠깐 멈출게\n"
        "또는 /project_update 1 진행중 다음 할 일 내용\n\n"
        "주의: 일반 메시지도 기억에 저장되며, 지난 날짜의 스케쥴은 조회에서 자동으로 빠집니다."
    )
    await update.message.reply_text(message)


@owner_only
async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """최근 기억 20개를 보여줍니다."""
    save_chat_id(update.effective_chat.id)
    text = "최근 저장된 기억입니다:\n\n" + format_memories(get_recent_memories(limit=20))
    # 목록은 음성으로 들을 수 없는 길이라 텍스트로만 보냅니다.
    await update.message.reply_text(text)


@owner_only
async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 남아 있는 일정을 날짜별로 전송합니다."""
    save_chat_id(update.effective_chat.id)
    await send_text_and_voice(update, format_schedule_by_date())


@owner_only
async def ideas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """최근 아이디어를 보여줍니다."""
    save_chat_id(update.effective_chat.id)
    ideas = get_ideas()[-MAX_IDEA_CONTEXT_ITEMS:]
    await update.message.reply_text("최근 저장된 아이디어입니다:\n\n" + format_memories(ideas))


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
        project_seq = int(context.args[0])
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
        updated = update_project(project_seq, status=status, sub_status=sub_status, note=content)
    else:
        updated = update_project(project_seq, status=status, next_steps=content)

    if updated:
        await update.message.reply_text(f"{project_seq}번 프로젝트를 갱신했습니다.")
    else:
        await update.message.reply_text(f"{project_seq}번 프로젝트를 찾지 못했습니다.")


@owner_only
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """명령어 인수로 받은 기억 번호를 완료 처리합니다."""
    save_chat_id(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("완료 처리할 번호를 입력해주세요. 예: /done 1")
        return
    try:
        seq = int(context.args[0])
    except ValueError:
        await update.message.reply_text("번호는 숫자로 입력해주세요. 예: /done 1")
        return
    if mark_done(seq):
        await update.message.reply_text(f"{seq}번 항목을 완료 처리했습니다.")
    else:
        await update.message.reply_text(f"{seq}번 항목을 찾지 못했습니다.")


@owner_only
async def forget_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """전체 기억을 보관 처리합니다(조회에서 빠지지만 데이터는 남습니다)."""
    save_chat_id(update.effective_chat.id)
    count = archive_all_memories()
    await update.message.reply_text(
        f"기억 {count}건을 보관 처리했습니다. 앞으로 조회와 답변에 사용하지 않습니다."
    )


@owner_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """일반 메시지를 core로 넘기고, 나오는 응답을 순서대로 전송합니다."""
    save_chat_id(update.effective_chat.id)

    try:
        for reply in handle_message(update.message.text, source=SOURCE_TELEGRAM):
            if reply.speak:
                await send_text_and_voice(update, reply.text)
            else:
                await update.message.reply_text(reply.text)
    except Exception:
        logging.exception("Error while handling a Telegram message")
        await update.message.reply_text("처리하는 중 오류가 발생했습니다.")
