"""매수(buy) ConversationHandler.

플로우:
  매수 → 입력 → (종목 검색 결과 1개 초과 시) 종목 선택 → 저장
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
from bot.keyboards import BUY_STOCK_PREFIX, stock_search_keyboard
from models.portfolio import Holding
from models.transaction import Transaction
from parsers.input_parser import (
    lookup_ticker,
    parse_buy_input,
    resolve_name,
    search_stocks,
)
from storage.json_store import (
    load_holdings,
    load_nickname_map,
    load_ticker_map,
    load_transactions,
    save_holdings,
    save_ticker_map,
    save_transactions,
)

logger = logging.getLogger(__name__)

# ConversationHandler 상태
INPUT = 0
PICK_STOCK = 1


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """매수 대화 시작 — 입력 안내 메시지."""
    await update.message.reply_text(
        "매수 정보를 입력해주세요:\n\n"
        "종목명\n"
        "섹터\n"
        "수량 (예: 10주)\n"
        "매수가 (예: 72000원)\n"
        "매수 근거\n"
        "참고 자료 (선택)\n\n"
        "예시:\n"
        "삼성전자\n"
        "반도체\n"
        "10주\n"
        "72000원\n"
        "AI 수요 증가 전망"
    )
    return INPUT


async def _receive_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """사용자 입력을 파싱. 종목 검색 후 후보가 여러 개면 선택 버튼 표시."""
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

        # ticker_map 캐시에 이미 있으면 바로 저장
        tmap = load_ticker_map()
        cached = tmap.get(buy_input.name, "")
        if not cached:
            # 대소문자 무시 검색
            for k, v in tmap.items():
                if k.lower() == buy_input.name.lower():
                    cached = v
                    break

        if cached:
            buy_input.ticker = cached
            return await _save_buy(update, context, buy_input)

        # 종목 검색 (별도 subprocess에서 Playwright 실행)
        await update.message.reply_text("종목 검색 중...")
        try:
            candidates = await asyncio.to_thread(search_stocks, buy_input.name)
        except Exception:
            logger.exception("종목 검색 실패: %s", buy_input.name)
            candidates = []

        if not candidates:
            # 검색 결과 없음 → 종목코드 없이 저장
            buy_input.ticker = ""
            await update.message.reply_text(
                "종목코드를 찾지 못했습니다. 종목코드 없이 저장합니다.\n"
                "(현황에서 현재가 조회가 안 될 수 있습니다)"
            )
            return await _save_buy(update, context, buy_input)

        # 정확히 1개만 매칭 → 바로 저장
        exact = [c for c in candidates if c.name == buy_input.name]
        if len(exact) == 1:
            suffix = ".KQ" if exact[0].market == "KOSDAQ" else ".KS"
            buy_input.ticker = exact[0].code + suffix
            return await _save_buy(update, context, buy_input)

        # 후보 표시 → 사용자에게 선택 요청
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
    """사용자가 종목 선택 버튼을 눌렀을 때 처리."""
    query = update.callback_query
    await query.answer()

    data = query.data.removeprefix(BUY_STOCK_PREFIX)
    # data 형식: "종목명|코드.KS" 또는 "|" (종목코드 없이 진행)
    parts = data.split("|", 1)
    selected_name = parts[0] if parts[0] else ""
    selected_ticker = parts[1] if len(parts) > 1 else ""

    buy_input = context.user_data.pop("buy_input", None)
    if buy_input is None:
        await query.edit_message_text("세션이 만료되었습니다. 다시 매수를 시작해주세요.")
        return ConversationHandler.END

    if selected_name:
        buy_input.name = selected_name
    buy_input.ticker = selected_ticker

    return await _save_buy_from_callback(query, context, buy_input)


async def _save_buy(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    buy_input,
) -> int:
    """매수 정보를 저장하고 결과 메시지 전송 (일반 메시지용)."""
    result_text = _process_and_save(buy_input)
    await update.message.reply_text(result_text)
    return ConversationHandler.END


async def _save_buy_from_callback(query, context, buy_input) -> int:
    """매수 정보를 저장하고 결과 메시지 전송 (콜백 쿼리용)."""
    result_text = _process_and_save(buy_input)
    await query.edit_message_text(result_text)
    return ConversationHandler.END


def _process_and_save(buy_input) -> str:
    """매수 데이터를 처리하고 저장. 결과 텍스트 반환."""
    tx = Transaction(
        type="buy",
        name=buy_input.name,
        sector=buy_input.sector,
        price=buy_input.price,
        quantity=buy_input.quantity,
        total_amount=buy_input.price * buy_input.quantity,
        thesis=buy_input.thesis,
        research_notes=buy_input.research_notes,
    )

    # 기존 종목 확인
    holdings_data = load_holdings()
    existing: Holding | None = None
    existing_idx: int | None = None

    for idx, h_dict in enumerate(holdings_data):
        if h_dict["name"].lower() == buy_input.name.lower():
            existing = Holding.from_dict(h_dict)
            existing_idx = idx
            break

    if existing is not None:
        existing.add_buy(buy_input.price, buy_input.quantity, tx.id)
        # 기존 보유 종목에 ticker가 없었으면 업데이트
        if buy_input.ticker and not existing.ticker:
            existing.ticker = buy_input.ticker
        holdings_data[existing_idx] = existing.to_dict()
    else:
        holding = Holding(
            name=buy_input.name,
            ticker=buy_input.ticker,
            sector=buy_input.sector,
            buy_date=datetime.now().strftime("%Y-%m-%d"),
            avg_price=buy_input.price,
            quantity=buy_input.quantity,
            total_invested=buy_input.price * buy_input.quantity,
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

    ticker_display = f" [{buy_input.ticker}]" if buy_input.ticker else ""
    return format_buy_result(
        name=buy_input.name,
        ticker=ticker_display,
        sector=buy_input.sector,
        quantity=buy_input.quantity,
        price=buy_input.price,
        thesis=buy_input.thesis,
    )


def buy_conversation() -> ConversationHandler:
    """매수 ConversationHandler를 생성하여 반환."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("buy", _start),
            MessageHandler(filters.Regex(r"^매수$"), _start),
        ],
        states={
            INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_input)],
            PICK_STOCK: [
                CallbackQueryHandler(_pick_stock, pattern=f"^{BUY_STOCK_PREFIX}"),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel_fallback)],
    )


async def _cancel_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel 명령어로 대화 중단."""
    context.user_data.pop("buy_input", None)
    await update.message.reply_text("매수 기록이 취소되었습니다.")
    return ConversationHandler.END
