"""매도 핸들러 유닛 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.handlers.sell import (
    _receive_kb_sell,
    _receive_sell_input,
    _receive_sell_reason,
    _select_holding,
    _start_sell,
    RETRO_ASK,
    SELECT,
    SELL_REASON,
    INPUT,
)
from bot.keyboards import SELL_SELECT_PREFIX
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


def _make_callback_update(data: str):
    """콜백 쿼리를 가진 가짜 Update 생성."""
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

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


# ── /sell 시작 → 종목 선택 카드 ──


@pytest.mark.asyncio
async def test_start_sell_shows_holdings():
    _seed_holding()
    update, context = _make_update_and_context()
    result = await _start_sell(update, context)
    assert result == SELECT  # 종목 선택 상태

    call_kwargs = update.message.reply_text.call_args
    assert "선택" in call_kwargs[0][0]
    assert call_kwargs[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_start_sell_empty_holdings():
    save_holdings([])
    update, context = _make_update_and_context()
    result = await _start_sell(update, context)
    assert result == -1  # ConversationHandler.END
    reply = update.message.reply_text.call_args[0][0]
    assert "보유 중인 종목이 없습니다" in reply


# ── 종목 선택 → INPUT 상태 ──


@pytest.mark.asyncio
async def test_select_holding():
    _seed_holding(quantity=10)
    update, context = _make_callback_update(f"{SELL_SELECT_PREFIX}삼성전자")
    result = await _select_holding(update, context)
    assert result == INPUT
    assert context.user_data["sell_name"] == "삼성전자"

    edit_text = update.callback_query.edit_message_text.call_args[0][0]
    assert "삼성전자" in edit_text
    assert "10주" in edit_text


# ── 매도 파싱 실패 (종목 선택 후 3줄 미만) ──


@pytest.mark.asyncio
async def test_receive_sell_invalid_input():
    update, context = _make_update_and_context("잘못된")
    context.user_data["sell_name"] = "삼성전자"
    result = await _receive_sell_input(update, context)
    assert result == INPUT
    reply = update.message.reply_text.call_args[0][0]
    assert "입력 오류" in reply


# ── 보유량 초과 매도 ──


@pytest.mark.asyncio
async def test_receive_sell_exceeds_quantity():
    _seed_holding(quantity=5)
    text = "10주\n85000원\n목표가 도달"
    update, context = _make_update_and_context(text)
    context.user_data["sell_name"] = "삼성전자"
    result = await _receive_sell_input(update, context)
    assert result == INPUT
    reply = update.message.reply_text.call_args[0][0]
    assert "매도할 수 없습니다" in reply


# ── 부분 매도 성공 (종목 선택 후 3줄 입력) ──


@pytest.mark.asyncio
async def test_receive_sell_partial():
    _seed_holding(quantity=10, avg_price=72000)
    text = "5주\n85000원\n목표가 도달"
    update, context = _make_update_and_context(text)
    context.user_data["sell_name"] = "삼성전자"

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
    text = "10주\n85000원\n전량 매도"
    update, context = _make_update_and_context(text)
    context.user_data["sell_name"] = "삼성전자"

    result = await _receive_sell_input(update, context)
    assert result == RETRO_ASK

    holdings = load_holdings()
    assert len(holdings) == 0  # 전량 매도 시 제거


# ── 손실 매도 ──


@pytest.mark.asyncio
async def test_receive_sell_loss():
    _seed_holding(quantity=10, avg_price=72000)
    text = "5주\n60000원\n손절"
    update, context = _make_update_and_context(text)
    context.user_data["sell_name"] = "삼성전자"

    result = await _receive_sell_input(update, context)
    assert result == RETRO_ASK

    txs = load_transactions()
    assert txs[0]["profit_loss"] == (60000 - 72000) * 5  # -60000
    assert txs[0]["profit_loss_pct"] < 0


# ── KB증권 체결 메시지 매도 ──

KB_MSG = (
    "[KB증권] 주식 체결 안내\n\n"
    "고객님, 주문하신 삼성전자 주식이 체결됐으니 확인해주세요.\n\n"
    "■ 계좌: ***-***-*12 [01] \n"
    "■ 종목명: 삼성전자 \n"
    "■ 주문수량: 5주 \n"
    "■ 체결금액: 425,000원 \n"
    "■ 내용: 매도체결(20147154)"
)


@pytest.mark.asyncio
async def test_kb_sell_parses_and_asks_reason():
    _seed_holding(quantity=10, avg_price=72000)
    update, context = _make_update_and_context(KB_MSG)

    result = await _receive_kb_sell(update, context)
    assert result == SELL_REASON

    # 파싱 결과가 user_data에 저장됨
    assert context.user_data["kb_sell_name"] == "삼성전자"
    assert context.user_data["kb_sell_quantity"] == 5
    assert context.user_data["kb_sell_price"] == 85000.0  # 425000 / 5

    # 매도사유 입력 요청
    reply = update.message.reply_text.call_args[0][0]
    assert "매도사유" in reply


@pytest.mark.asyncio
async def test_kb_sell_reason_completes_sell():
    _seed_holding(quantity=10, avg_price=72000)

    # Step 1: KB 메시지
    update1, context = _make_update_and_context(KB_MSG)
    await _receive_kb_sell(update1, context)

    # Step 2: 매도사유 입력
    update2, _ = _make_update_and_context("목표가 도달")
    # context 유지
    result = await _receive_sell_reason(update2, context)
    assert result == RETRO_ASK

    # 보유량 5주로 감소
    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 5

    # transaction 확인
    txs = load_transactions()
    assert len(txs) == 1
    assert txs[0]["type"] == "sell"
    assert txs[0]["name"] == "삼성전자"
    assert txs[0]["sell_reason"] == "목표가 도달"
    assert txs[0]["profit_loss"] == (85000 - 72000) * 5
