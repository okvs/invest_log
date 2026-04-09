"""매수 핸들러 유닛 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers.buy import (
    EXISTING_CONFIRM,
    SECTOR_INPUT,
    THESIS_INPUT,
    _receive_input,
    _start,
    _existing_confirm,
    _sector_input,
    _thesis_input,
)
from parsers.input_parser import StockCandidate
from storage.json_store import (
    load_holdings,
    load_ticker_map,
    load_transactions,
    save_nickname_map,
    save_ticker_map,
)


def _make_update_and_context(text: str = ""):
    """가짜 Update, Context 생성."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    return update, context


def _make_callback_update(data: str):
    """가짜 콜백 Update 생성."""
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.message = None
    return update


# ── /buy 시작 ──


@pytest.mark.asyncio
async def test_start_returns_input_state():
    update, context = _make_update_and_context()
    result = await _start(update, context)
    assert result == 0  # INPUT state
    update.message.reply_text.assert_called_once()


# ── 매수 입력 파싱 실패 ──


@pytest.mark.asyncio
async def test_receive_input_invalid_returns_input():
    update, context = _make_update_and_context("잘못된 입력")
    result = await _receive_input(update, context)
    assert result == 0  # INPUT (다시 입력 요청)
    reply = update.message.reply_text.call_args[0][0]
    assert "입력 오류" in reply


# ── 신규 매수: 종목명/수량/매수가 → 섹터 → 근거 ──


@pytest.mark.asyncio
async def test_receive_input_new_buy():
    save_ticker_map({"삼성전자": "005930.KS"})

    text = "삼성전자\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == SECTOR_INPUT  # 신규 → 섹터 입력 요청

    # 섹터 입력
    sector_update, _ = _make_update_and_context("반도체")
    result = await _sector_input(sector_update, context)
    assert result == THESIS_INPUT

    # 근거 입력
    thesis_update, _ = _make_update_and_context("AI 수요 증가 전망")
    result = await _thesis_input(thesis_update, context)
    assert result == -1  # ConversationHandler.END

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "삼성전자"
    assert holdings[0]["quantity"] == 10
    assert holdings[0]["avg_price"] == 72000
    assert holdings[0]["ticker"] == "005930.KS"
    assert holdings[0]["sector"] == "반도체"
    assert holdings[0]["buy_thesis"] == "AI 수요 증가 전망"


# ── 신규 매수 — 검색으로 정확히 1개 매칭 ──


@pytest.mark.asyncio
@patch(
    "bot.handlers.buy.search_stocks",
    return_value=[StockCandidate("삼성전자", "005930", "KOSPI")],
)
async def test_receive_input_search_exact_match(mock_search):
    text = "삼성전자\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == SECTOR_INPUT  # 신규 → 섹터 입력

    sector_update, _ = _make_update_and_context("반도체")
    result = await _sector_input(sector_update, context)
    assert result == THESIS_INPUT

    thesis_update, _ = _make_update_and_context("테스트")
    result = await _thesis_input(thesis_update, context)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["ticker"] == "005930.KS"


# ── 검색 결과 여러 개 → PICK_STOCK 상태 ──


@pytest.mark.asyncio
@patch(
    "bot.handlers.buy.search_stocks",
    return_value=[
        StockCandidate("삼성전자", "005930", "KOSPI"),
        StockCandidate("삼성전기", "009150", "KOSPI"),
        StockCandidate("삼성바이오로직스", "207940", "KOSPI"),
    ],
)
async def test_receive_input_multiple_candidates(mock_search):
    text = "삼성\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == 1  # PICK_STOCK

    assert "buy_input" in context.user_data
    reply_call = update.message.reply_text.call_args
    assert reply_call.kwargs.get("reply_markup") is not None


# ── 추가 매수 — 기존 섹터+근거 유지 ──


@pytest.mark.asyncio
async def test_additional_buy_keep_existing():
    save_ticker_map({"삼성전자": "005930.KS"})

    # 1차 매수 (신규)
    text1 = "삼성전자\n10주\n70000원"
    update1, context1 = _make_update_and_context(text1)
    result = await _receive_input(update1, context1)
    assert result == SECTOR_INPUT

    sector_update, _ = _make_update_and_context("반도체")
    await _sector_input(sector_update, context1)
    thesis_update, _ = _make_update_and_context("1차 매수")
    await _thesis_input(thesis_update, context1)

    # 2차 매수 (추가) → 기존 정보 확인
    text2 = "삼성전자\n10주\n80000원"
    update2, context2 = _make_update_and_context(text2)
    result = await _receive_input(update2, context2)
    assert result == EXISTING_CONFIRM

    # "그대로 유지" 선택
    cb_update = _make_callback_update("keep_existing")
    result = await _existing_confirm(cb_update, context2)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 20
    assert holdings[0]["avg_price"] == 75000
    assert holdings[0]["sector"] == "반도체"
    assert holdings[0]["buy_thesis"] == "1차 매수"


# ── 닉네임으로 매수 ──


@pytest.mark.asyncio
async def test_receive_input_with_nickname():
    save_nickname_map({"삼전": "삼성전자"})
    save_ticker_map({"삼성전자": "005930.KS"})

    text = "삼전\n5주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == SECTOR_INPUT

    sector_update, _ = _make_update_and_context("반도체")
    await _sector_input(sector_update, context)
    thesis_update, _ = _make_update_and_context("테스트")
    await _thesis_input(thesis_update, context)

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "삼성전자"


# ── 영어 대소문자 무시 추가매수 ──


@pytest.mark.asyncio
async def test_receive_input_case_insensitive():
    save_ticker_map({"NVIDIA": "NVDA"})

    # 1차 매수
    text1 = "NVIDIA\n5주\n800원"
    update1, context1 = _make_update_and_context(text1)
    result = await _receive_input(update1, context1)
    assert result == SECTOR_INPUT
    sector1, _ = _make_update_and_context("AI")
    await _sector_input(sector1, context1)
    thesis1, _ = _make_update_and_context("1차")
    await _thesis_input(thesis1, context1)

    # 2차 매수 — 소문자
    text2 = "nvidia\n5주\n900원"
    update2, context2 = _make_update_and_context(text2)
    result = await _receive_input(update2, context2)
    assert result == EXISTING_CONFIRM

    cb_update = _make_callback_update("keep_existing")
    result = await _existing_confirm(cb_update, context2)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 10


# ── 종목명 공백 제거 ──


@pytest.mark.asyncio
async def test_stock_name_no_spaces():
    save_ticker_map({"삼성전자": "005930.KS"})

    text = "삼성 전자\n5주\n72000원"
    update, context = _make_update_and_context(text)
    result = await _receive_input(update, context)

    # 공백이 제거되어 "삼성전자"로 매칭
    assert result == SECTOR_INPUT
    assert context.user_data["buy_input"].name == "삼성전자"
