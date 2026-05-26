"""회고 ConversationHandler.

플로우:
  /회고 → 미회고 매도 카드 → 선택 →
  투자 판단 평가 → 잘한 점 → 아쉬운 점 → 피할 수 있었나 → 교훈 → 저장
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.keyboards import (
    AVOIDABLE_NO,
    AVOIDABLE_UNKNOWN,
    AVOIDABLE_YES,
    RETRO_SELECT_PREFIX,
    THESIS_CORRECT,
    THESIS_PARTIAL,
    THESIS_WRONG,
    avoidable_keyboard,
    retro_select_keyboard,
    thesis_eval_keyboard,
)
from models.retrospective import Retrospective
from models.transaction import Transaction
from storage.json_store import (
    load_retrospectives,
    load_transactions,
    save_retrospectives,
    save_transactions,
)

# ConversationHandler states
(
    SELECT,
    THESIS,
    WELL,
    REGRETS,
    AVOIDABLE,
    LESSONS,
) = range(6)

# 카드 최대 노출 개수
MAX_CARDS = 10


def _pending_sells() -> list[dict]:
    """회고가 없는 매도 거래를 최신순으로 반환."""
    txs = load_transactions()
    sells = [
        t for t in txs
        if t.get("type") == "sell" and not t.get("retrospective_id")
    ]
    sells.sort(key=lambda t: t.get("date", ""), reverse=True)
    return sells[:MAX_CARDS]


async def _start_retro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """회고 시작 → 회고할 매도 카드 표시."""
    pending = _pending_sells()

    if not pending:
        await update.message.reply_text("회고할 매도 거래가 없습니다.")
        return ConversationHandler.END

    await update.message.reply_text(
        "회고할 매도를 선택해주세요:",
        reply_markup=retro_select_keyboard(pending),
    )
    return SELECT


async def _select_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도 선택 콜백 → 투자 판단 평가 질문."""
    query = update.callback_query
    await query.answer()

    tx_id = query.data.removeprefix(RETRO_SELECT_PREFIX)

    txs = load_transactions()
    tx_dict = next((t for t in txs if t.get("id") == tx_id), None)
    if tx_dict is None:
        await query.edit_message_text("해당 거래를 찾을 수 없습니다.")
        return ConversationHandler.END

    if tx_dict.get("retrospective_id"):
        await query.edit_message_text("이미 회고가 작성된 거래입니다.")
        return ConversationHandler.END

    context.user_data["retro_tx"] = tx_dict

    thesis = tx_dict.get("buy_thesis", "")
    thesis_display = thesis if thesis else "(기록 없음)"
    name = tx_dict.get("name", "")

    await query.edit_message_text(
        f"[{name}] 회고 시작\n"
        f"원래 매수 근거: '{thesis_display}'\n\n"
        "이 판단이 맞았나요?",
        reply_markup=thesis_eval_keyboard(),
    )
    return THESIS


async def _thesis_eval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """투자 판단 평가 → 잘한 점 질문."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == THESIS_CORRECT:
        context.user_data["retro_thesis_correct"] = True
    elif data == THESIS_WRONG:
        context.user_data["retro_thesis_correct"] = False
    else:  # THESIS_PARTIAL
        context.user_data["retro_thesis_correct"] = None

    await query.edit_message_text("잘한 점은 무엇인가요?")
    return WELL


async def _well(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """잘한 점 → 아쉬운 점 질문."""
    context.user_data["retro_well"] = update.message.text
    await update.message.reply_text("아쉬운 점은 무엇인가요? (건너뛰려면 /skip)")
    return REGRETS


async def _regrets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """아쉬운 점 → 피할 수 있었나 질문."""
    text = update.message.text
    context.user_data["retro_regrets"] = "" if text.strip() == "/skip" else text

    await update.message.reply_text(
        "이 아쉬움은 피할 수 있었나요?",
        reply_markup=avoidable_keyboard(),
    )
    return AVOIDABLE


async def _regrets_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """아쉬운 점 /skip."""
    context.user_data["retro_regrets"] = ""
    await update.message.reply_text(
        "이 아쉬움은 피할 수 있었나요?",
        reply_markup=avoidable_keyboard(),
    )
    return AVOIDABLE


async def _avoidable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """피할 수 있었나 → 교훈 질문."""
    query = update.callback_query
    await query.answer()

    avoidable_map = {
        AVOIDABLE_YES: "피할 수 있었다",
        AVOIDABLE_NO: "통제 불가",
        AVOIDABLE_UNKNOWN: "모르겠다",
    }
    context.user_data["retro_avoidable"] = avoidable_map.get(query.data, "모르겠다")

    await query.edit_message_text("이번 거래에서 얻은 교훈은? (건너뛰려면 /skip)")
    return LESSONS


async def _lessons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """교훈 → 저장."""
    text = update.message.text
    context.user_data["retro_lessons"] = "" if text.strip() == "/skip" else text
    return await _save(update, context)


async def _lessons_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """교훈 /skip."""
    context.user_data["retro_lessons"] = ""
    return await _save(update, context)


async def _save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Retrospective 저장 및 Transaction 연결."""
    tx_dict = context.user_data["retro_tx"]
    tx = Transaction.from_dict(tx_dict)

    retro = Retrospective(
        transaction_id=tx.id,
        stock_name=tx.name,
        sell_date=tx.date,
        original_thesis=tx_dict.get("buy_thesis", ""),
        thesis_correct=context.user_data.get("retro_thesis_correct"),
        what_went_well=context.user_data.get("retro_well", ""),
        regrets=context.user_data.get("retro_regrets", ""),
        avoidable=context.user_data.get("retro_avoidable", ""),
        lessons=context.user_data.get("retro_lessons", ""),
    )

    retrospectives = load_retrospectives()
    retrospectives.append(retro.to_dict())
    save_retrospectives(retrospectives)

    transactions = load_transactions()
    for t in transactions:
        if t["id"] == tx.id:
            t["retrospective_id"] = retro.id
            break
    save_transactions(transactions)

    await update.message.reply_text("회고 저장 완료!")
    _cleanup(context)
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 중 /cancel로 전체 취소."""
    await update.message.reply_text("회고가 취소되었습니다.")
    _cleanup(context)
    return ConversationHandler.END


def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """회고 관련 user_data 정리."""
    keys = [
        "retro_tx",
        "retro_thesis_correct",
        "retro_well",
        "retro_regrets",
        "retro_avoidable",
        "retro_lessons",
    ]
    for key in keys:
        context.user_data.pop(key, None)


def _other_command_filter() -> filters.BaseFilter:
    """다른 명령어 필터 — 회고 대화 중 다른 명령 입력 시 대화 종료용."""
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|수정|회고|자산그래프|선물진입|선물청산|선물롤오버|선물회고)$"
    ) | filters.COMMAND


def retro_conversation() -> ConversationHandler:
    """회고 ConversationHandler를 생성하여 반환."""
    other_cmd = _other_command_filter()

    return ConversationHandler(
        entry_points=[
            CommandHandler("retro", _start_retro),
            MessageHandler(filters.Regex(r"^회고$"), _start_retro),
        ],
        states={
            SELECT: [
                CallbackQueryHandler(
                    _select_transaction, pattern=f"^{RETRO_SELECT_PREFIX}"
                ),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            THESIS: [
                CallbackQueryHandler(
                    _thesis_eval,
                    pattern=f"^({THESIS_CORRECT}|{THESIS_WRONG}|{THESIS_PARTIAL})$",
                ),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            WELL: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _well),
            ],
            REGRETS: [
                MessageHandler(other_cmd, _cancel),
                CommandHandler("skip", _regrets_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _regrets),
            ],
            AVOIDABLE: [
                CallbackQueryHandler(
                    _avoidable,
                    pattern=f"^({AVOIDABLE_YES}|{AVOIDABLE_NO}|{AVOIDABLE_UNKNOWN})$",
                ),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            LESSONS: [
                MessageHandler(other_cmd, _cancel),
                CommandHandler("skip", _lessons_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _lessons),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _cancel),
            CommandHandler("cancel", _cancel),
        ],
        name="retro",
        allow_reentry=True,
    )
