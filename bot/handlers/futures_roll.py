"""선물롤오버 ConversationHandler.

플로우:
  선물롤오버 → 보유 포지션 카드 → 선택
            → 차월물 결제월 선택 (현재 결제월 제외, 분기물 4개)
            → 당월물 청산가 / 차월물 진입가 / 추가 증거금 입력
            → 사유 선택/입력 (디폴트: "롤오버: M월→M'월")
            → roll_close + roll_open 처리, 양쪽 거래를 linked_tx_id로 연결
"""
from __future__ import annotations

import logging
from datetime import date

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
    FUTURES_MONTH_PREFIX,
    FUTURES_POS_PREFIX,
    REASON_PICK_PREFIX,
    futures_month_keyboard,
    futures_positions_keyboard,
    reason_select_keyboard,
)
from models.futures_position import FuturesPosition
from models.futures_transaction import FuturesTransaction
from parsers.expiry import upcoming_months
from parsers.futures_input import parse_futures_roll
from storage.json_store import (
    adjust_futures_cash,
    get_recent_futures_reasons,
    load_futures_positions,
    load_futures_transactions,
    save_futures_positions,
    save_futures_transactions,
)

logger = logging.getLogger(__name__)

SELECT, MONTH, BODY, REASON = range(4)


def _default_roll_reason(from_cm: str, to_cm: str) -> str:
    fm = f"{from_cm[4:6]}월물" if len(from_cm) == 6 else from_cm
    tm = f"{to_cm[4:6]}월물" if len(to_cm) == 6 else to_cm
    return f"롤오버: {fm}→{tm}"


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    positions = [p for p in load_futures_positions() if p.get("contracts", 0) > 0]
    if not positions:
        await update.message.reply_text("롤오버할 선물 포지션이 없습니다.")
        return ConversationHandler.END
    positions.sort(key=lambda p: p.get("expiry_date", ""))
    await update.message.reply_text(
        "롤오버할 포지션을 선택해주세요:",
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

    context.user_data["fut_roll"] = {"position_id": pid}

    # 차월물 후보 — 현재 결제월 제외
    current_cm = pos.get("contract_month", "")
    months = [m for m in upcoming_months(count=8) if m.contract_month != current_cm][:6]
    context.user_data["fut_roll_months"] = {m.contract_month: m for m in months}

    direction_kr = "롱" if pos.get("direction") == "long" else "숏"
    cm = current_cm
    cm_label = f"{cm[2:4]}년{cm[4:6]}월물" if len(cm) == 6 else cm
    await query.edit_message_text(
        f"[{pos.get('name','')} {direction_kr} {pos.get('contracts',0)}계약 ({cm_label})]\n\n"
        "옮겨갈 차월물을 선택해주세요:",
        reply_markup=futures_month_keyboard(months),
    )
    return MONTH


async def _pick_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    cm = query.data.removeprefix(FUTURES_MONTH_PREFIX)
    months_map = context.user_data.get("fut_roll_months", {})
    fm = months_map.get(cm)
    if fm is None:
        await query.edit_message_text("결제월을 다시 선택해주세요.")
        return ConversationHandler.END

    state = context.user_data.setdefault("fut_roll", {})
    state["new_contract_month"] = fm.contract_month
    state["new_expiry_date"] = fm.expiry_iso

    await query.edit_message_text(
        f"차월물: {fm.label()}\n\n"
        "당월물 청산가 / 차월물 진입가 / 추가 증거금\n"
        "을 줄바꿈으로 입력해주세요. (환급이면 증거금에 음수 가능)\n"
        "(사유는 다음 단계에서 선택/입력)"
    )
    return BODY


async def _receive_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("fut_roll") or {}
    if not state.get("position_id"):
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        return ConversationHandler.END
    try:
        body = parse_futures_roll(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}")
        return BODY

    state["close_price"] = body.close_price
    state["open_price"] = body.open_price
    state["margin_delta"] = body.margin_delta

    if body.reason:
        state["reason"] = body.reason
        return await _do_roll(update, context, is_callback=False)

    # 디폴트 사유는 자동, 사용자가 다른 사유 클릭/입력하면 그걸로 덮어쓴다
    pos = next(
        (p for p in load_futures_positions() if p.get("id") == state["position_id"]),
        None,
    )
    if pos is None:
        await update.message.reply_text("포지션을 찾을 수 없습니다.")
        return ConversationHandler.END

    default_reason = _default_roll_reason(
        pos.get("contract_month", ""), state["new_contract_month"]
    )
    state["default_reason"] = default_reason

    reasons = [default_reason] + [
        r for r in get_recent_futures_reasons("close") if r != default_reason
    ]
    context.user_data["recent_reasons"] = reasons

    await update.message.reply_text(
        f"기본 사유: '{default_reason}'\n\n"
        "다른 사유를 선택하거나 직접 입력하세요. (그대로 두려면 그냥 디폴트 버튼을 누르세요)",
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
    state = context.user_data.get("fut_roll") or {}
    if not state.get("position_id") or idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END
    state["reason"] = reasons[idx]
    return await _do_roll(query, context, is_callback=True)


async def _reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("fut_roll") or {}
    if not state.get("position_id"):
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END
    state["reason"] = update.message.text.strip()
    return await _do_roll(update, context, is_callback=False)


async def _do_roll(update, context, *, is_callback: bool) -> int:
    state = context.user_data.get("fut_roll") or {}
    pid = state["position_id"]
    new_cm = state["new_contract_month"]
    new_exp = state["new_expiry_date"]
    close_price = float(state["close_price"])
    open_price = float(state["open_price"])
    margin_delta = float(state["margin_delta"])
    reason = state.get("reason") or state.get("default_reason", "")

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
    rolling_contracts = pos.contracts  # 전량 롤오버
    notional_close = close_price * rolling_contracts * pos.multiplier

    # 1) 당월물 전량 청산
    pnl, margin_release, _ = pos.close(price=close_price, contracts=rolling_contracts)
    pnl_pct = (pnl / notional_close * 100) if notional_close else 0.0

    close_tx = FuturesTransaction(
        type="roll_close",
        name=pos.name,
        symbol=pos.symbol,
        contract_code=pos.contract_code,
        contract_month=pos.contract_month,
        expiry_date=pos.expiry_date,
        direction=pos.direction,
        contracts=rolling_contracts,
        price=close_price,
        multiplier=pos.multiplier,
        margin=margin_release,
        sector=pos.sector,
        reason=reason,
        position_id=pos.id,
        pnl=pnl,
        pnl_pct=round(pnl_pct, 2),
        buy_thesis=pos.thesis,
    )

    # 당월물 포지션 제거 (contracts=0)
    positions.pop(pos_idx)

    # 2) 차월물 진입 — 같은 기초자산/방향/차월물 포지션 있으면 추가, 없으면 신규
    new_margin = max(0.0, margin_release + margin_delta)  # 환급 + 추가입금
    existing_new = next(
        (p for p in positions
         if p.get("contracts", 0) > 0
         and p.get("direction") == pos.direction
         and p.get("contract_month") == new_cm
         and (
             (pos.symbol and p.get("symbol") == pos.symbol)
             or (not pos.symbol and p.get("name", "").lower() == pos.name.lower())
         )),
        None,
    )

    open_tx = FuturesTransaction(
        type="roll_open",
        name=pos.name,
        symbol=pos.symbol,
        contract_code="",
        contract_month=new_cm,
        expiry_date=new_exp,
        direction=pos.direction,
        contracts=rolling_contracts,
        price=open_price,
        multiplier=pos.multiplier,
        margin=new_margin,
        sector=pos.sector,
        thesis=pos.thesis,  # 진입 사유는 기존 thesis 승계
        position_id="",
        reason=reason,
    )

    if existing_new is not None:
        new_pos = FuturesPosition.from_dict(existing_new)
        new_pos.add_entry(
            price=open_price,
            contracts=rolling_contracts,
            margin=new_margin,
            transaction_id=open_tx.id,
        )
        for i, p in enumerate(positions):
            if p["id"] == new_pos.id:
                positions[i] = new_pos.to_dict()
                break
        open_tx.position_id = new_pos.id
    else:
        new_pos = FuturesPosition(
            name=pos.name,
            symbol=pos.symbol,
            contract_code="",
            contract_month=new_cm,
            expiry_date=new_exp,
            direction=pos.direction,
            contracts=rolling_contracts,
            avg_entry_price=open_price,
            initial_margin=new_margin,
            sector=pos.sector,
            thesis=pos.thesis,
            transaction_ids=[open_tx.id],
        )
        open_tx.position_id = new_pos.id
        positions.append(new_pos.to_dict())

    # 페어 연결
    close_tx.linked_tx_id = open_tx.id
    open_tx.linked_tx_id = close_tx.id

    save_futures_positions(positions)

    # 롤오버 = 당월 청산(환급증거금+실현손익) − 차월 진입증거금. 순변동 반영.
    adjust_futures_cash(pnl + margin_release - new_margin)

    txs = load_futures_transactions()
    txs.append(close_tx.to_dict())
    txs.append(open_tx.to_dict())
    save_futures_transactions(txs)

    direction_kr = "롱" if pos.direction == "long" else "숏"
    sign = "+" if pnl >= 0 else ""
    msg = (
        f"롤오버 완료!\n"
        f"{pos.name} {direction_kr} {rolling_contracts}계약\n"
        f"{pos.contract_month[2:6]} 청산 {int(close_price):,}원 → {new_cm[2:6]} 진입 {int(open_price):,}원\n"
        f"청산 실현손익: {sign}{int(pnl):,}원 ({sign}{pnl_pct:.2f}%)\n"
        f"증거금: {int(new_margin):,}원\n"
        f"사유: '{reason}'"
    )
    if is_callback:
        await update.edit_message_text(msg)
    else:
        await update.message.reply_text(msg)

    _cleanup(context)
    return ConversationHandler.END


def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("fut_roll", "fut_roll_months", "recent_reasons"):
        context.user_data.pop(k, None)


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|help|수정|회고|자산그래프|백테스트|10억|입금|출금|입출금목록|선물진입|선물청산|선물롤오버|선물회고|복기)$"
    ) | filters.COMMAND


async def _abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cleanup(context)
    await update.message.reply_text("선물 롤오버가 취소되었습니다.")
    return ConversationHandler.END


def futures_roll_conversation() -> ConversationHandler:
    other_cmd = _other_command_filter()
    return ConversationHandler(
        entry_points=[
            CommandHandler("futures_roll", _start),
            MessageHandler(filters.Regex(r"^선물롤오버$"), _start),
        ],
        states={
            SELECT: [
                CallbackQueryHandler(_select_position, pattern=f"^{FUTURES_POS_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _abort),
            ],
            MONTH: [
                CallbackQueryHandler(_pick_month, pattern=f"^{FUTURES_MONTH_PREFIX}"),
                MessageHandler(other_cmd, _abort),
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
        name="futures_roll",
        allow_reentry=True,
    )
