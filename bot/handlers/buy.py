"""매수(buy) ConversationHandler."""
from __future__ import annotations

from datetime import datetime

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.formatters import format_buy_result
from models.portfolio import Holding
from models.transaction import Transaction
from parsers.input_parser import lookup_ticker, parse_buy_input, resolve_name
from storage.json_store import (
    load_holdings,
    load_nickname_map,
    load_ticker_map,
    load_transactions,
    save_holdings,
    save_ticker_map,
    save_transactions,
)

# ConversationHandler 상태
INPUT = 0


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
    """사용자 입력을 파싱하고 즉시 저장."""
    text = update.message.text

    try:
        buy_input = parse_buy_input(text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}\n\n다시 입력해주세요.")
        return INPUT

    # 닉네임 → 실제 종목명 변환
    nmap = load_nickname_map()
    buy_input.name = resolve_name(buy_input.name, nickname_map=nmap)

    # 종목코드 자동 조회
    tmap = load_ticker_map()
    buy_input.ticker = lookup_ticker(buy_input.name, ticker_map=tmap)

    # Transaction 생성
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
        # 추가 매수 — 평균단가 재계산
        existing.add_buy(buy_input.price, buy_input.quantity, tx.id)
        holdings_data[existing_idx] = existing.to_dict()
    else:
        # 신규 매수
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

    # ticker_map에 저장
    if buy_input.ticker:
        tmap = load_ticker_map()
        tmap[buy_input.name] = buy_input.ticker
        save_ticker_map(tmap)

    # Transaction 저장
    transactions = load_transactions()
    transactions.append(tx.to_dict())
    save_transactions(transactions)

    result_text = format_buy_result(
        name=buy_input.name,
        sector=buy_input.sector,
        quantity=buy_input.quantity,
        price=buy_input.price,
        thesis=buy_input.thesis,
    )
    await update.message.reply_text(result_text)
    return ConversationHandler.END


def buy_conversation() -> ConversationHandler:
    """매수 ConversationHandler를 생성하여 반환."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("buy", _start),
            MessageHandler(filters.Regex(r"^매수$"), _start),
        ],
        states={
            INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_input)],
        },
        fallbacks=[CommandHandler("cancel", _cancel_fallback)],
    )


async def _cancel_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel 명령어로 대화 중단."""
    context.user_data.pop("buy_input", None)
    await update.message.reply_text("매수 기록이 취소되었습니다.")
    return ConversationHandler.END
