"""선물 회고 ConversationHandler.

플로우:
  선물회고 → 미회고 close/roll_close 카드 → 선택 →
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
    FUTURES_RETRO_PREFIX,
    THESIS_CORRECT,
    THESIS_PARTIAL,
    THESIS_WRONG,
    avoidable_keyboard,
    futures_retro_select_keyboard,
    thesis_eval_keyboard,
)
from models.futures_transaction import FuturesTransaction
from models.retrospective import Retrospective
from storage.json_store import (
    load_futures_transactions,
    load_retrospectives,
    save_futures_transactions,
    save_retrospectives,
)

SELECT, THESIS, WELL, REGRETS, AVOIDABLE, LESSONS = range(6)
MAX_CARDS = 10


def _pending_closes() -> list[dict]:
    """회고가 없는 선물 청산/롤오버 거래를 최신순으로 반환."""
    txs = load_futures_transactions()
    out = [
        t for t in txs
        if t.get("type") in ("close", "roll_close")
        and not t.get("retrospective_id")
    ]
    out.sort(key=lambda t: t.get("date", ""), reverse=True)
    return out[:MAX_CARDS]


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pending = _pending_closes()
    if not pending:
        await update.message.reply_text("회고할 선물 청산 거래가 없습니다.")
        return ConversationHandler.END
    await update.message.reply_text(
        "회고할 선물 청산/롤오버를 선택해주세요:",
        reply_markup=futures_retro_select_keyboard(pending),
    )
    return SELECT


async def _select_tx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tx_id = query.data.removeprefix(FUTURES_RETRO_PREFIX)
    tx_dict = next(
        (t for t in load_futures_transactions() if t.get("id") == tx_id), None
    )
    if tx_dict is None:
        await query.edit_message_text("해당 거래를 찾을 수 없습니다.")
        return ConversationHandler.END
    if tx_dict.get("retrospective_id"):
        await query.edit_message_text("이미 회고가 작성된 거래입니다.")
        return ConversationHandler.END

    context.user_data["fut_retro_tx"] = tx_dict

    thesis = tx_dict.get("buy_thesis", "")
    thesis_display = thesis if thesis else "(기록 없음)"
    direction_kr = "롱" if tx_dict.get("direction") == "long" else "숏"
    await query.edit_message_text(
        f"[{tx_dict.get('name','')} {direction_kr} {tx_dict.get('contracts',0)}계약] 회고 시작\n"
        f"원래 진입 사유: '{thesis_display}'\n\n"
        "이 판단이 맞았나요?",
        reply_markup=thesis_eval_keyboard(),
    )
    return THESIS


async def _thesis_eval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == THESIS_CORRECT:
        context.user_data["fut_retro_thesis_correct"] = True
    elif data == THESIS_WRONG:
        context.user_data["fut_retro_thesis_correct"] = False
    else:
        context.user_data["fut_retro_thesis_correct"] = None
    await query.edit_message_text("잘한 점은 무엇인가요?")
    return WELL


async def _well(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["fut_retro_well"] = update.message.text
    await update.message.reply_text("아쉬운 점은 무엇인가요? (건너뛰려면 /skip)")
    return REGRETS


async def _regrets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data["fut_retro_regrets"] = "" if text.strip() == "/skip" else text
    await update.message.reply_text(
        "이 아쉬움은 피할 수 있었나요?", reply_markup=avoidable_keyboard()
    )
    return AVOIDABLE


async def _regrets_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["fut_retro_regrets"] = ""
    await update.message.reply_text(
        "이 아쉬움은 피할 수 있었나요?", reply_markup=avoidable_keyboard()
    )
    return AVOIDABLE


async def _avoidable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    avoidable_map = {
        AVOIDABLE_YES: "피할 수 있었다",
        AVOIDABLE_NO: "통제 불가",
        AVOIDABLE_UNKNOWN: "모르겠다",
    }
    context.user_data["fut_retro_avoidable"] = avoidable_map.get(query.data, "모르겠다")
    await query.edit_message_text("이번 거래에서 얻은 교훈은? (건너뛰려면 /skip)")
    return LESSONS


async def _lessons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data["fut_retro_lessons"] = "" if text.strip() == "/skip" else text
    return await _save(update, context)


async def _lessons_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["fut_retro_lessons"] = ""
    return await _save(update, context)


async def _save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tx_dict = context.user_data["fut_retro_tx"]
    tx = FuturesTransaction.from_dict(tx_dict)

    retro = Retrospective(
        transaction_id=tx.id,
        stock_name=tx.name,
        sell_date=tx.date,
        original_thesis=tx_dict.get("buy_thesis", ""),
        thesis_correct=context.user_data.get("fut_retro_thesis_correct"),
        what_went_well=context.user_data.get("fut_retro_well", ""),
        regrets=context.user_data.get("fut_retro_regrets", ""),
        avoidable=context.user_data.get("fut_retro_avoidable", ""),
        lessons=context.user_data.get("fut_retro_lessons", ""),
        is_futures=True,
    )

    retrospectives = load_retrospectives()
    retrospectives.append(retro.to_dict())
    save_retrospectives(retrospectives)

    txs = load_futures_transactions()
    for t in txs:
        if t["id"] == tx.id:
            t["retrospective_id"] = retro.id
            break
    save_futures_transactions(txs)

    await update.message.reply_text("선물 회고 저장 완료!")
    _cleanup(context)
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("선물 회고가 취소되었습니다.")
    _cleanup(context)
    return ConversationHandler.END


def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in (
        "fut_retro_tx", "fut_retro_thesis_correct", "fut_retro_well",
        "fut_retro_regrets", "fut_retro_avoidable", "fut_retro_lessons",
    ):
        context.user_data.pop(k, None)


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(
        r"^(매도|매수|현황|도움말|수정|회고|선물진입|선물청산|선물롤오버|선물회고)$"
    ) | filters.COMMAND


def futures_retro_conversation() -> ConversationHandler:
    other_cmd = _other_command_filter()
    return ConversationHandler(
        entry_points=[
            CommandHandler("futures_retro", _start),
            MessageHandler(filters.Regex(r"^선물회고$"), _start),
        ],
        states={
            SELECT: [
                CallbackQueryHandler(_select_tx, pattern=f"^{FUTURES_RETRO_PREFIX}"),
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
        name="futures_retro",
        allow_reentry=True,
    )
