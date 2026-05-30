"""매도 기록 ConversationHandler.

플로우:
  /sell → 보유 종목 카드 → 선택 → 수량/매도가/사유 입력 → 저장 → 종료

회고는 별도 명령어 `/회고`(retro 핸들러)에서 진행한다.
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

from bot.formatters import format_sell_result
from bot.keyboards import (
    REASON_PICK_PREFIX,
    SELL_PINNED_REASONS,
    SELL_SELECT_PREFIX,
    holdings_select_keyboard,
    reason_select_keyboard,
)
from models.portfolio import Holding
from models.transaction import Transaction
from parsers.input_parser import parse_sell_input, resolve_name
from storage.json_store import (
    get_recent_reasons,
    load_account,
    load_holdings,
    load_nickname_map,
    load_transactions,
    save_account,
    save_holdings,
    save_transactions,
)

# ConversationHandler states
SELECT, INPUT, REASON = range(3)


async def _start_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도 시작 → 보유 종목 카드 표시."""
    holdings = [h for h in load_holdings() if h.get("quantity", 0) > 0]

    if not holdings:
        await update.message.reply_text("보유 중인 종목이 없습니다.")
        return ConversationHandler.END

    await update.message.reply_text(
        "매도할 종목을 선택해주세요:",
        reply_markup=holdings_select_keyboard(holdings),
    )
    return SELECT


async def _select_holding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """종목 선택 콜백 → 매도 정보 입력 안내."""
    query = update.callback_query
    await query.answer()

    name = query.data.removeprefix(SELL_SELECT_PREFIX)
    context.user_data["sell_name"] = name

    # 선택한 종목의 보유량 + 평단 + 매수사유 표시 (왜 샀는지 상기)
    holdings = load_holdings()
    qty = 0
    avg = 0
    buy_thesis = ""
    for h in holdings:
        if h["name"] == name:
            qty = h["quantity"]
            avg = h.get("avg_price", 0)
            buy_thesis = h.get("buy_thesis", "")
            break

    thesis_line = f"\n📌 매수사유: {buy_thesis}\n" if buy_thesis else ""
    await query.edit_message_text(
        f"[{name}] {qty}주 보유 중 · 평단 {int(avg):,}원\n"
        f"{thesis_line}\n"
        "수량 / 매도가\n"
        "를 줄바꿈으로 입력해주세요.\n"
        "(사유는 다음 단계에서 선택하거나 입력합니다 — 한 메시지에 사유까지 함께 적어도 됩니다)"
    )
    return INPUT


async def _process_sell(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    name: str,
    quantity: int,
    price: float,
    sell_reason: str,
    error_state: int = INPUT,
) -> int:
    """매도 공통 처리: 보유 확인 → 저장 → 결과 응답. 실패 시 error_state 반환."""
    # 보유 종목 확인 (대소문자 무시)
    holdings = load_holdings()
    holding_dict = None
    for h in holdings:
        if h["name"].lower() == name.lower():
            holding_dict = h
            break

    if holding_dict is None:
        await update.message.reply_text(
            f"'{name}'은(는) 보유 종목이 아닙니다. 종목명을 확인해주세요."
        )
        return error_state

    if quantity > holding_dict["quantity"]:
        await update.message.reply_text(
            f"'{name}' 보유량은 {holding_dict['quantity']}주입니다. "
            f"{quantity}주를 매도할 수 없습니다."
        )
        return error_state

    avg_price = holding_dict["avg_price"]
    total = price * quantity
    profit_loss = (price - avg_price) * quantity
    profit_loss_pct = profit_loss / (avg_price * quantity) * 100

    # Holding 업데이트
    holding = Holding.from_dict(holding_dict)
    loan_repay = holding.remove_sell(quantity)

    new_holdings = []
    for h in holdings:
        if h["name"].lower() == name.lower():
            if holding.quantity > 0:
                new_holdings.append(holding.to_dict())
        else:
            new_holdings.append(h)
    save_holdings(new_holdings)

    # 예수금 가산 (매도금액 - 대출상환)
    account = load_account()
    if account.get("initial_capital"):
        cash = account.get("cash", account["initial_capital"])
        account["cash"] = cash + total - loan_repay
        save_account(account)

    # Transaction 생성 및 저장
    tx = Transaction(
        type="sell",
        name=name,
        sector=holding_dict.get("sector", ""),
        price=price,
        quantity=quantity,
        total_amount=total,
        profit_loss=profit_loss,
        profit_loss_pct=round(profit_loss_pct, 2),
        sell_reason=sell_reason,
        holding_id=holding_dict.get("id", ""),
        buy_thesis=holding_dict.get("buy_thesis", ""),
    )
    transactions = load_transactions()
    transactions.append(tx.to_dict())
    save_transactions(transactions)

    # 매도 결과 응답
    result_text = format_sell_result(
        name=name,
        quantity=quantity,
        price=price,
        total=total,
        profit_loss=profit_loss,
        profit_loss_pct=profit_loss_pct,
    )
    await update.message.reply_text(
        result_text + "\n\n회고하려면 '회고' 를 입력해주세요."
    )

    _cleanup_user_data(context)
    return ConversationHandler.END


async def _receive_sell_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도 정보 파싱. 사유가 함께 입력되면 즉시 저장,
    아니면 사유 입력 단계(REASON)로 넘어가 최근 사유 버튼을 노출.
    """
    text = update.message.text
    sell_name = context.user_data.get("sell_name", "")

    try:
        sell_input = parse_sell_input(text, name=sell_name)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}")
        return INPUT

    if not sell_name:
        nmap = load_nickname_map()
        sell_input.name = resolve_name(sell_input.name, nickname_map=nmap)

    if sell_input.sell_reason:
        return await _process_sell(
            update, context,
            sell_input.name, sell_input.quantity, sell_input.price, sell_input.sell_reason,
        )

    # 사유 미입력 → 최근 사유 버튼 + 자유 입력 단계로 이동
    context.user_data["sell_name"] = sell_input.name
    context.user_data["sell_qty"] = sell_input.quantity
    context.user_data["sell_price"] = sell_input.price

    reasons = get_recent_reasons("sell", pinned=SELL_PINNED_REASONS)
    context.user_data["recent_reasons"] = reasons

    # 매수사유 상기 — 왜 샀는지 보고 매도 사유를 적도록
    buy_thesis = ""
    for h in load_holdings():
        if h["name"].lower() == sell_input.name.lower():
            buy_thesis = h.get("buy_thesis", "")
            break
    thesis_line = f"📌 매수사유: {buy_thesis}\n\n" if buy_thesis else ""

    msg = (
        f"[{sell_input.name}] {sell_input.quantity}주 / {int(sell_input.price):,}원\n\n"
        f"{thesis_line}"
        "매도 사유를 입력해주세요.\n\n"
        "최근 사유를 선택하거나 직접 입력하세요."
    )
    await update.message.reply_text(msg, reply_markup=reason_select_keyboard(reasons))
    return REASON


async def _reason_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """최근 매도 사유 버튼 클릭."""
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])

    name = context.user_data.get("sell_name", "")
    qty = context.user_data.get("sell_qty")
    price = context.user_data.get("sell_price")
    if not name or qty is None or price is None or idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다. 다시 매도를 시작해주세요.")
        _cleanup_user_data(context)
        return ConversationHandler.END

    reason = reasons[idx]
    await query.edit_message_text(f"매도 사유: {reason}")
    return await _process_sell(query, context, name, qty, price, reason, error_state=REASON)


async def _reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도 사유 직접 입력."""
    name = context.user_data.get("sell_name", "")
    qty = context.user_data.get("sell_qty")
    price = context.user_data.get("sell_price")
    if not name or qty is None or price is None:
        await update.message.reply_text("세션이 만료되었습니다. 다시 매도를 시작해주세요.")
        _cleanup_user_data(context)
        return ConversationHandler.END

    reason = update.message.text.strip()
    return await _process_sell(update, context, name, qty, price, reason, error_state=REASON)


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 중 /cancel로 전체 취소."""
    await update.message.reply_text("매도가 취소되었습니다.")
    _cleanup_user_data(context)
    return ConversationHandler.END


def _cleanup_user_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """매도 관련 user_data 정리."""
    for key in ["sell_name", "sell_input", "sell_qty", "sell_price", "recent_reasons"]:
        context.user_data.pop(key, None)


def _other_command_filter() -> filters.BaseFilter:
    """다른 명령어 필터 — 매도 대화 중 다른 명령 입력 시 대화 종료용."""
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|help|수정|회고|자산그래프|백테스트|10억|입금|출금|입출금목록|선물진입|선물청산|선물롤오버|선물회고)$"
    ) | filters.COMMAND


def sell_conversation() -> ConversationHandler:
    """매도 ConversationHandler를 생성하여 반환."""
    other_cmd = _other_command_filter()

    return ConversationHandler(
        entry_points=[
            CommandHandler("sell", _start_sell),
            MessageHandler(filters.Regex(r"^매도$"), _start_sell),
        ],
        states={
            SELECT: [
                CallbackQueryHandler(
                    _select_holding, pattern=f"^{SELL_SELECT_PREFIX}"
                ),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            INPUT: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_sell_input),
            ],
            REASON: [
                CallbackQueryHandler(_reason_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _reason_text),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _cancel),
            CommandHandler("cancel", _cancel),
        ],
        name="sell",
        allow_reentry=True,
    )
