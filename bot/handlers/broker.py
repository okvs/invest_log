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
from bot.handlers.sell import _process_sell
from bot.handlers.buy import (
    _process_and_save,
    _find_existing_holding,
    _strip_name,
)
from bot.keyboards import (
    APPEND_THESIS,
    EDIT_SECTOR,
    EDIT_THESIS,
    FUTURES_PINNED_CLOSE_REASONS,
    KEEP_EXISTING,
    MARGIN_PREFIX,
    REASON_PICK_PREFIX,
    SELL_PINNED_REASONS,
    existing_info_keyboard,
    margin_ratio_keyboard,
    reason_select_keyboard,
)
from models.futures_position import FuturesPosition
from models.futures_transaction import FuturesTransaction
from parsers.expiry import second_thursday
from parsers.futures_input import _parse_number as _parse_money
from parsers.input_parser import (
    BuyInput,
    FuturesBrokerMessage,
    parse_broker_message,
    resolve_name,
)
from storage.json_store import (
    get_recent_futures_reasons,
    get_recent_reasons,
    load_account,
    load_futures_positions,
    load_futures_transactions,
    load_nickname_map,
    save_futures_positions,
    save_futures_transactions,
)

# ConversationHandler states (10~부터 시작하여 buy/sell 상태값과 충돌 방지)
SELL_REASON = 10
BUY_SECTOR = 11
BUY_THESIS = 12
BROKER_EXISTING_CONFIRM = 13
BROKER_MARGIN_SELECT = 14
BUY_THESIS_APPEND = 15
# 선물 분기
FUT_MARGIN = 20
FUT_THESIS = 21
FUT_CLOSE_REASON = 22


def _end_other_conversations(
    context: ContextTypes.DEFAULT_TYPE, update: Update, keep: str = "broker"
) -> None:
    """다른 ConversationHandler에 남아있는 orphan 상태를 정리.

    사용자가 '매수'/'매도'/'수정' 명령으로 buy/sell/edit 대화를 시작한 뒤
    broker 메시지를 붙여넣으면, broker가 update를 가로채기 때문에 기존 대화는
    해당 update를 보지 못하고 state가 남은 채로 방치됨.
    이후 '매도' 같은 다른 명령을 입력하면 고아 상태에 걸려서
    '매수 기록이 취소되었습니다' 메시지가 뜨는 문제가 생김.
    broker 대화가 시작될 때 이 함수를 호출해 다른 ConversationHandler의
    state를 명시적으로 비워준다.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    key = (chat.id, user.id)
    app = context.application
    for handlers in app.handlers.values():
        for handler in handlers:
            if (
                isinstance(handler, ConversationHandler)
                and handler.name != keep
                and key in handler._conversations
            ):
                handler._conversations.pop(key, None)


async def _receive_broker_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """증권사 메시지 파싱 → 매수/매도(현물) 또는 진입/청산(선물) 분기."""
    _end_other_conversations(context, update, keep="broker")
    text = update.message.text

    try:
        msg = parse_broker_message(text)
    except ValueError as e:
        await update.message.reply_text(f"메시지 인식 실패: {e}")
        return ConversationHandler.END

    # 닉네임 변환 + 공백 제거
    nmap = load_nickname_map()
    msg.name = resolve_name(msg.name, nickname_map=nmap)

    # 선물 분기
    if isinstance(msg, FuturesBrokerMessage):
        return await _handle_futures_broker_msg(update, context, msg)

    if msg.trade_type == "sell":
        context.user_data["broker_sell"] = msg
        reasons = get_recent_reasons("sell", pinned=SELL_PINNED_REASONS)
        context.user_data["recent_reasons"] = reasons
        prompt = (
            f"{msg.name} {msg.quantity}주 {int(msg.price):,}원 매도 체결 확인.\n\n"
            "매도사유를 입력해주세요.\n\n"
            "최근 사유를 선택하거나 직접 입력하세요."
        )
        await update.message.reply_text(
            prompt,
            reply_markup=reason_select_keyboard(reasons),
        )
        return SELL_REASON
    else:
        # 기존 보유 종목 확인
        existing = _find_existing_holding("", msg.name)
        if existing:
            sector = existing.get("sector", "") or "(없음)"
            thesis = existing.get("buy_thesis", "") or "(없음)"

            buy_input = BuyInput(
                name=msg.name,
                ticker=existing.get("ticker", ""),
                sector=existing.get("sector", ""),
                quantity=msg.quantity,
                price=msg.price,
                thesis=existing.get("buy_thesis", ""),
            )
            context.user_data["buy_input"] = buy_input

            await update.message.reply_text(
                f"{msg.name} {msg.quantity}주 {int(msg.price):,}원 매수 체결 확인.\n\n"
                f"기존 보유 종목입니다.\n\n"
                f"섹터: {sector}\n"
                f"매수사유: {thesis}\n\n"
                f"그대로 유지하거나 수정할 항목을 선택해주세요.",
                reply_markup=existing_info_keyboard(),
            )
            return BROKER_EXISTING_CONFIRM
        else:
            context.user_data["broker_buy"] = msg
            await update.message.reply_text(
                f"{msg.name} {msg.quantity}주 {int(msg.price):,}원 매수 체결 확인.\n\n"
                "섹터를 입력해주세요. (예: 반도체, IT, 바이오)"
            )
            return BUY_SECTOR


async def _sell_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매도사유 직접 입력 → 매도 처리."""
    reason = update.message.text.strip()
    msg = context.user_data.pop("broker_sell")
    context.user_data.pop("recent_reasons", None)

    return await _process_sell(
        update, context,
        msg.name, msg.quantity, msg.price, reason,
        error_state=ConversationHandler.END,
    )


async def _sell_reason_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """최근 매도 사유 버튼 클릭 → 그 사유로 매도 처리."""
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    msg = context.user_data.pop("broker_sell", None)
    context.user_data.pop("recent_reasons", None)

    if msg is None or idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    reason = reasons[idx]
    await query.edit_message_text(f"매도 사유: {reason}")
    return await _process_sell(
        query, context,
        msg.name, msg.quantity, msg.price, reason,
        error_state=ConversationHandler.END,
    )


async def _broker_ask_margin(update, context, buy_input, *, is_callback=False) -> int:
    """증거금비율 질문. 계좌 미설정 시 바로 저장."""
    account = load_account()
    if not account.get("initial_capital"):
        result_text = _process_and_save(buy_input)
        if is_callback:
            query = update.callback_query if hasattr(update, 'callback_query') and update.callback_query else update
            await query.edit_message_text(result_text)
        else:
            await update.message.reply_text(result_text)
        return ConversationHandler.END

    context.user_data["buy_input"] = buy_input
    msg = "증거금비율을 선택해주세요."
    if is_callback:
        query = update.callback_query if hasattr(update, 'callback_query') and update.callback_query else update
        await query.edit_message_text(msg, reply_markup=margin_ratio_keyboard())
    else:
        await update.message.reply_text(msg, reply_markup=margin_ratio_keyboard())
    return BROKER_MARGIN_SELECT


async def _broker_margin_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """증거금비율 선택 후 매수 저장."""
    query = update.callback_query
    await query.answer()

    margin_ratio = int(query.data.removeprefix(MARGIN_PREFIX))
    buy_input = context.user_data.pop("buy_input", None)
    if buy_input is None:
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    result_text = _process_and_save(buy_input, margin_ratio=margin_ratio)
    await query.edit_message_text(result_text)
    return ConversationHandler.END


async def _broker_existing_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """증권사 매수: 기존 보유 종목 섹터/근거 유지·이어쓰기·대체."""
    query = update.callback_query
    await query.answer()

    buy_input = context.user_data.get("buy_input")
    if buy_input is None:
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    if query.data == KEEP_EXISTING:
        return await _broker_ask_margin(update, context, buy_input, is_callback=True)
    elif query.data == EDIT_SECTOR:
        await query.edit_message_text("새로운 섹터를 입력해주세요.")
        context.user_data["_broker_edit"] = "sector"
        return BUY_SECTOR
    elif query.data == APPEND_THESIS:
        existing_thesis = buy_input.thesis or ""
        context.user_data["append_base"] = existing_thesis
        preview = existing_thesis if existing_thesis else "(기존 사유 없음)"
        await query.edit_message_text(
            f"기존 매수사유:\n{preview}\n\n이어붙일 내용을 입력해주세요."
        )
        return BUY_THESIS_APPEND
    else:  # EDIT_THESIS — 대체
        context.user_data["_broker_edit"] = "thesis"
        return await _broker_ask_thesis(
            query, context, is_callback=True,
            prompt="새로운 매수 근거를 입력해주세요.",
        )


async def _buy_sector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """섹터 입력."""
    sector = update.message.text.strip()

    # 기존 보유 종목에서 섹터 수정 중인 경우 → 바로 저장
    edit_mode = context.user_data.pop("_broker_edit", None)
    if edit_mode == "sector":
        buy_input = context.user_data.pop("buy_input", None)
        if buy_input:
            buy_input.sector = sector
            return await _broker_ask_margin(update, context, buy_input)

    # 신규 종목 → 매수 근거 입력 (최근 사유 버튼)
    context.user_data["broker_sector"] = sector
    return await _broker_ask_thesis(update, context, is_callback=False)


async def _broker_ask_thesis(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    is_callback: bool,
    prompt: str = "매수 근거를 입력해주세요.",
) -> int:
    """증권사 매수의 매수근거 입력 프롬프트 (최근 사유 버튼 포함)."""
    reasons = get_recent_reasons("buy")
    context.user_data["recent_reasons"] = reasons

    msg = prompt
    if reasons:
        msg += "\n\n최근 사유를 선택하거나 직접 입력하세요."
    keyboard = reason_select_keyboard(reasons) if reasons else None

    if is_callback:
        await update_or_query.edit_message_text(msg, reply_markup=keyboard)
    else:
        await update_or_query.message.reply_text(msg, reply_markup=keyboard)
    return BUY_THESIS


async def _buy_thesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매수근거 직접 입력 → 매수 저장."""
    thesis = update.message.text.strip()
    context.user_data.pop("recent_reasons", None)

    # 기존 보유 종목에서 근거 수정 중인 경우 → 바로 저장
    edit_mode = context.user_data.pop("_broker_edit", None)
    if edit_mode == "thesis":
        buy_input = context.user_data.pop("buy_input", None)
        if buy_input:
            buy_input.thesis = thesis
            return await _broker_ask_margin(update, context, buy_input)

    # 신규 종목
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

    return await _broker_ask_margin(update, context, buy_input)


async def _buy_thesis_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """최근 매수 사유 버튼 클릭 → 그 사유로 저장."""
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    context.user_data.pop("recent_reasons", None)

    if idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    thesis = reasons[idx]

    edit_mode = context.user_data.pop("_broker_edit", None)
    if edit_mode == "thesis":
        buy_input = context.user_data.pop("buy_input", None)
        if buy_input:
            buy_input.thesis = thesis
            return await _broker_ask_margin(query, context, buy_input, is_callback=True)

    # 신규 종목
    msg = context.user_data.pop("broker_buy", None)
    sector = context.user_data.pop("broker_sector", "")
    if msg is None:
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    buy_input = BuyInput(
        name=msg.name,
        ticker="",
        sector=sector,
        quantity=msg.quantity,
        price=msg.price,
        thesis=thesis,
    )
    return await _broker_ask_margin(query, context, buy_input, is_callback=True)


async def _buy_thesis_append(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """기존 매수사유에 이어쓰기 → 결합 후 저장."""
    extra = update.message.text.strip()
    base = context.user_data.pop("append_base", "")
    buy_input = context.user_data.pop("buy_input", None)
    context.user_data.pop("_broker_edit", None)

    if buy_input is None:
        await update.message.reply_text("세션이 만료되었습니다.")
        return ConversationHandler.END

    buy_input.thesis = f"{base}\n{extra}" if base else extra
    return await _broker_ask_margin(update, context, buy_input)


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 취소."""
    for key in (
        "broker_sell", "broker_buy", "broker_sector",
        "buy_input", "_broker_edit", "recent_reasons", "append_base",
        "fut_msg", "fut_action", "fut_direction", "fut_margin",
        "fut_add_pos_id", "fut_close_pos_id", "fut_close_contracts",
        "fut_close_price",
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text("취소되었습니다.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# 선물 체결 메시지 처리
# ---------------------------------------------------------------------------


def _find_futures_position(
    name: str, contract_month: str, direction: str,
) -> dict | None:
    """같은 기초자산·결제월·방향 포지션을 반환."""
    name_key = name.replace(" ", "").lower()
    for p in load_futures_positions():
        if p.get("contracts", 0) <= 0:
            continue
        if (
            p.get("name", "").replace(" ", "").lower() == name_key
            and p.get("contract_month") == contract_month
            and p.get("direction") == direction
        ):
            return p
    return None


def _resolve_futures_action(
    msg: FuturesBrokerMessage,
) -> tuple[str, str, dict | None]:
    """체결 메시지로 액션 자동 추론.

    Returns (action, direction, existing_pos):
      action: "new" | "add" | "close"
      direction:
        - new/add: 신규 진입 방향
        - close: 청산되는 기존 포지션 방향
    """
    name = msg.name
    cm = msg.contract_month
    if msg.trade_type == "buy":
        short_pos = _find_futures_position(name, cm, "short")
        if short_pos:
            return "close", "short", short_pos
        long_pos = _find_futures_position(name, cm, "long")
        if long_pos:
            return "add", "long", long_pos
        return "new", "long", None
    else:
        long_pos = _find_futures_position(name, cm, "long")
        if long_pos:
            return "close", "long", long_pos
        short_pos = _find_futures_position(name, cm, "short")
        if short_pos:
            return "add", "short", short_pos
        return "new", "short", None


def _format_msg_summary(msg: FuturesBrokerMessage) -> str:
    cm = msg.contract_month
    cm_label = f"{cm[2:4]}년{cm[4:6]}월물" if len(cm) == 6 else cm
    side = "매수" if msg.trade_type == "buy" else "매도"
    pps = msg.price_per_share()
    total = msg.total_amount()
    return (
        f"[KB 선물 체결] {msg.name} {cm_label} {side} {msg.quantity}계약\n"
        f"단가 {int(pps):,}원 × {msg.quantity}계약 × {msg.multiplier}승수 "
        f"= 총 {int(total):,}원"
    )


async def _handle_futures_broker_msg(
    update: Update, context: ContextTypes.DEFAULT_TYPE, msg: FuturesBrokerMessage,
) -> int:
    action, direction, existing = _resolve_futures_action(msg)
    context.user_data["fut_msg"] = msg
    context.user_data["fut_action"] = action
    context.user_data["fut_direction"] = direction

    direction_kr = "롱" if direction == "long" else "숏"
    summary = _format_msg_summary(msg)

    if action == "close":
        context.user_data["fut_close_pos_id"] = existing["id"]
        context.user_data["fut_close_contracts"] = msg.quantity
        context.user_data["fut_close_price"] = msg.price_per_share()
        reasons = get_recent_futures_reasons(
            "close", pinned=FUTURES_PINNED_CLOSE_REASONS
        )
        context.user_data["recent_reasons"] = reasons
        await update.message.reply_text(
            f"{summary}\n\n"
            f"기존 {direction_kr} 포지션 청산으로 처리합니다.\n"
            "청산 사유를 입력해주세요.\n\n"
            "최근 사유를 선택하거나 직접 입력하세요.",
            reply_markup=reason_select_keyboard(reasons),
        )
        return FUT_CLOSE_REASON

    if action == "add":
        context.user_data["fut_add_pos_id"] = existing["id"]
        await update.message.reply_text(
            f"{summary}\n\n"
            f"기존 {direction_kr} 포지션에 추가 진입으로 처리합니다.\n"
            "추가 위탁증거금(원)을 입력해주세요."
        )
        return FUT_MARGIN

    # new
    await update.message.reply_text(
        f"{summary}\n\n"
        f"신규 {direction_kr} 진입으로 처리합니다.\n"
        "위탁증거금(원)을 입력해주세요."
    )
    return FUT_MARGIN


async def _fut_margin_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> int:
    text = update.message.text
    try:
        margin = _parse_money(text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}")
        return FUT_MARGIN
    if margin <= 0:
        await update.message.reply_text("증거금은 0보다 커야 합니다.")
        return FUT_MARGIN
    context.user_data["fut_margin"] = margin

    reasons = get_recent_futures_reasons("open")
    context.user_data["recent_reasons"] = reasons
    msg = "진입 사유를 입력해주세요."
    if reasons:
        msg += "\n\n최근 사유를 선택하거나 직접 입력하세요."
    await update.message.reply_text(
        msg, reply_markup=reason_select_keyboard(reasons) if reasons else None,
    )
    return FUT_THESIS


async def _fut_thesis_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> int:
    thesis = update.message.text.strip()
    context.user_data.pop("recent_reasons", None)
    return await _do_open_save(update, context, thesis, is_callback=False)


async def _fut_thesis_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    context.user_data.pop("recent_reasons", None)
    if idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END
    return await _do_open_save(query, context, reasons[idx], is_callback=True)


async def _do_open_save(
    update_or_query, context: ContextTypes.DEFAULT_TYPE, thesis: str,
    *, is_callback: bool,
) -> int:
    msg: FuturesBrokerMessage = context.user_data.pop("fut_msg")
    direction = context.user_data.pop("fut_direction")
    margin = float(context.user_data.pop("fut_margin"))
    action = context.user_data.pop("fut_action")
    add_pos_id = context.user_data.pop("fut_add_pos_id", None)

    price = msg.price_per_share()
    contracts = msg.quantity
    year = int(msg.contract_month[:4])
    month = int(msg.contract_month[4:6])
    expiry_date = second_thursday(year, month).isoformat()

    tx = FuturesTransaction(
        type="open",
        name=msg.name,
        symbol="",
        contract_code="",
        contract_month=msg.contract_month,
        expiry_date=expiry_date,
        direction=direction,
        contracts=contracts,
        price=price,
        multiplier=msg.multiplier,
        margin=margin,
        thesis=thesis,
    )

    positions = load_futures_positions()
    if action == "add" and add_pos_id:
        idx = next((i for i, p in enumerate(positions) if p["id"] == add_pos_id), None)
        if idx is None:
            await _reply(update_or_query, "기존 포지션을 찾지 못해 신규 진입으로 처리합니다.", is_callback)
            action = "new"
        else:
            pos = FuturesPosition.from_dict(positions[idx])
            pos.add_entry(price=price, contracts=contracts, margin=margin, transaction_id=tx.id)
            if thesis:
                pos.thesis = thesis
            positions[idx] = pos.to_dict()
            tx.position_id = pos.id

    if action == "new":
        pos = FuturesPosition(
            name=msg.name,
            symbol="",
            contract_code="",
            contract_month=msg.contract_month,
            expiry_date=expiry_date,
            direction=direction,
            contracts=contracts,
            avg_entry_price=price,
            initial_margin=margin,
            multiplier=msg.multiplier,
            thesis=thesis,
            transaction_ids=[tx.id],
        )
        tx.position_id = pos.id
        positions.append(pos.to_dict())

    save_futures_positions(positions)
    txs = load_futures_transactions()
    txs.append(tx.to_dict())
    save_futures_transactions(txs)

    direction_kr = "롱" if direction == "long" else "숏"
    label = "신규" if action == "new" else "추가"
    cm_label = f"{msg.contract_month[2:4]}년 {msg.contract_month[4:6]}월물"
    result = (
        f"선물 {label} 진입 완료!\n"
        f"{msg.name} {direction_kr} {contracts}계약 ({cm_label})\n"
        f"단가: {int(price):,}원  |  증거금: {int(margin):,}원\n"
        f'사유: "{thesis}"'
    )
    await _reply(update_or_query, result, is_callback)
    return ConversationHandler.END


async def _fut_close_reason_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> int:
    reason = update.message.text.strip()
    context.user_data.pop("recent_reasons", None)
    return await _do_close_save(update, context, reason, is_callback=False)


async def _fut_close_reason_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    context.user_data.pop("recent_reasons", None)
    if idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다.")
        return ConversationHandler.END
    return await _do_close_save(query, context, reasons[idx], is_callback=True)


async def _do_close_save(
    update_or_query, context: ContextTypes.DEFAULT_TYPE, reason: str,
    *, is_callback: bool,
) -> int:
    msg = context.user_data.pop("fut_msg")
    pid = context.user_data.pop("fut_close_pos_id")
    contracts = int(context.user_data.pop("fut_close_contracts"))
    price = float(context.user_data.pop("fut_close_price"))
    context.user_data.pop("fut_action", None)
    context.user_data.pop("fut_direction", None)

    positions = load_futures_positions()
    pos_idx = next((i for i, p in enumerate(positions) if p["id"] == pid), None)
    if pos_idx is None:
        await _reply(update_or_query, "포지션을 찾을 수 없습니다.", is_callback)
        return ConversationHandler.END

    pos = FuturesPosition.from_dict(positions[pos_idx])
    actual = min(contracts, pos.contracts)
    notional = price * actual * pos.multiplier
    pnl, margin_release, _ = pos.close(price=price, contracts=actual)
    pnl_pct = (pnl / notional * 100) if notional else 0.0

    tx = FuturesTransaction(
        type="close",
        name=pos.name,
        symbol=pos.symbol,
        contract_code=pos.contract_code,
        contract_month=pos.contract_month,
        expiry_date=pos.expiry_date,
        direction=pos.direction,
        contracts=actual,
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
    result = (
        f"선물 청산 완료!\n"
        f"{pos.name} {direction_kr} {actual}계약 x {int(price):,}원\n"
        f"손익: {sign}{int(pnl):,}원 ({sign}{pnl_pct:.2f}%)\n"
        f"환급 증거금: {int(margin_release):,}원\n\n"
        "회고하려면 '선물회고' 를 입력해주세요."
    )
    await _reply(update_or_query, result, is_callback)
    return ConversationHandler.END


async def _reply(update_or_query, text: str, is_callback: bool) -> None:
    if is_callback:
        await update_or_query.edit_message_text(text)
    else:
        await update_or_query.message.reply_text(text)


def _other_command_filter() -> filters.BaseFilter:
    """다른 명령어 필터 — 대화 중 다른 명령 입력 시 대화 종료용."""
    return filters.Regex(
        r"^(매도|매수|현황|도움말|수정|회고|선물진입|선물청산|선물롤오버|선물회고)$"
    ) | filters.COMMAND


def broker_conversation() -> ConversationHandler:
    """증권사 체결 메시지 ConversationHandler."""
    other_cmd = _other_command_filter()

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
                CallbackQueryHandler(_sell_reason_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _sell_reason),
            ],
            BUY_SECTOR: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buy_sector),
            ],
            BUY_THESIS: [
                CallbackQueryHandler(_buy_thesis_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buy_thesis),
            ],
            BUY_THESIS_APPEND: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _buy_thesis_append),
            ],
            BROKER_EXISTING_CONFIRM: [
                CallbackQueryHandler(
                    _broker_existing_confirm,
                    pattern=f"^({KEEP_EXISTING}|{EDIT_SECTOR}|{EDIT_THESIS}|{APPEND_THESIS})$",
                ),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            BROKER_MARGIN_SELECT: [
                CallbackQueryHandler(_broker_margin_selected, pattern=f"^{MARGIN_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            FUT_MARGIN: [
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _fut_margin_input),
            ],
            FUT_THESIS: [
                CallbackQueryHandler(_fut_thesis_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _fut_thesis_input),
            ],
            FUT_CLOSE_REASON: [
                CallbackQueryHandler(_fut_close_reason_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _fut_close_reason_input),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _cancel),
            CommandHandler("cancel", _cancel),
        ],
        name="broker",
        allow_reentry=True,
    )
