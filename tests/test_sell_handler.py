"""매도 핸들러 유닛 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.handlers.sell import _receive_sell_input, _start_sell, RETRO_ASK
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
    """테스트용 보유 종목 생성."""
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


# ── /sell 시작 ──


@pytest.mark.asyncio
async def test_start_sell_returns_input():
    update, context = _make_update_and_context()
    result = await _start_sell(update, context)
    assert result == 0  # INPUT


# ── 매도 파싱 실패 ──


@pytest.mark.asyncio
async def test_receive_sell_invalid_input():
    update, context = _make_update_and_context("잘못된")
    result = await _receive_sell_input(update, context)
    assert result == 0  # INPUT
    reply = update.message.reply_text.call_args[0][0]
    assert "입력 오류" in reply


# ── 미보유 종목 매도 시도 ──


@pytest.mark.asyncio
async def test_receive_sell_not_held():
    text = "카카오\n5주\n85000원\n목표가 도달"
    update, context = _make_update_and_context(text)
    result = await _receive_sell_input(update, context)
    assert result == 0
    reply = update.message.reply_text.call_args[0][0]
    assert "보유 종목이 아닙니다" in reply


# ── 보유량 초과 매도 ──


@pytest.mark.asyncio
async def test_receive_sell_exceeds_quantity():
    _seed_holding(quantity=5)
    text = "삼성전자\n10주\n85000원\n목표가 도달"
    update, context = _make_update_and_context(text)
    result = await _receive_sell_input(update, context)
    assert result == 0
    reply = update.message.reply_text.call_args[0][0]
    assert "매도할 수 없습니다" in reply


# ── 부분 매도 성공 ──


@pytest.mark.asyncio
async def test_receive_sell_partial():
    _seed_holding(quantity=10, avg_price=72000)
    text = "삼성전자\n5주\n85000원\n목표가 도달"
    update, context = _make_update_and_context(text)

    result = await _receive_sell_input(update, context)
    assert result == RETRO_ASK

    # 보유량 5주로 감소
    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 5

    # transaction 저장
    txs = load_transactions()
    assert len(txs) == 1
    assert txs[0]["type"] == "sell"
    assert txs[0]["profit_loss"] == (85000 - 72000) * 5  # +65000

    # 결과 메시지
    calls = update.message.reply_text.call_args_list
    result_msg = calls[0][0][0]
    assert "매도 기록 완료" in result_msg

    # 회고 질문
    retro_msg = calls[1][0][0]
    assert "회고" in retro_msg


# ── 전량 매도 → 보유 목록에서 제거 ──


@pytest.mark.asyncio
async def test_receive_sell_full_removes_holding():
    _seed_holding(quantity=10, avg_price=72000)
    text = "삼성전자\n10주\n85000원\n전량 매도"
    update, context = _make_update_and_context(text)

    result = await _receive_sell_input(update, context)
    assert result == RETRO_ASK

    holdings = load_holdings()
    assert len(holdings) == 0  # 전량 매도 시 제거


# ── 손실 매도 ──


@pytest.mark.asyncio
async def test_receive_sell_loss():
    _seed_holding(quantity=10, avg_price=72000)
    text = "삼성전자\n5주\n60000원\n손절"
    update, context = _make_update_and_context(text)

    result = await _receive_sell_input(update, context)
    assert result == RETRO_ASK

    txs = load_transactions()
    assert txs[0]["profit_loss"] == (60000 - 72000) * 5  # -60000
    assert txs[0]["profit_loss_pct"] < 0
