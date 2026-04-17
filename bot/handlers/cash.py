"""예수금(계좌) 설정 ConversationHandler.

플로우:
  /cash 또는 '예수금' → 초기자본 입력 → 기존 보유종목별 증거금비율 선택 → 예수금 자동 계산
  이미 설정된 경우: 현재 예수금 표시 + 초기화 옵션
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.formatters import format_number
from bot.keyboards import MARGIN_PREFIX, margin_ratio_keyboard
from models.portfolio import Holding
from storage.json_store import (
    load_account,
    load_holdings,
    save_account,
    save_holdings,
)

logger = logging.getLogger(__name__)

# ConversationHandler states (20~부터 시작)
CAPITAL_INPUT = 20
HOLDING_MARGIN = 21

RESET_CONFIRM = "cash_reset"
CASH_CANCEL = "cash_cancel"


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """예수금 설정 시작."""
    account = load_account()

    if account.get("initial_capital"):
        cash = account.get("cash", 0)
        capital = account["initial_capital"]
        await update.message.reply_text(
            f"초기자본: {format_number(capital)}원\n"
            f"현재 예수금: {format_number(cash)}원\n\n"
            "초기자본을 재설정하려면 금액을 입력해주세요.\n"
            "취소하려면 /cancel 을 입력해주세요."
        )
    else:
        await update.message.reply_text(
            "초기자본을 입력해주세요.\n"
            "(예: 1억 → 100000000, 5000만원 → 50000000)"
        )
    return CAPITAL_INPUT


async def _capital_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """초기자본 입력 처리 → 기존 보유종목 증거금비율 순차 질문."""
    import re
    text = update.message.text.strip()
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not cleaned:
        await update.message.reply_text("숫자를 입력해주세요. (예: 100000000)")
        return CAPITAL_INPUT

    capital = float(cleaned)
    if capital <= 0:
        await update.message.reply_text("0보다 큰 금액을 입력해주세요.")
        return CAPITAL_INPUT

    context.user_data["cash_capital"] = capital

    # 기존 보유종목 확인
    holdings = load_holdings()
    active = [h for h in holdings if h.get("quantity", 0) > 0]

    if not active:
        # 보유종목 없으면 바로 저장
        account = {"initial_capital": capital, "cash": capital}
        save_account(account)
        await update.message.reply_text(
            f"초기자본 {format_number(capital)}원 설정 완료!\n"
            f"예수금: {format_number(capital)}원"
        )
        return ConversationHandler.END

    # 보유종목별 증거금비율 순차 질문
    context.user_data["cash_holdings"] = active
    context.user_data["cash_idx"] = 0
    context.user_data["cash_margins"] = []

    h = active[0]
    await update.message.reply_text(
        f"기존 보유종목의 증거금비율을 선택해주세요.\n\n"
        f"[1/{len(active)}] {h['name']}  |  {h['quantity']}주  |  "
        f"투자금 {format_number(h['total_invested'])}원",
        reply_markup=margin_ratio_keyboard(),
    )
    return HOLDING_MARGIN


async def _holding_margin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """보유종목별 증거금비율 선택."""
    query = update.callback_query
    await query.answer()

    margin_ratio = int(query.data.removeprefix(MARGIN_PREFIX))
    holdings = context.user_data["cash_holdings"]
    idx = context.user_data["cash_idx"]
    margins = context.user_data["cash_margins"]

    margins.append(margin_ratio)
    idx += 1
    context.user_data["cash_idx"] = idx

    if idx < len(holdings):
        # 다음 종목 질문
        h = holdings[idx]
        await query.edit_message_text(
            f"[{idx + 1}/{len(holdings)}] {h['name']}  |  {h['quantity']}주  |  "
            f"투자금 {format_number(h['total_invested'])}원",
            reply_markup=margin_ratio_keyboard(),
        )
        return HOLDING_MARGIN

    # 모든 종목 완료 → 계산 및 저장
    capital = context.user_data.pop("cash_capital")
    context.user_data.pop("cash_holdings")
    context.user_data.pop("cash_idx")
    context.user_data.pop("cash_margins")

    # 보유종목에 credit_loan 설정 + 예수금 계산
    all_holdings = load_holdings()
    cash = capital

    active_idx = 0
    for h_dict in all_holdings:
        if h_dict.get("quantity", 0) <= 0:
            continue
        if active_idx >= len(margins):
            break
        ratio = margins[active_idx]
        invested = h_dict.get("total_invested", 0)
        credit_loan = invested * (1 - ratio / 100) if ratio < 100 else 0.0
        h_dict["credit_loan"] = credit_loan
        cash -= invested * (ratio / 100)
        active_idx += 1

    save_holdings(all_holdings)

    account = {"initial_capital": capital, "cash": cash}
    save_account(account)

    await query.edit_message_text(
        f"설정 완료!\n\n"
        f"초기자본: {format_number(capital)}원\n"
        f"예수금: {format_number(cash)}원"
    )
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 취소."""
    for key in ["cash_capital", "cash_holdings", "cash_idx", "cash_margins"]:
        context.user_data.pop(key, None)
    await update.message.reply_text("예수금 설정이 취소되었습니다.")
    return ConversationHandler.END


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(r"^(매도|매수|현황|도움말|수정)$") | filters.COMMAND


def cash_conversation() -> ConversationHandler:
    """예수금 설정 ConversationHandler."""
    other_cmd = _other_command_filter()

    return ConversationHandler(
        entry_points=[
            CommandHandler("cash", _start),
            MessageHandler(filters.Regex(r"^예수금$"), _start),
        ],
        states={
            CAPITAL_INPUT: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _capital_input),
            ],
            HOLDING_MARGIN: [
                CallbackQueryHandler(_holding_margin, pattern=f"^{MARGIN_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _cancel),
            CommandHandler("cancel", _cancel),
        ],
        name="cash",
        allow_reentry=True,
    )
