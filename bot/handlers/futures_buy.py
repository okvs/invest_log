"""선물진입 ConversationHandler.

플로우:
  선물진입 → 종목명 입력 → 종목검색(자동/카드선택)
            → 방향 버튼(롱/숏) → 결제월 버튼(분기물 4개)
            → 계약수/진입가/위탁증거금 입력
            → 사유 선택/입력 → 저장
"""
from __future__ import annotations

import asyncio
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
    APPEND_THESIS,
    BUY_STOCK_PREFIX,
    EDIT_THESIS,
    FUTURES_DIR_PREFIX,
    FUTURES_LONG,
    FUTURES_MONTH_PREFIX,
    FUTURES_SHORT,
    KEEP_EXISTING,
    REASON_PICK_PREFIX,
    futures_direction_keyboard,
    futures_existing_thesis_keyboard,
    futures_month_keyboard,
    reason_select_keyboard,
    stock_search_keyboard,
)
from models.futures_position import FuturesPosition, DEFAULT_MULTIPLIER
from models.futures_transaction import FuturesTransaction
from parsers.expiry import upcoming_months
from parsers.futures_input import parse_futures_entry
from parsers.input_parser import resolve_name, search_stocks
from storage.json_store import (
    get_recent_futures_reasons,
    load_futures_positions,
    load_futures_transactions,
    load_nickname_map,
    load_ticker_map,
    save_futures_positions,
    save_futures_transactions,
    save_ticker_map,
)

logger = logging.getLogger(__name__)

# ConversationHandler states
NAME, PICK_STOCK, DIRECTION, MONTH, BODY, SECTOR, REASON, EXISTING_THESIS, APPEND_INPUT = range(9)


def _strip_name(name: str) -> str:
    return name.replace(" ", "")


def _pick_existing_position(symbol: str, name: str, direction: str, contract_month: str) -> dict | None:
    """같은 기초자산·방향·결제월 포지션이 있으면 반환 (추가 진입용)."""
    for p in load_futures_positions():
        if p.get("contracts", 0) <= 0:
            continue
        if p.get("direction") != direction or p.get("contract_month") != contract_month:
            continue
        if symbol and p.get("symbol") == symbol:
            return p
        if not symbol and p.get("name", "").lower() == name.lower():
            return p
    return None


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["fut_entry"] = {}
    await update.message.reply_text("기초자산 종목명을 입력해주세요. (예: 삼성전자)")
    return NAME


async def _receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = _strip_name(update.message.text.strip())
    nmap = load_nickname_map()
    name = resolve_name(text, nickname_map=nmap)

    tmap = load_ticker_map()
    cached = tmap.get(name, "") or next(
        (v for k, v in tmap.items() if k.lower() == name.lower()), ""
    )

    state = context.user_data.setdefault("fut_entry", {})
    state["name"] = name

    if cached:
        state["ticker"] = cached
        state["symbol"] = cached.split(".")[0]
        return await _ask_direction(update, context, is_callback=False)

    await update.message.reply_text("종목 검색 중...")
    try:
        candidates = await asyncio.to_thread(search_stocks, name)
    except Exception:
        logger.exception("선물 진입: 종목 검색 실패: %s", name)
        candidates = []

    if not candidates:
        state["ticker"] = ""
        state["symbol"] = ""
        await update.message.reply_text(
            "기초자산 종목코드를 찾지 못했습니다. 코드 없이 진행합니다.\n"
            "(현재가 자동 조회가 안 될 수 있습니다)"
        )
        return await _ask_direction(update, context, is_callback=False)

    exact = [c for c in candidates if c.name == name]
    if len(exact) == 1:
        suffix = ".KQ" if exact[0].market == "KOSDAQ" else ".KS"
        state["ticker"] = exact[0].code + suffix
        state["symbol"] = exact[0].code
        return await _ask_direction(update, context, is_callback=False)

    await update.message.reply_text(
        f'"{name}" 검색 결과입니다.\n기초자산을 선택해주세요:',
        reply_markup=stock_search_keyboard(candidates),
    )
    return PICK_STOCK


async def _pick_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data.removeprefix(BUY_STOCK_PREFIX)
    parts = data.split("|", 1)
    selected_name = parts[0]
    selected_ticker = parts[1] if len(parts) > 1 else ""

    state = context.user_data.setdefault("fut_entry", {})
    if selected_name:
        state["name"] = _strip_name(selected_name)
    state["ticker"] = selected_ticker
    state["symbol"] = selected_ticker.split(".")[0] if selected_ticker else ""
    return await _ask_direction(query, context, is_callback=True)


async def _ask_direction(update, context, *, is_callback: bool) -> int:
    msg = "포지션 방향을 선택해주세요."
    if is_callback:
        await update.edit_message_text(msg, reply_markup=futures_direction_keyboard())
    else:
        await update.message.reply_text(msg, reply_markup=futures_direction_keyboard())
    return DIRECTION


async def _pick_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    state = context.user_data.setdefault("fut_entry", {})
    state["direction"] = "long" if query.data == FUTURES_LONG else "short"

    months = upcoming_months(count=6)
    context.user_data["fut_months"] = {m.contract_month: m for m in months}
    await query.edit_message_text(
        "결제월을 선택해주세요:",
        reply_markup=futures_month_keyboard(months),
    )
    return MONTH


async def _pick_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    cm = query.data.removeprefix(FUTURES_MONTH_PREFIX)
    months_map = context.user_data.get("fut_months", {})
    fm = months_map.get(cm)
    if fm is None:
        await query.edit_message_text("결제월을 다시 선택해주세요. 다시 시작해주세요.")
        return ConversationHandler.END

    state = context.user_data.setdefault("fut_entry", {})
    state["contract_month"] = fm.contract_month
    state["expiry_date"] = fm.expiry_iso

    direction_kr = "롱" if state.get("direction") == "long" else "숏"
    name = state.get("name", "")
    await query.edit_message_text(
        f"[{name} {direction_kr} {fm.label()}]\n\n"
        "계약수 / 진입가 / 위탁증거금\n"
        "을 줄바꿈으로 입력해주세요.\n"
        f"(승수 {DEFAULT_MULTIPLIER}주/계약, 사유는 다음 단계에서 선택/입력)"
    )
    return BODY


async def _receive_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("fut_entry") or {}
    if not state.get("contract_month"):
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        return ConversationHandler.END

    try:
        body = parse_futures_entry(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}")
        return BODY

    state["contracts"] = body.contracts
    state["price"] = body.price
    state["margin"] = body.margin

    # 사유가 본문에 함께 들어왔으면 섹터만 더 받고 저장
    if body.reason:
        state["reason"] = body.reason

    # 같은 종목의 기존 포지션이 있으면 그 섹터를 기본값으로 제안 — 있으면 묻지 않고 그대로 사용
    suggested_sector = _suggest_sector(state.get("symbol", ""), state.get("name", ""))
    if suggested_sector:
        state["sector"] = suggested_sector
        return await _proceed_to_reason(update, context, state)

    state["suggested_sector"] = suggested_sector
    prompt = "섹터를 입력해주세요. (예: 반도체, 로봇, IT)"
    await update.message.reply_text(prompt)
    return SECTOR


def _suggest_sector(symbol: str, name: str) -> str:
    """기존 선물 포지션 또는 현물 보유 종목의 섹터를 기본값으로 제안."""
    from storage.json_store import load_holdings
    for p in load_futures_positions():
        if symbol and p.get("symbol") == symbol and p.get("sector"):
            return p["sector"]
        if not symbol and p.get("name", "").lower() == name.lower() and p.get("sector"):
            return p["sector"]
    for h in load_holdings():
        if h.get("name", "").lower() == name.lower() and h.get("sector"):
            return h["sector"]
    return ""


async def _proceed_to_reason(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict,
) -> int:
    """섹터가 정해진 뒤 사유 단계.

    분기:
      - 본문에 사유가 함께 들어왔으면 → 바로 저장
      - 같은 종목·방향·결제월의 기존 포지션이 있고 기존 사유가 있으면
        → 유지/이어쓰기/새로쓰기 키보드 (EXISTING_THESIS)
      - 그 외 → 최근 사유 키보드 + 자유 입력 (REASON)
    """
    if state.get("reason"):
        return await _do_save(update, context, is_callback=False)

    existing = _pick_existing_position(
        state.get("symbol", ""), state.get("name", ""),
        state.get("direction", ""), state.get("contract_month", ""),
    )
    existing_thesis = (existing or {}).get("thesis", "") if existing else ""
    if existing_thesis:
        state["existing_thesis"] = existing_thesis
        name = state.get("name", "")
        direction_kr = "롱" if state.get("direction") == "long" else "숏"
        contracts = state.get("contracts", 0)
        price = state.get("price", 0)
        msg = (
            f"[{name} {direction_kr} {contracts}계약 / {int(price):,}원]\n\n"
            f"기존 진입사유:\n{existing_thesis}\n\n"
            "유지하거나 이어쓰기/새로쓰기 중 선택해주세요."
        )
        await update.message.reply_text(
            msg, reply_markup=futures_existing_thesis_keyboard(),
        )
        return EXISTING_THESIS

    reasons = get_recent_futures_reasons("open")
    context.user_data["recent_reasons"] = reasons

    name = state.get("name", "")
    direction_kr = "롱" if state.get("direction") == "long" else "숏"
    contracts = state.get("contracts", 0)
    price = state.get("price", 0)
    msg = (
        f"[{name} {direction_kr} {contracts}계약 / {int(price):,}원]\n\n"
        "진입 사유를 입력해주세요."
    )
    if reasons:
        msg += "\n\n최근 사유를 선택하거나 직접 입력하세요."
    await update.message.reply_text(
        msg,
        reply_markup=reason_select_keyboard(reasons) if reasons else None,
    )
    return REASON


async def _existing_thesis_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """기존 사유 유지/이어쓰기/대체 분기."""
    query = update.callback_query
    await query.answer()
    state = context.user_data.get("fut_entry") or {}
    existing_thesis = state.get("existing_thesis", "")
    if not state.get("contract_month"):
        await query.edit_message_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END

    if query.data == KEEP_EXISTING:
        state["reason"] = existing_thesis
        state.pop("existing_thesis", None)
        return await _do_save(query, context, is_callback=True)

    if query.data == APPEND_THESIS:
        state["append_base"] = existing_thesis
        preview = existing_thesis if existing_thesis else "(기존 사유 없음)"
        await query.edit_message_text(
            f"기존 진입사유:\n{preview}\n\n이어붙일 내용을 입력해주세요."
        )
        return APPEND_INPUT

    # EDIT_THESIS — 대체: 최근 사유 키보드 + 자유 입력으로
    state.pop("existing_thesis", None)
    reasons = get_recent_futures_reasons("open")
    context.user_data["recent_reasons"] = reasons
    msg = "새로운 진입 사유를 입력해주세요."
    if reasons:
        msg += "\n\n최근 사유를 선택하거나 직접 입력하세요."
    await query.edit_message_text(
        msg,
        reply_markup=reason_select_keyboard(reasons) if reasons else None,
    )
    return REASON


async def _append_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """기존 사유에 이어쓰기 → 결합 후 저장."""
    state = context.user_data.get("fut_entry") or {}
    base = state.pop("append_base", "")
    if not state.get("contract_month"):
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END
    extra = update.message.text.strip()
    state["reason"] = f"{base}\n{extra}" if base else extra
    return await _do_save(update, context, is_callback=False)


async def _receive_sector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("fut_entry") or {}
    if not state.get("contract_month"):
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END

    raw = update.message.text.strip()
    if raw == "." and state.get("suggested_sector"):
        state["sector"] = state["suggested_sector"]
    else:
        state["sector"] = raw

    return await _proceed_to_reason(update, context, state)


async def _reason_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    state = context.user_data.get("fut_entry") or {}
    if not state.get("contract_month") or idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END
    state["reason"] = reasons[idx]
    return await _do_save(query, context, is_callback=True)


async def _reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = context.user_data.get("fut_entry") or {}
    if not state.get("contract_month"):
        await update.message.reply_text("세션이 만료되었습니다. 다시 시작해주세요.")
        _cleanup(context)
        return ConversationHandler.END
    state["reason"] = update.message.text.strip()
    return await _do_save(update, context, is_callback=False)


async def _do_save(update, context, *, is_callback: bool) -> int:
    state = context.user_data.get("fut_entry") or {}
    name = state.get("name", "")
    symbol = state.get("symbol", "")
    direction = state.get("direction", "long")
    contract_month = state.get("contract_month", "")
    expiry_date = state.get("expiry_date", "")
    contracts = int(state.get("contracts", 0))
    price = float(state.get("price", 0.0))
    margin = float(state.get("margin", 0.0))
    reason = state.get("reason", "")
    sector = state.get("sector", "")

    # 기존 포지션 추가 진입 vs 신규 진입
    positions = load_futures_positions()
    existing = _pick_existing_position(symbol, name, direction, contract_month)

    tx = FuturesTransaction(
        type="open",
        name=name,
        symbol=symbol,
        contract_code="",
        contract_month=contract_month,
        expiry_date=expiry_date,
        direction=direction,
        contracts=contracts,
        price=price,
        multiplier=DEFAULT_MULTIPLIER,
        margin=margin,
        thesis=reason,
    )

    if existing is not None:
        pos = FuturesPosition.from_dict(existing)
        pos.add_entry(price=price, contracts=contracts, margin=margin, transaction_id=tx.id)
        # 사유/섹터는 가장 최근 입력으로 갱신
        if reason:
            pos.thesis = reason
        if sector:
            pos.sector = sector
        for i, p in enumerate(positions):
            if p["id"] == pos.id:
                positions[i] = pos.to_dict()
                break
        tx.position_id = pos.id
    else:
        pos = FuturesPosition(
            name=name,
            symbol=symbol,
            contract_code="",
            contract_month=contract_month,
            expiry_date=expiry_date,
            direction=direction,
            contracts=contracts,
            avg_entry_price=price,
            initial_margin=margin,
            sector=sector,
            thesis=reason,
            transaction_ids=[tx.id],
        )
        tx.position_id = pos.id
        positions.append(pos.to_dict())

    save_futures_positions(positions)

    txs = load_futures_transactions()
    txs.append(tx.to_dict())
    save_futures_transactions(txs)

    # ticker_map 캐시 (기초자산)
    ticker = state.get("ticker", "")
    if ticker:
        tmap = load_ticker_map()
        tmap[name] = ticker
        save_ticker_map(tmap)

    direction_kr = "롱" if direction == "long" else "숏"
    cm_label = f"{contract_month[2:4]}년 {contract_month[4:6]}월물"
    msg = (
        f"선물 진입 완료!\n"
        f"{name} {direction_kr} {contracts}계약 ({cm_label})\n"
        f"진입가: {int(price):,}원  |  증거금: {int(margin):,}원\n"
        f'섹터: {sector or "(미입력)"}\n'
        f'사유: "{reason}"'
    )
    if is_callback:
        await update.edit_message_text(msg)
    else:
        await update.message.reply_text(msg)

    _cleanup(context)
    return ConversationHandler.END


def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("fut_entry", "fut_months", "recent_reasons", "suggested_sector",
              "append_base"):
        context.user_data.pop(k, None)


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|수정|회고|자산그래프|선물진입|선물청산|선물롤오버|선물회고)$"
    ) | filters.COMMAND


async def _abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cleanup(context)
    await update.message.reply_text("선물 진입이 취소되었습니다.")
    return ConversationHandler.END


def futures_entry_conversation() -> ConversationHandler:
    other_cmd = _other_command_filter()
    return ConversationHandler(
        entry_points=[
            CommandHandler("futures_buy", _start),
            MessageHandler(filters.Regex(r"^선물진입$"), _start),
        ],
        states={
            NAME: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_name),
            ],
            PICK_STOCK: [
                CallbackQueryHandler(_pick_stock, pattern=f"^{BUY_STOCK_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _abort),
            ],
            DIRECTION: [
                CallbackQueryHandler(_pick_direction, pattern=f"^{FUTURES_DIR_PREFIX}"),
                MessageHandler(other_cmd, _abort),
            ],
            MONTH: [
                CallbackQueryHandler(_pick_month, pattern=f"^{FUTURES_MONTH_PREFIX}"),
                MessageHandler(other_cmd, _abort),
            ],
            BODY: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_body),
            ],
            SECTOR: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_sector),
            ],
            REASON: [
                CallbackQueryHandler(_reason_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _reason_text),
            ],
            EXISTING_THESIS: [
                CallbackQueryHandler(
                    _existing_thesis_confirm,
                    pattern=f"^({KEEP_EXISTING}|{APPEND_THESIS}|{EDIT_THESIS})$",
                ),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _abort),
            ],
            APPEND_INPUT: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _append_input),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _abort),
            CommandHandler("cancel", _abort),
        ],
        name="futures_entry",
        allow_reentry=True,
    )
