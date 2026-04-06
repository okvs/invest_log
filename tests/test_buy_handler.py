"""매수 핸들러 유닛 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers.buy import _receive_input, _start
from storage.json_store import load_holdings, load_ticker_map, load_transactions


def _make_update_and_context(text: str = ""):
    """가짜 Update, Context 생성."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    return update, context


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


# ── 신규 매수 성공 ──


@pytest.mark.asyncio
async def test_receive_input_new_buy():
    text = "삼성전자\n005930\n반도체\n10주\n72000원\nAI 수요 증가 전망"
    update, context = _make_update_and_context(text)

    result = await _receive_input(update, context)
    assert result == -1  # ConversationHandler.END

    # holdings 저장 확인
    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["name"] == "삼성전자"
    assert holdings[0]["quantity"] == 10
    assert holdings[0]["avg_price"] == 72000

    # transaction 저장 확인
    txs = load_transactions()
    assert len(txs) == 1
    assert txs[0]["type"] == "buy"
    assert txs[0]["name"] == "삼성전자"

    # ticker_map 저장 확인
    tmap = load_ticker_map()
    assert tmap["삼성전자"] == "005930.KS"

    # 응답 메시지 확인
    reply = update.message.reply_text.call_args[0][0]
    assert "매수 기록 완료" in reply


# ── 추가 매수 — 평균단가 재계산 ──


@pytest.mark.asyncio
async def test_receive_input_additional_buy():
    # 1차 매수
    text1 = "삼성전자\n005930\n반도체\n10주\n70000원\n1차 매수"
    update1, context1 = _make_update_and_context(text1)
    await _receive_input(update1, context1)

    # 2차 매수
    text2 = "삼성전자\n005930\n반도체\n10주\n80000원\n추가 매수"
    update2, context2 = _make_update_and_context(text2)
    result = await _receive_input(update2, context2)
    assert result == -1

    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 20
    # 평균단가: (700000 + 800000) / 20 = 75000
    assert holdings[0]["avg_price"] == 75000

    txs = load_transactions()
    assert len(txs) == 2
