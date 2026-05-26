"""선물시세 ConversationHandler.

플로우:
  선물시세 → 보유 선물 포지션 카드 → 선택 → 현재 선물가 입력 → 저장
  저장된 시세는 6시간 동안 대시보드/회고 노출 시 미실현 P&L 계산에 사용.
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

from bot.futures_quote import set_manual_quote
from bot.keyboards import (
    FUTURES_POS_PREFIX,
    futures_positions_keyboard,
)
from parsers.futures_input import _parse_number
from storage.json_store import load_futures_positions

SELECT, PRICE = range(2)


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    positions = [p for p in load_futures_positions() if p.get("contracts", 0) > 0]
    if not positions:
        await update.message.reply_text("선물 포지션이 없습니다.")
        return ConversationHandler.END
    positions.sort(key=lambda p: p.get("expiry_date", ""))
    await update.message.reply_text(
        "시세를 입력할 포지션을 선택해주세요:",
        reply_markup=futures_positions_keyboard(positions),
    )
    return SELECT


async def _select_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    pid = query.data.removeprefix(FUTURES_POS_PREFIX)
    pos = next((p for p in load_futures_positions() if p.get("id") == pid), None)
    if pos is None:
        await query.edit_message_text("해당 포지션을 찾을 수 없습니다.")
        return ConversationHandler.END

    context.user_data["fut_quote_symbol"] = pos.get("symbol", "")
    context.user_data["fut_quote_name"] = pos.get("name", "")

    await query.edit_message_text(
        f"[{pos.get('name','')}] 현재 선물가(원)를 입력해주세요.\n"
        "예: 72500"
    )
    return PRICE


async def _receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    symbol = context.user_data.get("fut_quote_symbol", "")
    name = context.user_data.get("fut_quote_name", "")
    if not symbol:
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        return ConversationHandler.END
    try:
        price = _parse_number(text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}")
        return PRICE

    set_manual_quote(symbol, price)
    await update.message.reply_text(
        f"[{name}] 현재 선물가 {int(price):,}원 저장 완료.\n"
        "6시간 동안 대시보드 미실현 P&L 계산에 사용됩니다."
    )
    _cleanup(context)
    return ConversationHandler.END


def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("fut_quote_symbol", "fut_quote_name"):
        context.user_data.pop(k, None)


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|수정|회고|자산그래프|입금|출금|입출금목록|선물진입|선물청산|선물롤오버|선물회고|선물시세)$"
    ) | filters.COMMAND


async def _abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cleanup(context)
    await update.message.reply_text("시세 입력이 취소되었습니다.")
    return ConversationHandler.END


def futures_quote_conversation() -> ConversationHandler:
    other_cmd = _other_command_filter()
    return ConversationHandler(
        entry_points=[
            CommandHandler("futures_quote", _start),
            MessageHandler(filters.Regex(r"^선물시세$"), _start),
        ],
        states={
            SELECT: [
                CallbackQueryHandler(_select_position, pattern=f"^{FUTURES_POS_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _abort),
            ],
            PRICE: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_price),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _abort),
            CommandHandler("cancel", _abort),
        ],
        name="futures_quote",
        allow_reentry=True,
    )
