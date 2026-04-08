"""매수 핸들러 유닛 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers.buy import (
    THESIS_CONFIRM,
    THESIS_INPUT,
    _receive_input,
    _start,
    _thesis_confirm,
    _thesis_input,
)
from parsers.input_parser import StockCandidate
from storage.json_store import (
    load_holdings,
    load_ticker_map,
    load_transactions,
    save_holdings,
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


# ── 신규 매수 성공 (ticker_map 캐시 히트) → THESIS_INPUT 상태 ──


@pytest.mark.asyncio
async def test_receive_input_new_buy():
    # ticker_map에 미리 등록
    save_ticker_map({"삼성전자": "005930.KS"})

    text = "삼성전자\n반도체\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == THESIS_INPUT  # 신규 종목 → 근거 입력 요청

    # buy_input이 context에 저장됨
    assert "buy_input" in context.user_data

    # thesis 입력
    thesis_update, _ = _make_update_and_context("AI 수요 증가 전망")
    result = await _thesis_input(thesis_update, context)
    assert result == -1  # ConversationHandler.END

    # holdings 저장 확인
    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "삼성전자"
    assert holdings[0]["quantity"] == 10
    assert holdings[0]["avg_price"] == 72000
    assert holdings[0]["ticker"] == "005930.KS"
    assert holdings[0]["buy_thesis"] == "AI 수요 증가 전망"

    # transaction 저장 확인
    txs = load_transactions()
    assert len(txs) == 1
    assert txs[0]["type"] == "buy"
    assert txs[0]["name"] == "삼성전자"

    # 응답 메시지 확인
    reply = thesis_update.message.reply_text.call_args[0][0]
    assert "매수 기록 완료" in reply
    assert "005930.KS" in reply


# ── 신규 매수 — 검색으로 정확히 1개 매칭 ──


@pytest.mark.asyncio
@patch(
    "bot.handlers.buy.search_stocks",
    return_value=[StockCandidate("삼성전자", "005930", "KOSPI")],
)
async def test_receive_input_search_exact_match(mock_search):
    text = "삼성전자\n반도체\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == THESIS_INPUT  # 신규 → 근거 입력

    # thesis 입력
    thesis_update, _ = _make_update_and_context("AI 수요 증가 전망")
    result = await _thesis_input(thesis_update, context)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["ticker"] == "005930.KS"

    tmap = load_ticker_map()
    assert tmap["삼성전자"] == "005930.KS"


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
    text = "삼성\n반도체\n10주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == 1  # PICK_STOCK

    # buy_input이 context에 저장되어야 함
    assert "buy_input" in context.user_data
    # 선택 키보드가 표시되어야 함
    reply_call = update.message.reply_text.call_args
    assert reply_call.kwargs.get("reply_markup") is not None


# ── 추가 매수 — 기존 사유 유지 ──


@pytest.mark.asyncio
async def test_receive_input_additional_buy_keep_thesis():
    save_ticker_map({"삼성전자": "005930.KS"})

    # 1차 매수
    text1 = "삼성전자\n반도체\n10주\n70000원"
    update1, context1 = _make_update_and_context(text1)
    result = await _receive_input(update1, context1)
    assert result == THESIS_INPUT

    thesis_update1, _ = _make_update_and_context("1차 매수")
    await _thesis_input(thesis_update1, context1)

    # 2차 매수 → 기존 사유 확인
    text2 = "삼성전자\n반도체\n10주\n80000원"
    update2, context2 = _make_update_and_context(text2)
    result = await _receive_input(update2, context2)
    assert result == THESIS_CONFIRM  # 기존 사유 유지/수정 선택

    # "그대로 유지" 선택
    cb_update = _make_callback_update("keep_thesis")
    cb_update.callback_query.data = "keep_thesis"
    context2.user_data = context2.user_data  # buy_input 유지
    result = await _thesis_confirm(cb_update, context2)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 20
    assert holdings[0]["avg_price"] == 75000
    assert holdings[0]["buy_thesis"] == "1차 매수"

    txs = load_transactions()
    assert len(txs) == 2


# ── 닉네임으로 매수 ──


@pytest.mark.asyncio
async def test_receive_input_with_nickname():
    save_nickname_map({"삼전": "삼성전자"})
    save_ticker_map({"삼성전자": "005930.KS"})

    text = "삼전\n반도체\n5주\n72000원"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == THESIS_INPUT

    thesis_update, _ = _make_update_and_context("닉네임 테스트")
    result = await _thesis_input(thesis_update, context)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "삼성전자"  # 닉네임이 실제 종목명으로 변환됨


# ── 영어 대소문자 무시 매수 ──


@pytest.mark.asyncio
async def test_receive_input_case_insensitive():
    save_ticker_map({"NVIDIA": "NVDA"})

    # 1차 매수 — 대문자
    text1 = "NVIDIA\nAI\n5주\n800원"
    update1, context1 = _make_update_and_context(text1)
    result = await _receive_input(update1, context1)
    assert result == THESIS_INPUT

    thesis1, _ = _make_update_and_context("1차")
    await _thesis_input(thesis1, context1)

    # 2차 매수 — 소문자 (같은 종목으로 인식되어야 함)
    text2 = "nvidia\nAI\n5주\n900원"
    update2, context2 = _make_update_and_context(text2)
    result = await _receive_input(update2, context2)
    assert result == THESIS_CONFIRM  # 기존 종목이므로 사유 확인

    # 유지 선택
    cb_update = _make_callback_update("keep_thesis")
    result = await _thesis_confirm(cb_update, context2)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 10  # 추가 매수로 합산
