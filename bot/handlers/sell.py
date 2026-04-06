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
    SKIP_RETRO,
    START_RETRO,
    THESIS_CORRECT,
    THESIS_PARTIAL,
    THESIS_WRONG,
    avoidable_keyboard,
    retro_ask_keyboard,
    thesis_eval_keyboard,
)
from models.portfolio import Holding
from models.retrospective import Retrospective
from models.transaction import Transaction
from parsers.input_parser import parse_sell_input
from storage.json_store import (
    load_holdings,
    load_retrospectives,
    load_transactions,
    save_holdings,
    save_retrospectives,
    save_transactions,
)

# ConversationHandler states
(
    INPUT,
    RETRO_ASK,
    RETRO_THESIS,
    RETRO_WELL,
    RETRO_REGRETS,
    RETRO_AVOIDABLE,
    RETRO_LESSONS,
) = range(7)


async def _start_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Step 1-2: /sell 커맨드 → 매도 정보 입력 안내."""
    await update.message.reply_text(
        "매도 정보를 입력해주세요:\n\n"
        "종목명\n수량(예: 5주)\n매도가(예: 85000원)\n매도 사유\n\n"
        "예시:\n삼성전자\n5주\n85000원\n목표가 도달"
    )
    return INPUT


async def _receive_sell_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도 정보 파싱 → 보유 종목 확인 → 즉시 저장 → 회고 여부 질문."""
    text = update.message.text

    # 파싱
    try:
        sell_input = parse_sell_input(text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}")
        return INPUT

    # 보유 종목 확인
    holdings = load_holdings()
    holding_dict = None
    for h in holdings:
        if h["name"] == sell_input.name:
            holding_dict = h
            break

    if holding_dict is None:
        await update.message.reply_text(
            f"'{sell_input.name}'은(는) 보유 종목이 아닙니다. 종목명을 확인해주세요."
        )
        return INPUT

    if sell_input.quantity > holding_dict["quantity"]:
        await update.message.reply_text(
            f"'{sell_input.name}' 보유량은 {holding_dict['quantity']}주입니다. "
            f"{sell_input.quantity}주를 매도할 수 없습니다."
        )
        return INPUT

    context.user_data["sell_holding"] = holding_dict

    avg_price = holding_dict["avg_price"]
    total = sell_input.price * sell_input.quantity
    profit_loss = (sell_input.price - avg_price) * sell_input.quantity
    profit_loss_pct = profit_loss / (avg_price * sell_input.quantity) * 100

    # Holding 업데이트
    holding = Holding.from_dict(holding_dict)
    holding.remove_sell(sell_input.quantity)

    new_holdings = []
    for h in holdings:
        if h["name"] == sell_input.name:
            if holding.quantity > 0:
                new_holdings.append(holding.to_dict())
        else:
            new_holdings.append(h)
    save_holdings(new_holdings)

    # Transaction 생성 및 저장
    tx = Transaction(
        type="sell",
        name=sell_input.name,
        sector=holding_dict.get("sector", ""),
        price=sell_input.price,
        quantity=sell_input.quantity,
        total_amount=total,
        profit_loss=profit_loss,
        profit_loss_pct=round(profit_loss_pct, 2),
        sell_reason=sell_input.sell_reason,
        holding_id=holding_dict.get("id", ""),
    )
    transactions = load_transactions()
    transactions.append(tx.to_dict())
    save_transactions(transactions)

    context.user_data["sell_transaction"] = tx

    # 매도 결과 응답
    result_text = format_sell_result(
        name=sell_input.name,
        quantity=sell_input.quantity,
        price=sell_input.price,
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


def sell_conversation() -> ConversationHandler:
    """매도 + 회고 ConversationHandler를 생성하여 반환."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("sell", _start_sell),
            MessageHandler(filters.Regex(r"^매도$"), _start_sell),
        ],
        states={
            INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_sell_input),
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
        fallbacks=[
            CommandHandler("cancel", _cancel),
        ],
    )
