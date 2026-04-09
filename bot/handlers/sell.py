"""매도 기록 + 회고 ConversationHandler.

플로우:
  /sell → 매도 정보 입력 → 확인 → 수익률 계산 → 회고 여부 →
  (회고 시작) → 투자 판단 평가 → 잘한 점 → 아쉬운 점 → 피할 수 있었나 → 교훈 → 저장
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
    AVOIDABLE_NO,
    AVOIDABLE_UNKNOWN,
    AVOIDABLE_YES,
    SELL_SELECT_PREFIX,
    SKIP_RETRO,
    START_RETRO,
    THESIS_CORRECT,
    THESIS_PARTIAL,
    THESIS_WRONG,
    avoidable_keyboard,
    holdings_select_keyboard,
    retro_ask_keyboard,
    thesis_eval_keyboard,
)
from models.portfolio import Holding
from models.retrospective import Retrospective
from models.transaction import Transaction
from parsers.input_parser import parse_sell_input, resolve_name
from storage.json_store import (
    load_holdings,
    load_nickname_map,
    load_retrospectives,
    load_transactions,
    save_holdings,
    save_retrospectives,
    save_transactions,
)

# ConversationHandler states
(
    SELECT,
    INPUT,
    RETRO_ASK,
    RETRO_THESIS,
    RETRO_WELL,
    RETRO_REGRETS,
    RETRO_AVOIDABLE,
    RETRO_LESSONS,
) = range(8)


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

    # 선택한 종목의 보유량 표시
    holdings = load_holdings()
    qty = 0
    for h in holdings:
        if h["name"] == name:
            qty = h["quantity"]
            break

    await query.edit_message_text(
        f"[{name}] {qty}주 보유 중\n\n"
        "수량 / 매도가 / 사유\n"
        "를 줄바꿈으로 입력해주세요."
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
    """매도 공통 처리: 보유 확인 → 저장 → 회고 질문. 실패 시 error_state 반환."""
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

    context.user_data["sell_holding"] = holding_dict

    avg_price = holding_dict["avg_price"]
    total = price * quantity
    profit_loss = (price - avg_price) * quantity
    profit_loss_pct = profit_loss / (avg_price * quantity) * 100

    # Holding 업데이트
    holding = Holding.from_dict(holding_dict)
    holding.remove_sell(quantity)

    new_holdings = []
    for h in holdings:
        if h["name"].lower() == name.lower():
            if holding.quantity > 0:
                new_holdings.append(holding.to_dict())
        else:
            new_holdings.append(h)
    save_holdings(new_holdings)

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
    )
    transactions = load_transactions()
    transactions.append(tx.to_dict())
    save_transactions(transactions)

    context.user_data["sell_transaction"] = tx

    # 매도 결과 응답
    result_text = format_sell_result(
        name=name,
        quantity=quantity,
        price=price,
        total=total,
        profit_loss=profit_loss,
        profit_loss_pct=profit_loss_pct,
    )
    await update.message.reply_text(result_text)

    # 회고 여부 질문
    await update.message.reply_text(
        "이 매도에 대해 회고를 진행할까요?",
        reply_markup=retro_ask_keyboard(),
    )
    return RETRO_ASK


async def _receive_sell_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도 정보 파싱 → 즉시 저장 → 회고 여부 질문."""
    text = update.message.text
    sell_name = context.user_data.get("sell_name", "")

    # 파싱
    try:
        sell_input = parse_sell_input(text, name=sell_name)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}")
        return INPUT

    # 이름이 직접 입력된 경우 닉네임 변환
    if not sell_name:
        nmap = load_nickname_map()
        sell_input.name = resolve_name(sell_input.name, nickname_map=nmap)

    return await _process_sell(
        update, context,
        sell_input.name, sell_input.quantity, sell_input.price, sell_input.sell_reason,
    )


async def _start_retro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 6: 회고 시작 → 투자 판단 평가 질문."""
    query = update.callback_query
    await query.answer()

    holding_dict = context.user_data["sell_holding"]
    thesis = holding_dict.get("buy_thesis", "")
    thesis_display = thesis if thesis else "(기록 없음)"

    await query.edit_message_text(
        f"원래 매수 근거: '{thesis_display}'\n\n이 판단이 맞았나요?",
        reply_markup=thesis_eval_keyboard(),
    )
    return RETRO_THESIS


async def _skip_retro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """회고 건너뛰기."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("회고를 건너뛰었습니다. 나중에 다시 진행할 수 있습니다.")
    _cleanup_user_data(context)
    return ConversationHandler.END


async def _retro_thesis_eval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 6 응답: 투자 판단 평가 → 잘한 점 질문."""
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
    return RETRO_WELL


async def _retro_well(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 7: 잘한 점 입력 → 아쉬운 점 질문."""
    text = update.message.text
    context.user_data["retro_well"] = text
    await update.message.reply_text("아쉬운 점은 무엇인가요? (건너뛰려면 /skip)")
    return RETRO_REGRETS


async def _retro_regrets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 8: 아쉬운 점 입력 → 피할 수 있었나 질문."""
    text = update.message.text
    if text.strip() == "/skip":
        context.user_data["retro_regrets"] = ""
    else:
        context.user_data["retro_regrets"] = text

    await update.message.reply_text(
        "이 아쉬움은 피할 수 있었나요?",
        reply_markup=avoidable_keyboard(),
    )
    return RETRO_AVOIDABLE


async def _retro_regrets_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 8: 아쉬운 점 /skip."""
    context.user_data["retro_regrets"] = ""
    await update.message.reply_text(
        "이 아쉬움은 피할 수 있었나요?",
        reply_markup=avoidable_keyboard(),
    )
    return RETRO_AVOIDABLE


async def _retro_avoidable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 9: 피할 수 있었나 → 교훈 질문."""
    query = update.callback_query
    await query.answer()

    data = query.data
    avoidable_map = {
        AVOIDABLE_YES: "피할 수 있었다",
        AVOIDABLE_NO: "통제 불가",
        AVOIDABLE_UNKNOWN: "모르겠다",
    }
    context.user_data["retro_avoidable"] = avoidable_map.get(data, "모르겠다")

    await query.edit_message_text("이번 거래에서 얻은 교훈은? (건너뛰려면 /skip)")
    return RETRO_LESSONS


async def _retro_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 10-12: 교훈 입력 → Retrospective 저장."""
    text = update.message.text
    if text.strip() == "/skip":
        context.user_data["retro_lessons"] = ""
    else:
        context.user_data["retro_lessons"] = text

    return await _save_retrospective(update, context)


async def _retro_lessons_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 10: 교훈 /skip."""
    context.user_data["retro_lessons"] = ""
    return await _save_retrospective(update, context)


async def _save_retrospective(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Retrospective 저장 및 Transaction 연결."""
    tx: Transaction = context.user_data["sell_transaction"]
    holding_dict = context.user_data["sell_holding"]

    retro = Retrospective(
        transaction_id=tx.id,
        stock_name=tx.name,
        sell_date=tx.date,
        original_thesis=holding_dict.get("buy_thesis", ""),
        thesis_correct=context.user_data.get("retro_thesis_correct"),
        what_went_well=context.user_data.get("retro_well", ""),
        regrets=context.user_data.get("retro_regrets", ""),
        avoidable=context.user_data.get("retro_avoidable", ""),
        lessons=context.user_data.get("retro_lessons", ""),
    )

    # Retrospective 저장
    retrospectives = load_retrospectives()
    retrospectives.append(retro.to_dict())
    save_retrospectives(retrospectives)

    # Transaction에 retrospective_id 연결
    transactions = load_transactions()
    for t in transactions:
        if t["id"] == tx.id:
            t["retrospective_id"] = retro.id
            break
    save_transactions(transactions)

    await update.message.reply_text("회고 저장 완료!")
    _cleanup_user_data(context)
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 중 /cancel로 전체 취소."""
    await update.message.reply_text("매도/회고가 취소되었습니다.")
    _cleanup_user_data(context)
    return ConversationHandler.END


def _cleanup_user_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """매도/회고 관련 user_data 정리."""
    keys = [
        "sell_name",
        "sell_input",
        "sell_holding",
        "sell_profit_loss",
        "sell_profit_loss_pct",
        "sell_transaction",
        "retro_thesis_correct",
        "retro_well",
        "retro_regrets",
        "retro_avoidable",
        "retro_lessons",
    ]
    for key in keys:
        context.user_data.pop(key, None)


def _other_command_filter() -> filters.BaseFilter:
    """다른 명령어 필터 — 매도 대화 중 다른 명령 입력 시 대화 종료용."""
    return filters.Regex(r"^(매도|매수|현황|도움말|수정)$") | filters.COMMAND


def sell_conversation() -> ConversationHandler:
    """매도 + 회고 ConversationHandler를 생성하여 반환."""
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
            RETRO_ASK: [
                CallbackQueryHandler(_start_retro, pattern=f"^{START_RETRO}$"),
                CallbackQueryHandler(_skip_retro, pattern=f"^{SKIP_RETRO}$"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            RETRO_THESIS: [
                CallbackQueryHandler(
                    _retro_thesis_eval,
                    pattern=f"^({THESIS_CORRECT}|{THESIS_WRONG}|{THESIS_PARTIAL})$",
                ),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            RETRO_WELL: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _retro_well),
            ],
            RETRO_REGRETS: [
                MessageHandler(other_cmd, _cancel),
                CommandHandler("skip", _retro_regrets_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _retro_regrets),
            ],
            RETRO_AVOIDABLE: [
                CallbackQueryHandler(
                    _retro_avoidable,
                    pattern=f"^({AVOIDABLE_YES}|{AVOIDABLE_NO}|{AVOIDABLE_UNKNOWN})$",
                ),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            RETRO_LESSONS: [
                MessageHandler(other_cmd, _cancel),
                CommandHandler("skip", _retro_lessons_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _retro_lessons),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _cancel),
            CommandHandler("cancel", _cancel),
        ],
        name="sell",
        allow_reentry=True,
    )
