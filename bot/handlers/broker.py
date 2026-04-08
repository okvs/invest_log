"""증권사 체결 알림 메시지 처리 ConversationHandler.

KB증권 ([KB증권] ...) 또는 신한증권 (계좌명 : ...) 메시지를
붙여넣으면 매수/매도를 자동 파싱하고 부족한 정보만 추가 질문.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.formatters import format_buy_result, format_sell_result
from bot.handlers.sell import _process_sell
from bot.handlers.buy import _process_and_save
from bot.keyboards import retro_ask_keyboard
from parsers.input_parser import BuyInput, parse_broker_message, resolve_name
from storage.json_store import load_nickname_map

# ConversationHandler states
SELL_REASON, BUY_SECTOR, BUY_THESIS = range(3)


async def _receive_broker_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """증권사 메시지 파싱 → 매수/매도 분기."""
    text = update.message.text

    try:
        msg = parse_broker_message(text)
    except ValueError as e:
        await update.message.reply_text(f"메시지 인식 실패: {e}")
        return ConversationHandler.END

    # 닉네임 변환
    nmap = load_nickname_map()
    msg.name = resolve_name(msg.name, nickname_map=nmap)

    if msg.trade_type == "sell":
        context.user_data["broker_sell"] = msg
        await update.message.reply_text(
            f"{msg.name} {msg.quantity}주 {int(msg.price):,}원 매도 체결 확인.\n\n"
            "매도사유를 입력해주세요."
        )
        return SELL_REASON
    else:
        context.user_data["broker_buy"] = msg
        await update.message.reply_text(
            f"{msg.name} {msg.quantity}주 {int(msg.price):,}원 매수 체결 확인.\n\n"
            "섹터를 입력해주세요. (예: 반도체, IT, 바이오)"
        )
        return BUY_SECTOR


async def _sell_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도사유 입력 → 매도 처리."""
    reason = update.message.text.strip()
    msg = context.user_data.pop("broker_sell")

    return await _process_sell(
        update, context,
        msg.name, msg.quantity, msg.price, reason,
        error_state=ConversationHandler.END,
    )


async def _buy_sector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """섹터 입력 → 매수근거 질문."""
    context.user_data["broker_sector"] = update.message.text.strip()
    await update.message.reply_text("매수 근거를 입력해주세요.")
    return BUY_THESIS


async def _buy_thesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매수근거 입력 → 매수 저장."""
    thesis = update.message.text.strip()
    msg = context.user_data.pop("broker_buy")
    sector = context.user_data.pop("broker_sector")

    buy_input = BuyInput(
        name=msg.name,
        ticker="",
        sector=sector,
        quantity=msg.quantity,
        price=msg.price,
        thesis=thesis,
    )

    result_text = _process_and_save(buy_input)
    await update.message.reply_text(result_text)
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 취소."""
    for key in ["broker_sell", "broker_buy", "broker_sector"]:
        context.user_data.pop(key, None)
    await update.message.reply_text("취소되었습니다.")
    return ConversationHandler.END


def broker_conversation() -> ConversationHandler:
    """증권사 체결 메시지 ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(r"(?s)^\[KB증권\]"), _receive_broker_msg
            ),
            MessageHandler(
                filters.Regex(r"(?s)^계좌명\s*:"), _receive_broker_msg
            ),
        ],
        states={
            SELL_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _sell_reason),
            ],
            BUY_SECTOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buy_sector),
            ],
            BUY_THESIS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buy_thesis),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="broker",
        allow_reentry=True,
    )
