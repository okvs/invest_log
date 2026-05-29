"""매수(buy) ConversationHandler.

플로우:
  신규: 종목명/수량/매수가 → 티커검색 → 섹터 입력 → 매수근거 입력 → 저장
  추가매수: 종목명/수량/매수가 → 티커검색 → 기존 섹터+근거 확인 → 유지 or 수정 → 저장
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.formatters import format_buy_result
from bot.keyboards import (
    APPEND_THESIS,
    BUY_STOCK_PREFIX,
    EDIT_SECTOR,
    EDIT_THESIS,
    KEEP_EXISTING,
    MARGIN_PREFIX,
    REASON_PICK_PREFIX,
    existing_info_keyboard,
    margin_ratio_keyboard,
    reason_select_keyboard,
    stock_search_keyboard,
)
from models.portfolio import Holding
from models.transaction import Transaction
from parsers.input_parser import (
    lookup_ticker,
    parse_buy_input,
    resolve_name,
    search_stocks,
)
from storage.json_store import (
    get_recent_reasons,
    load_account,
    load_holdings,
    load_nickname_map,
    load_ticker_map,
    load_transactions,
    save_account,
    save_holdings,
    save_ticker_map,
    save_transactions,
)

logger = logging.getLogger(__name__)

# ConversationHandler 상태
INPUT = 0
PICK_STOCK = 1
EXISTING_CONFIRM = 2
SECTOR_INPUT = 3
THESIS_INPUT = 4
MARGIN_SELECT = 5
APPEND_INPUT = 6  # 기존 매수사유에 이어쓸 텍스트 입력


# ---------------------------------------------------------------------------
# 기존 보유 종목 조회 헬퍼
# ---------------------------------------------------------------------------

def _find_existing_holding(ticker: str, name: str) -> dict | None:
    """기존 보유 종목 dict를 찾아 반환. ticker 우선, 이름 fallback."""
    holdings_data = load_holdings()
    if ticker:
        for h_dict in holdings_data:
            if h_dict.get("ticker", "") == ticker:
                return h_dict
    for h_dict in holdings_data:
        if h_dict["name"].lower() == name.lower():
            return h_dict
    return None


def _strip_name(name: str) -> str:
    """종목명 공백 제거."""
    return name.replace(" ", "")


# ---------------------------------------------------------------------------
# 대화 시작
# ---------------------------------------------------------------------------

async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매수 대화 시작."""
    await update.message.reply_text(
        "종목명 / 수량 / 매수가\n"
        "를 줄바꿈으로 입력해주세요."
    )
    return INPUT


# ---------------------------------------------------------------------------
# 입력 파싱 + 티커 검색
# ---------------------------------------------------------------------------

async def _receive_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """사용자 입력 파싱 → 티커 검색 → 기존/신규 분기."""
    text = update.message.text

    try:
        buy_input = parse_buy_input(text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}\n\n다시 입력해주세요.")
        return INPUT

    try:
        # 닉네임 → 실제 종목명 변환
        nmap = load_nickname_map()
        buy_input.name = resolve_name(buy_input.name, nickname_map=nmap)

        # ticker_map 캐시 확인
        tmap = load_ticker_map()
        cached = tmap.get(buy_input.name, "")
        if not cached:
            for k, v in tmap.items():
                if k.lower() == buy_input.name.lower():
                    cached = v
                    break

        if cached:
            buy_input.ticker = cached
            return await _after_ticker_resolved(update, context, buy_input)

        # 네이버 종목 검색
        await update.message.reply_text("종목 검색 중...")
        try:
            candidates = await asyncio.to_thread(search_stocks, buy_input.name)
        except Exception:
            logger.exception("종목 검색 실패: %s", buy_input.name)
            candidates = []

        if not candidates:
            buy_input.ticker = ""
            await update.message.reply_text(
                "종목코드를 찾지 못했습니다. 종목코드 없이 저장합니다.\n"
                "(현황에서 현재가 조회가 안 될 수 있습니다)"
            )
            return await _after_ticker_resolved(update, context, buy_input)

        exact = [c for c in candidates if c.name == buy_input.name]
        if len(exact) == 1:
            suffix = ".KQ" if exact[0].market == "KOSDAQ" else ".KS"
            buy_input.ticker = exact[0].code + suffix
            return await _after_ticker_resolved(update, context, buy_input)

        # 후보 여러 개 → 선택 요청
        context.user_data["buy_input"] = buy_input
        keyboard = stock_search_keyboard(candidates)
        await update.message.reply_text(
            f'"{buy_input.name}" 검색 결과입니다.\n종목을 선택해주세요:',
            reply_markup=keyboard,
        )
        return PICK_STOCK

    except Exception:
        logger.exception("매수 처리 중 오류 발생")
        await update.message.reply_text(
            "매수 처리 중 오류가 발생했습니다. 다시 시도해주세요."
        )
        return ConversationHandler.END


async def _pick_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """종목 선택 버튼 처리."""
    query = update.callback_query
    await query.answer()

    data = query.data.removeprefix(BUY_STOCK_PREFIX)
    parts = data.split("|", 1)
    selected_name = parts[0] if parts[0] else ""
    selected_ticker = parts[1] if len(parts) > 1 else ""

    buy_input = context.user_data.pop("buy_input", None)
    if buy_input is None:
        await query.edit_message_text("세션이 만료되었습니다. 다시 매수를 시작해주세요.")
        return ConversationHandler.END

    if selected_name:
        buy_input.name = _strip_name(selected_name)
    buy_input.ticker = selected_ticker

    return await _after_ticker_resolved_cb(query, context, buy_input)


# ---------------------------------------------------------------------------
# 티커 확정 후 → 기존/신규 분기
# ---------------------------------------------------------------------------

async def _after_ticker_resolved(update, context, buy_input) -> int:
    """티커 확정 후: 기존 보유면 정보 확인, 신규면 섹터 질문."""
    existing = _find_existing_holding(buy_input.ticker, buy_input.name)
    if existing:
        return await _ask_existing_confirm(
            update, context, buy_input, existing, is_callback=False
        )
    # 신규 → 섹터 입력
    context.user_data["buy_input"] = buy_input
    await update.message.reply_text("섹터를 입력해주세요. (예: 반도체, IT, 바이오)")
    return SECTOR_INPUT


async def _after_ticker_resolved_cb(query, context, buy_input) -> int:
    """콜백용 _after_ticker_resolved."""
    existing = _find_existing_holding(buy_input.ticker, buy_input.name)
    if existing:
        return await _ask_existing_confirm(
            query, context, buy_input, existing, is_callback=True
        )
    context.user_data["buy_input"] = buy_input
    await query.edit_message_text("섹터를 입력해주세요. (예: 반도체, IT, 바이오)")
    return SECTOR_INPUT


async def _ask_existing_confirm(update, context, buy_input, existing, *, is_callback):
    """기존 보유 종목의 섹터+근거를 보여주고 유지/수정 선택."""
    sector = existing.get("sector", "") or "(없음)"
    thesis = existing.get("buy_thesis", "") or "(없음)"

    # 기존 섹터/근거를 buy_input에 미리 채워둠
    buy_input.sector = existing.get("sector", "")
    buy_input.thesis = existing.get("buy_thesis", "")
    context.user_data["buy_input"] = buy_input

    msg = (
        f"기존 보유 종목입니다.\n\n"
        f"섹터: {sector}\n"
        f"매수사유: {thesis}\n\n"
        f"그대로 유지하거나 수정할 항목을 선택해주세요."
    )
    if is_callback:
        await update.edit_message_text(msg, reply_markup=existing_info_keyboard())
    else:
        await update.message.reply_text(msg, reply_markup=existing_info_keyboard())
    return EXISTING_CONFIRM


# ---------------------------------------------------------------------------
# 기존 보유 종목: 유지/수정 선택
# ---------------------------------------------------------------------------

async def _existing_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """기존 섹터/근거 유지·이어쓰기·대체 분기."""
    query = update.callback_query
    await query.answer()

    buy_input = context.user_data.get("buy_input")
    if buy_input is None:
        await query.edit_message_text("세션이 만료되었습니다. 다시 매수를 시작해주세요.")
        return ConversationHandler.END

    if query.data == KEEP_EXISTING:
        context.user_data.pop("buy_input", None)
        return await _do_save(update, context, buy_input, is_callback=True)
    elif query.data == EDIT_SECTOR:
        await query.edit_message_text("새로운 섹터를 입력해주세요.")
        context.user_data["_after_sector"] = "existing_confirm"
        return SECTOR_INPUT
    elif query.data == APPEND_THESIS:
        # 기존 매수사유에 이어붙일 텍스트 입력
        existing_thesis = buy_input.thesis or ""
        context.user_data["append_base"] = existing_thesis
        preview = existing_thesis if existing_thesis else "(기존 사유 없음)"
        await query.edit_message_text(
            f"기존 매수사유:\n{preview}\n\n이어붙일 내용을 입력해주세요."
        )
        return APPEND_INPUT
    else:  # EDIT_THESIS — 대체
        return await _ask_thesis(
            update, context, buy_input, is_callback=True,
            prompt="새로운 매수 근거를 입력해주세요.",
        )


# ---------------------------------------------------------------------------
# 섹터 입력
# ---------------------------------------------------------------------------

async def _sector_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """섹터 입력 처리."""
    buy_input = context.user_data.get("buy_input")
    if buy_input is None:
        await update.message.reply_text("세션이 만료되었습니다. 다시 매수를 시작해주세요.")
        return ConversationHandler.END

    buy_input.sector = update.message.text.strip()

    # 기존 종목에서 섹터만 수정한 경우 → 바로 저장
    after = context.user_data.pop("_after_sector", None)
    if after == "existing_confirm":
        context.user_data.pop("buy_input", None)
        return await _do_save(update, context, buy_input, is_callback=False)

    # 신규 종목 → 매수 근거 입력 (최근 사유 버튼 + 자유 입력)
    return await _ask_thesis(update, context, buy_input, is_callback=False)


# ---------------------------------------------------------------------------
# 매수 근거 입력
# ---------------------------------------------------------------------------

async def _ask_thesis(
    update,
    context: ContextTypes.DEFAULT_TYPE,
    buy_input,
    *,
    is_callback: bool,
    prompt: str = "매수 근거를 입력해주세요.",
) -> int:
    """매수 근거를 입력받기 직전 프롬프트.
    해당 종목의 최근 사유가 있으면 클릭 가능한 버튼을 함께 보여주고,
    실제 사유 텍스트는 context.user_data['recent_reasons']에 저장한다.
    """
    context.user_data["buy_input"] = buy_input
    reasons = get_recent_reasons("buy")
    context.user_data["recent_reasons"] = reasons

    keyboard = reason_select_keyboard(reasons) if reasons else None
    msg = prompt
    if reasons:
        msg += "\n\n최근 사유를 선택하거나 직접 입력하세요."

    if is_callback:
        query = update.callback_query if hasattr(update, "callback_query") and update.callback_query else update
        await query.edit_message_text(msg, reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, reply_markup=keyboard)
    return THESIS_INPUT


async def _thesis_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """최근 사유 버튼 클릭 → 해당 사유로 저장."""
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.removeprefix(REASON_PICK_PREFIX))
    except ValueError:
        idx = -1
    reasons = context.user_data.get("recent_reasons", [])
    buy_input = context.user_data.pop("buy_input", None)
    context.user_data.pop("recent_reasons", None)

    if buy_input is None or idx < 0 or idx >= len(reasons):
        await query.edit_message_text("세션이 만료되었습니다. 다시 매수를 시작해주세요.")
        return ConversationHandler.END

    buy_input.thesis = reasons[idx]
    return await _do_save(update, context, buy_input, is_callback=True)


async def _thesis_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매수 근거 직접 입력 처리."""
    buy_input = context.user_data.pop("buy_input", None)
    context.user_data.pop("recent_reasons", None)
    if buy_input is None:
        await update.message.reply_text("세션이 만료되었습니다. 다시 매수를 시작해주세요.")
        return ConversationHandler.END

    buy_input.thesis = update.message.text.strip()
    return await _do_save(update, context, buy_input, is_callback=False)


async def _append_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """기존 매수사유에 이어쓰기 → 결합 후 저장."""
    buy_input = context.user_data.pop("buy_input", None)
    base = context.user_data.pop("append_base", "")
    if buy_input is None:
        await update.message.reply_text("세션이 만료되었습니다. 다시 매수를 시작해주세요.")
        return ConversationHandler.END

    extra = update.message.text.strip()
    buy_input.thesis = f"{base}\n{extra}" if base else extra
    return await _do_save(update, context, buy_input, is_callback=False)


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------

async def _do_save(update, context, buy_input, *, is_callback=False) -> int:
    """증거금비율 선택 질문. 계좌 미설정 시 바로 저장."""
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
    return MARGIN_SELECT


async def _margin_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """증거금비율 선택 후 매수 저장."""
    query = update.callback_query
    await query.answer()

    margin_ratio = int(query.data.removeprefix(MARGIN_PREFIX))
    buy_input = context.user_data.pop("buy_input", None)
    if buy_input is None:
        await query.edit_message_text("세션이 만료되었습니다. 다시 매수를 시작해주세요.")
        return ConversationHandler.END

    result_text = _process_and_save(buy_input, margin_ratio=margin_ratio)
    await query.edit_message_text(result_text)
    return ConversationHandler.END


def _process_and_save(buy_input, *, margin_ratio: int = 100) -> str:
    """매수 데이터를 처리하고 저장. 결과 텍스트 반환."""
    # 종목명 공백 제거
    buy_input.name = _strip_name(buy_input.name)

    tx = Transaction(
        type="buy",
        name=buy_input.name,
        sector=buy_input.sector,
        price=buy_input.price,
        quantity=buy_input.quantity,
        total_amount=buy_input.price * buy_input.quantity,
        thesis=buy_input.thesis,
        research_notes=buy_input.research_notes,
        margin_ratio=margin_ratio,
    )

    # 기존 종목 확인 (ticker 우선, 이름 fallback)
    holdings_data = load_holdings()
    existing: Holding | None = None
    existing_idx: int | None = None

    if buy_input.ticker:
        for idx, h_dict in enumerate(holdings_data):
            if h_dict.get("ticker", "") == buy_input.ticker:
                existing = Holding.from_dict(h_dict)
                existing_idx = idx
                break

    if existing is None:
        for idx, h_dict in enumerate(holdings_data):
            if h_dict["name"].lower() == buy_input.name.lower():
                existing = Holding.from_dict(h_dict)
                existing_idx = idx
                break

    if existing is not None:
        existing.add_buy(buy_input.price, buy_input.quantity, tx.id, margin_ratio)
        if buy_input.ticker and not existing.ticker:
            existing.ticker = buy_input.ticker
        if buy_input.sector:
            existing.sector = buy_input.sector
        if buy_input.thesis:
            existing.buy_thesis = buy_input.thesis
        # 종목명 공백 통일
        existing.name = _strip_name(existing.name)
        holdings_data[existing_idx] = existing.to_dict()
    else:
        buy_amount = buy_input.price * buy_input.quantity
        credit_loan = buy_amount * (1 - margin_ratio / 100) if margin_ratio < 100 else 0.0
        holding = Holding(
            name=buy_input.name,
            ticker=buy_input.ticker,
            sector=buy_input.sector,
            buy_date=datetime.now().strftime("%Y-%m-%d"),
            avg_price=buy_input.price,
            quantity=buy_input.quantity,
            total_invested=buy_amount,
            credit_loan=credit_loan,
            buy_thesis=buy_input.thesis,
            research_notes=buy_input.research_notes,
            transaction_ids=[tx.id],
        )
        holdings_data.append(holding.to_dict())

    save_holdings(holdings_data)

    # ticker_map 캐시 업데이트
    if buy_input.ticker:
        tmap = load_ticker_map()
        tmap[buy_input.name] = buy_input.ticker
        save_ticker_map(tmap)

    # Transaction 저장
    transactions = load_transactions()
    transactions.append(tx.to_dict())
    save_transactions(transactions)

    # 예수금 차감
    account = load_account()
    if account.get("initial_capital"):
        cash = account.get("cash", account["initial_capital"])
        cash_deduct = tx.total_amount * (margin_ratio / 100)
        account["cash"] = cash - cash_deduct
        save_account(account)

    ticker_display = f" [{buy_input.ticker}]" if buy_input.ticker else ""
    return format_buy_result(
        name=buy_input.name,
        ticker=ticker_display,
        sector=buy_input.sector,
        quantity=buy_input.quantity,
        price=buy_input.price,
        thesis=buy_input.thesis,
    )


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------

def _other_command_filter() -> filters.BaseFilter:
    """다른 명령어 필터 — 매수 대화 중 다른 명령 입력 시 대화 종료용."""
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|help|수정|회고|자산그래프|백테스트|10억|입금|출금|입출금목록|선물진입|선물청산|선물롤오버|선물회고)$"
    ) | filters.COMMAND


def buy_conversation() -> ConversationHandler:
    """매수 ConversationHandler를 생성하여 반환."""
    other_cmd = _other_command_filter()

    return ConversationHandler(
        entry_points=[
            CommandHandler("buy", _start),
            MessageHandler(filters.Regex(r"^매수$"), _start),
        ],
        states={
            INPUT: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_input),
            ],
            PICK_STOCK: [
                CallbackQueryHandler(_pick_stock, pattern=f"^{BUY_STOCK_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _abort),
            ],
            EXISTING_CONFIRM: [
                CallbackQueryHandler(
                    _existing_confirm,
                    pattern=f"^({KEEP_EXISTING}|{EDIT_SECTOR}|{EDIT_THESIS}|{APPEND_THESIS})$",
                ),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _abort),
            ],
            SECTOR_INPUT: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _sector_input),
            ],
            THESIS_INPUT: [
                CallbackQueryHandler(_thesis_pick, pattern=f"^{REASON_PICK_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _thesis_input),
            ],
            APPEND_INPUT: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _append_input),
            ],
            MARGIN_SELECT: [
                CallbackQueryHandler(_margin_selected, pattern=f"^{MARGIN_PREFIX}"),
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _abort),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _abort),
            CommandHandler("cancel", _abort),
        ],
        name="buy",
        allow_reentry=True,
    )


async def _abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매수 대화를 즉시 종료하고 idle로 복귀."""
    for key in ("buy_input", "_after_sector", "recent_reasons", "append_base"):
        context.user_data.pop(key, None)
    await update.message.reply_text("매수 기록이 취소되었습니다.")
    return ConversationHandler.END
