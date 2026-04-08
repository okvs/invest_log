"""증권사 체결 메시지 핸들러 테스트."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers.broker import (
    _receive_broker_msg,
    _sell_reason,
    _buy_sector,
    _buy_thesis,
    SELL_REASON,
    BUY_SECTOR,
    BUY_THESIS,
)
from bot.handlers.sell import RETRO_ASK
from storage.json_store import (
    load_holdings,
    load_transactions,
    save_holdings,
)


def _make_update_and_context(text: str = ""):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    return update, context


def _seed_holding(name="삼성전자", quantity=10, avg_price=72000):
    from models.portfolio import Holding

    h = Holding(
        name=name,
        ticker="005930.KS",
        sector="반도체",
        buy_date="2026-01-01",
        avg_price=avg_price,
        quantity=quantity,
        total_invested=avg_price * quantity,
        buy_thesis="테스트 근거",
    )
    save_holdings([h.to_dict()])
    return h


# ── KB증권 ──

KB_SELL_MSG = (
    "[KB증권] 주식 체결 안내\n\n"
    "고객님, 주문하신 삼성전자 주식이 체결됐으니 확인해주세요.\n\n"
    "■ 계좌: ***-***-*12 [01] \n"
    "■ 종목명: 삼성전자 \n"
    "■ 주문수량: 5주 \n"
    "■ 체결금액: 85,000원 \n"
    "■ 내용: 매도체결(20147154)"
)

KB_BUY_MSG = (
    "[KB증권] 주식 체결 안내\n\n"
    "고객님, 주문하신 삼성전자 주식이 체결됐으니 확인해주세요.\n\n"
    "■ 계좌: ***-***-*12 [01] \n"
    "■ 종목명: 삼성전자 \n"
    "■ 주문수량: 10주 \n"
    "■ 체결금액: 72,000원 \n"
    "■ 내용: 매수체결(20147155)"
)


@pytest.mark.asyncio
async def test_kb_sell_asks_reason():
    _seed_holding(quantity=10)
    update, context = _make_update_and_context(KB_SELL_MSG)

    result = await _receive_broker_msg(update, context)
    assert result == SELL_REASON

    msg = context.user_data["broker_sell"]
    assert msg.name == "삼성전자"
    assert msg.quantity == 5
    assert msg.price == 85000.0  # 체결금액 = 주당 가격

    reply = update.message.reply_text.call_args[0][0]
    assert "매도사유" in reply


@pytest.mark.asyncio
async def test_kb_sell_full_flow():
    _seed_holding(quantity=10, avg_price=72000)

    # Step 1: KB 매도 메시지
    update1, context = _make_update_and_context(KB_SELL_MSG)
    await _receive_broker_msg(update1, context)

    # Step 2: 매도사유
    update2, _ = _make_update_and_context("목표가 도달")
    result = await _sell_reason(update2, context)
    assert result == RETRO_ASK

    holdings = load_holdings()
    assert holdings[0]["quantity"] == 5

    txs = load_transactions()
    assert txs[0]["type"] == "sell"
    assert txs[0]["sell_reason"] == "목표가 도달"
    assert txs[0]["profit_loss"] == (85000 - 72000) * 5


@pytest.mark.asyncio
async def test_kb_buy_asks_sector():
    update, context = _make_update_and_context(KB_BUY_MSG)

    result = await _receive_broker_msg(update, context)
    assert result == BUY_SECTOR

    msg = context.user_data["broker_buy"]
    assert msg.name == "삼성전자"
    assert msg.quantity == 10
    assert msg.price == 72000.0  # 체결금액 = 주당 가격

    reply = update.message.reply_text.call_args[0][0]
    assert "섹터" in reply


@pytest.mark.asyncio
async def test_kb_buy_full_flow():
    save_holdings([])

    # Step 1: KB 매수 메시지
    update1, context = _make_update_and_context(KB_BUY_MSG)
    await _receive_broker_msg(update1, context)

    # Step 2: 섹터
    update2, _ = _make_update_and_context("반도체")
    result = await _buy_sector(update2, context)
    assert result == BUY_THESIS

    # Step 3: 매수근거
    update3, _ = _make_update_and_context("AI 수요 증가")
    result = await _buy_thesis(update3, context)
    assert result == -1  # ConversationHandler.END

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "삼성전자"
    assert holdings[0]["quantity"] == 10
    assert holdings[0]["avg_price"] == 72000
    assert holdings[0]["sector"] == "반도체"
    assert holdings[0]["buy_thesis"] == "AI 수요 증가"


# ── 신한증권 ──

SHINHAN_SELL_MSG = (
    "계좌명 : 정승민\n"
    "계좌번호 : 270-82-8***75\n"
    "종목명 : 삼성전자\n"
    "종목코드 : 005930\n"
    "체결구분 : 매도체결\n"
    "체결수량 : 5주\n"
    "체결단가 : 85000원\n"
    "-------------------------------\n"
    "주문수량 : 5주\n"
    "누적체결수량 : 5주\n"
    "-------------------------------\n"
    "체결 내역을 확인해보세요."
)

SHINHAN_BUY_MSG = (
    "계좌명 : 정승민\n"
    "계좌번호 : 270-82-8***75\n"
    "종목명 : 파두\n"
    "종목코드 : 440110\n"
    "체결구분 : 매수체결\n"
    "체결수량 : 30주\n"
    "체결단가 : 1680원\n"
    "-------------------------------\n"
    "주문수량 : 30주\n"
    "누적체결수량 : 30주\n"
    "-------------------------------\n"
    "체결 내역을 확인해보세요."
)


@pytest.mark.asyncio
async def test_shinhan_sell_asks_reason():
    _seed_holding(quantity=10)
    update, context = _make_update_and_context(SHINHAN_SELL_MSG)

    result = await _receive_broker_msg(update, context)
    assert result == SELL_REASON

    msg = context.user_data["broker_sell"]
    assert msg.name == "삼성전자"
    assert msg.quantity == 5
    assert msg.price == 85000.0  # 신한은 체결단가 = 주당가격

    reply = update.message.reply_text.call_args[0][0]
    assert "매도사유" in reply


@pytest.mark.asyncio
async def test_shinhan_sell_full_flow():
    _seed_holding(quantity=10, avg_price=72000)

    update1, context = _make_update_and_context(SHINHAN_SELL_MSG)
    await _receive_broker_msg(update1, context)

    update2, _ = _make_update_and_context("익절")
    result = await _sell_reason(update2, context)
    assert result == RETRO_ASK

    holdings = load_holdings()
    assert holdings[0]["quantity"] == 5

    txs = load_transactions()
    assert txs[0]["profit_loss"] == (85000 - 72000) * 5


@pytest.mark.asyncio
async def test_shinhan_buy_full_flow():
    save_holdings([])

    update1, context = _make_update_and_context(SHINHAN_BUY_MSG)
    await _receive_broker_msg(update1, context)

    update2, _ = _make_update_and_context("AI반도체")
    result = await _buy_sector(update2, context)
    assert result == BUY_THESIS

    update3, _ = _make_update_and_context("저가 매수")
    result = await _buy_thesis(update3, context)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "파두"
    assert holdings[0]["quantity"] == 30
    assert holdings[0]["avg_price"] == 1680
    assert holdings[0]["sector"] == "AI반도체"
