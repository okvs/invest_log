"""선물청산 ConversationHandler.

플로우:
  선물청산 → 보유 포지션 카드 → 선택 → 계약수/청산가 입력
            → 사유 선택/입력 → 청산 처리 → 결과 표시
"""
from __future__ import annotations

import logging

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
    FUTURES_PINNED_CLOSE_REASONS,
    FUTURES_POS_PREFIX,
    REASON_PICK_PREFIX,
    futures_positions_keyboard,
    reason_select_keyboard,
)
from models.futures_position import FuturesPosition
from models.futures_transaction import FuturesTransaction
from parsers.futures_input import parse_futures_close
from storage.json_store import (
    get_recent_futures_reasons,
    load_futures_positions,
    load_futures_transactions,
    save_futures_positions,
    save_futures_transactions,
)

logger = logging.getLogger(__name__)

SELECT, BODY, REASON = range(3)


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    positions = [p for p in load_futures_positions() if p.get("contracts", 0) > 0]
    if not positions:
        await update.message.reply_text("청산할 선물 포지션이 없습니다.")
        return ConversationHandler.END
    # 만기 임박순
    positions.sort(key=lambda p: p.get("expiry_date", ""))
    await update.message.reply_text(
        "청산할 포지션을 선택해주세요:",
        reply_markup=futures_positions_keyboard(positions),
    )
    return SELECT


async def _select_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    pid = query.data.removeprefix(FUTURES_POS_PREFIX)
    pos_dict = next((p for p in load_futures_positions() if p.get("id") == pid), None)
    if pos_dict is None:
        await query.edit_message_text("해당 포지션을 찾을 수 없습니다.")
        return ConversationHandler.END

    context.user_data["fut_close"] = {"position_id": pid}
    direction_kr = "롱" if pos_dict.get("direction") == "long" else "숏"
    contracts = pos_dict.get("contracts", 0)
    avg = pos_dict.get("avg_entry_price", 0)
    cm = pos_dict.get("contract_month", "")
    cm_label = f"{cm[2:4]}년{cm[4:6]}월물" if len(cm) == 6 else cm
    await query.edit_message_text(
        f"[{pos_dict.get('name','')} {direction_kr} {contracts}계약 ({cm_label})]\n"
        f"평균진입 {int(avg):,}원\n\n"
        "계약수 / 청산가\n"
        "를 줄바꿈으로 입력해주세요.\n"
        "(사유는 다음 단계에서 선택/입력)"
    )
    return BODY


async def _receive_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("fut_close") or {}
    pid = state.get("position_id")
    if not pid:
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        return ConversationHandler.END

    try:
        body = parse_futures_close(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}")
        return BODY

    # 보유 계약수 초과 검사
    pos_dict = next((p for p in load_futures_positions() if p.get("id") == pid), None)
    if pos_dict is None:
        await update.message.reply_text("포지션을 찾을 수 없습니다. 다시 시작해주세요.")
        return ConversationHandler.END
    if body.contracts > pos_dict.get("contracts", 0):
        await update.message.reply_text(
            f"보유 계약수 {pos_dict.get('contracts', 0)}을 초과합니다."
        )
        return BODY

    state["contracts"] = body.contracts
    state["price"] = body.price

    if body.reason:
        state["reason"] = body.reason
        return await _do_close(update, context, is_callback=False)

    reasons = get_recent_futures_reasons("close", pinned=FUTURES_PINNED_CLOSE_REASONS)
    context.user_data["recent_reasons"] = reasons
    await update.message.reply_text(
        f"[{pos_dict.get('name','')}] {body.contracts}계약 / {int(body.price):,}원\n\n"
        "청산 사유를 입력해주세요.\n\n"
        "최근 사유를 선택하거나 직접 입력하세요.",
        reply_markup=reason_select_keyboard(reasons),
    )
    return REASON


async def _reason_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    state = context.user_data.get("fut_close") or {}
    if not state.get("position_id") or idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END
    state["reason"] = reasons[idx]
    return await _do_close(query, context, is_callback=True)


async def _reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("fut_close") or {}
    if not state.get("position_id"):
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END
    state["reason"] = update.message.text.strip()
    return await _do_close(update, context, is_callback=False)


async def _do_close(update, context, *, is_callback: bool) -> int:
    state = context.user_data.get("fut_close") or {}
    pid = state["position_id"]
    contracts = int(state["contracts"])
    price = float(state["price"])
    reason = state.get("reason", "")

    positions = load_futures_positions()
    pos_idx = next((i for i, p in enumerate(positions) if p["id"] == pid), None)
    if pos_idx is None:
        msg = "포지션을 찾을 수 없습니다."
        if is_callback:
            await update.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        _cleanup(context)
        return ConversationHandler.END

    pos = FuturesPosition.from_dict(positions[pos_idx])
    notional_before = price * contracts * pos.multiplier
    pnl, margin_release, closed = pos.close(price=price, contracts=contracts)
    pnl_pct = (pnl / notional_before * 100) if notional_before else 0.0

    tx = FuturesTransaction(
        type="close",
        name=pos.name,
        symbol=pos.symbol,
        contract_code=pos.contract_code,
        contract_month=pos.contract_month,
        expiry_date=pos.expiry_date,
        direction=pos.direction,
        contracts=contracts,
        price=price,
        multiplier=pos.multiplier,
        margin=margin_release,
        sector=pos.sector,
        reason=reason,
        position_id=pos.id,
        pnl=pnl,
        pnl_pct=round(pnl_pct, 2),
        buy_thesis=pos.thesis,
    )

    # 포지션 갱신: 잔여 0이면 제거
    if pos.contracts <= 0:
        positions.pop(pos_idx)
    else:
        positions[pos_idx] = pos.to_dict()
    save_futures_positions(positions)

    txs = load_futures_transactions()
    txs.append(tx.to_dict())
    save_futures_transactions(txs)

    direction_kr = "롱" if pos.direction == "long" else "숏"
    sign = "+" if pnl >= 0 else ""
    msg = (
        f"선물 청산 완료!\n"
        f"{pos.name} {direction_kr} {contracts}계약 x {int(price):,}원\n"
        f"손익: {sign}{int(pnl):,}원 ({sign}{pnl_pct:.2f}%)\n"
        f"환급 증거금: {int(margin_release):,}원\n\n"
        "회고하려면 '선물회고' 를 입력해주세요."
    )
    if is_callback:
        await update.edit_message_text(msg)
    else:
        await update.message.reply_text(msg)

    _cleanup(context)
    return ConversationHandler.END


def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("fut_close", "recent_reasons"):
        context.user_data.pop(k, None)


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|수정|회고|자산그래프|백테스트|입금|출금|입출금목록|선물진입|선물청산|선물롤오버|선물회고)$"
    ) | filters.COMMAND


async def _abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cleanup(context)
    await update.message.reply_text("선물 청산이 취소되었습니다.")
    return ConversationHandler.END


def futures_close_conversation() -> ConversationHandler:
    other_cmd = _other_command_filter()
    return ConversationHandler(
        entry_points=[
            CommandHandler("futures_sell", _start),
            MessageHandler(filters.Regex(r"^선물청산$"), _start),
        ],
        states={
            SELECT: [
                CallbackQueryHandler(_select_position, pattern=f"^{FUTURES_POS_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _abort),
            ],
            BODY: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_body),
            ],
            REASON: [
                CallbackQueryHandler(_reason_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _reason_text),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _abort),
            CommandHandler("cancel", _abort),
        ],
        name="futures_close",
        allow_reentry=True,
    )
