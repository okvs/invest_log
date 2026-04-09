"""증권사 체결 알림 메시지 처리 ConversationHandler.

KB증권 ([KB증권] ...) 또는 신한증권 (계좌명 : ...) 메시지를
붙여넣으면 매수/매도를 자동 파싱하고 부족한 정보만 추가 질문.
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

from bot.formatters import format_buy_result, format_sell_result
from bot.handlers.sell import (
    _process_sell,
    _skip_retro,
    _start_retro,
    _retro_thesis_eval,
    _retro_well,
    _retro_regrets,
    _retro_regrets_skip,
    _retro_avoidable,
    _retro_lessons,
    _retro_lessons_skip,
    RETRO_ASK,
    RETRO_THESIS,
    RETRO_WELL,
    RETRO_REGRETS,
    RETRO_AVOIDABLE,
    RETRO_LESSONS,
)
from bot.handlers.buy import _process_and_save
from bot.keyboards import (
    AVOIDABLE_NO,
    AVOIDABLE_UNKNOWN,
    AVOIDABLE_YES,
    SKIP_RETRO,
    START_RETRO,
    THESIS_CORRECT,
    THESIS_PARTIAL,
    THESIS_WRONG,
    retro_ask_keyboard,
)
from parsers.input_parser import BuyInput, parse_broker_message, resolve_name
from storage.json_store import load_nickname_map

# ConversationHandler states (10~부터 시작하여 sell.py 상태값과 충돌 방지)
SELL_REASON = 10
BUY_SECTOR = 11
BUY_THESIS = 12


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
            RETRO_ASK: [
                CallbackQueryHandler(_start_retro, pattern=f"^{START_RETRO}$"),
                CallbackQueryHandler(_skip_retro, pattern=f"^{SKIP_RETRO}$"),
            ],
            RETRO_THESIS: [
                CallbackQueryHandler(
                    _retro_thesis_eval,
                    pattern=f"^({THESIS_CORRECT}|{THESIS_WRONG}|{THESIS_PARTIAL})$",
                ),
            ],
            RETRO_WELL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _retro_well),
            ],
            RETRO_REGRETS: [
                CommandHandler("skip", _retro_regrets_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _retro_regrets),
            ],
            RETRO_AVOIDABLE: [
                CallbackQueryHandler(
                    _retro_avoidable,
                    pattern=f"^({AVOIDABLE_YES}|{AVOIDABLE_NO}|{AVOIDABLE_UNKNOWN})$",
                ),
            ],
            RETRO_LESSONS: [
                CommandHandler("skip", _retro_lessons_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _retro_lessons),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="broker",
        allow_reentry=True,
    )
