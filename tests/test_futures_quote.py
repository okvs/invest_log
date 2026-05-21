"""선물 시세 조회/수동 입력 테스트."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ConversationHandler

import bot.futures_quote as fq
from bot.handlers.futures_quote import (
    PRICE,
    SELECT,
    _receive_price,
    _select_position,
    _start,
)
from bot.keyboards import FUTURES_POS_PREFIX
from models.futures_position import FuturesPosition
from storage.json_store import save_futures_positions, save_ticker_map


def _make_update(text: str = ""):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}
    return update, context


def _make_callback(data: str):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}
    return update, context


def _seed_position(symbol="005930") -> FuturesPosition:
    pos = FuturesPosition(
        name="삼성전자",
        symbol=symbol,
        contract_code="",
        contract_month="202606",
        expiry_date="2026-06-11",
        direction="long",
        contracts=2,
        avg_entry_price=70000.0,
        initial_margin=2520000.0,
    )
    save_futures_positions([pos.to_dict()])
    return pos


# ── 수동 시세 저장/조회 ─────────────────────────────────────────────────


def test_set_and_read_manual_quote():
    fq.set_manual_quote("005930", 72500.0)
    quotes = fq._read_fresh_manual_quotes()
    assert quotes["005930"] == 72500.0


def test_expired_manual_quote_removed():
    fq.set_manual_quote("005930", 72500.0)
    # 만료 시각으로 강제 변경
    quotes = fq._load_quotes()
    quotes["005930"]["ts"] = time.time() - fq.QUOTE_TTL_SECONDS - 1
    fq._save_quotes(quotes)
    # 다시 읽으면 만료 항목 자동 정리
    fresh = fq._read_fresh_manual_quotes()
    assert "005930" not in fresh


# ── fetch_futures_prices: 수동 우선, yfinance 폴백 ──────────────────────


@pytest.mark.asyncio
async def test_fetch_uses_manual_quote_first():
    pos = _seed_position()
    fq.set_manual_quote("005930", 71800.0)
    with patch("bot.futures_quote.fetch_current_prices") as mock_fetch:
        # yfinance가 호출되지 않아도 되도록 (수동 시세 있으면 우회)
        mock_fetch.return_value = {"005930.KS": 99999.0}
        result = await fq.fetch_futures_prices([pos.to_dict()])
    assert result["005930"] == 71800.0  # yfinance 결과 무시


@pytest.mark.asyncio
async def test_fetch_falls_back_to_yfinance():
    pos = _seed_position()
    save_ticker_map({"삼성전자": "005930.KS"})
    with patch("bot.futures_quote.fetch_current_prices") as mock_fetch:
        mock_fetch.return_value = {"005930.KS": 72000.0}
        result = await fq.fetch_futures_prices([pos.to_dict()])
    assert result["005930"] == 72000.0


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_yfinance_fails():
    pos = _seed_position()
    with patch("bot.futures_quote.fetch_current_prices") as mock_fetch:
        mock_fetch.return_value = {}  # yfinance가 빈 응답
        result = await fq.fetch_futures_prices([pos.to_dict()])
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_with_empty_positions():
    assert await fq.fetch_futures_prices([]) == {}


# ── 선물시세 핸들러 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quote_handler_no_positions_ends():
    save_futures_positions([])
    update, ctx = _make_update()
    result = await _start(update, ctx)
    assert result == ConversationHandler.END


@pytest.mark.asyncio
async def test_quote_handler_save_path():
    pos = _seed_position()
    # 1) 포지션 선택
    sel_update, ctx = _make_callback(f"{FUTURES_POS_PREFIX}{pos.id}")
    result = await _select_position(sel_update, ctx)
    assert result == PRICE
    assert ctx.user_data["fut_quote_symbol"] == "005930"

    # 2) 가격 입력
    price_update = _make_update("72500")[0]
    result = await _receive_price(price_update, ctx)
    assert result == ConversationHandler.END

    fresh = fq._read_fresh_manual_quotes()
    assert fresh["005930"] == 72500.0


@pytest.mark.asyncio
async def test_quote_handler_invalid_price_stays():
    pos = _seed_position()
    sel_update, ctx = _make_callback(f"{FUTURES_POS_PREFIX}{pos.id}")
    await _select_position(sel_update, ctx)

    bad_update = _make_update("abc")[0]
    result = await _receive_price(bad_update, ctx)
    assert result == PRICE
    assert "입력 오류" in bad_update.message.reply_text.call_args[0][0]
