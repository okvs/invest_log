"""매도 핸들러 유닛 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram.ext import ConversationHandler

from bot.handlers.sell import (
    INPUT,
    REASON,
    SELECT,
    _reason_pick,
    _reason_text,
    _receive_sell_input,
    _select_holding,
    _start_sell,
)
from bot.keyboards import REASON_PICK_PREFIX, SELL_SELECT_PREFIX
from storage.json_store import (
    load_holdings,
    load_transactions,
    save_holdings,
    save_transactions,
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
    # query.message.reply_text 도 비동기 호출이 일어날 수 있어 AsyncMock으로 세팅
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()

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
    assert result == ConversationHandler.END

    # 보유량 5주로 감소
    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 5

    # transaction 저장
    txs = load_transactions()
    assert len(txs) == 1
    assert txs[0]["type"] == "sell"
    assert txs[0]["profit_loss"] == (85000 - 72000) * 5  # +65000
    # buy_thesis 스냅샷 저장
    assert txs[0]["buy_thesis"] == "테스트 근거"
    # 회고는 매도 시점에 진행하지 않음
    assert txs[0]["retrospective_id"] == ""

    # 결과 메시지에 매도 결과 + 회고 안내 문구 포함
    result_msg = update.message.reply_text.call_args[0][0]
    assert "매도 기록 완료" in result_msg
    assert "회고" in result_msg


# ── 전량 매도 → 보유 목록에서 제거 ──


@pytest.mark.asyncio
async def test_receive_sell_full_removes_holding():
    _seed_holding(quantity=10, avg_price=72000)
    text = "10주\n85000원\n전량 매도"
    update, context = _make_update_and_context(text)
    context.user_data["sell_name"] = "삼성전자"

    result = await _receive_sell_input(update, context)
    assert result == ConversationHandler.END

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
    assert result == ConversationHandler.END

    txs = load_transactions()
    assert txs[0]["profit_loss"] == (60000 - 72000) * 5  # -60000
    assert txs[0]["profit_loss_pct"] < 0


# ── 사유 미입력 → REASON 상태로 진입하여 사유 별도 입력 ──


def _seed_past_sells(name: str, reasons: list[str]) -> None:
    """과거 매도 거래를 시드 (최근 사유 버튼 노출용)."""
    txs = []
    for i, reason in enumerate(reasons):
        txs.append({
            "id": f"sell-{i}",
            "type": "sell",
            "name": name,
            "sector": "",
            "date": f"2026-03-0{i+1}T10:00:00",
            "price": 100.0,
            "quantity": 1,
            "total_amount": 100.0,
            "sell_reason": reason,
        })
    save_transactions(txs)


@pytest.mark.asyncio
async def test_receive_sell_two_lines_goes_to_reason():
    """수량/매도가만 입력하면 REASON 상태로 가서 사유 입력을 별도로 받음."""
    _seed_holding(quantity=10, avg_price=72000)
    _seed_past_sells("삼성전자", ["목표가 도달", "리밸런싱"])

    text = "5주\n85000원"
    update, context = _make_update_and_context(text)
    context.user_data["sell_name"] = "삼성전자"

    result = await _receive_sell_input(update, context)
    assert result == REASON
    assert context.user_data["sell_qty"] == 5
    assert context.user_data["sell_price"] == 85000
    # 최근 사유가 컨텍스트와 키보드에 노출
    assert context.user_data["recent_reasons"] == ["리밸런싱", "목표가 도달"]
    last_call = update.message.reply_text.call_args
    assert last_call.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_reason_pick_saves_clicked_reason():
    """REASON 상태에서 버튼 클릭 시 그 사유로 매도 저장."""
    _seed_holding(quantity=10, avg_price=72000)
    _seed_past_sells("삼성전자", ["목표가 도달"])

    # 2-line 입력으로 REASON 상태 진입
    update, context = _make_update_and_context("5주\n85000원")
    context.user_data["sell_name"] = "삼성전자"
    await _receive_sell_input(update, context)

    # 버튼 클릭
    cb_update = _make_callback_update(f"{REASON_PICK_PREFIX}0")[0]
    # context 그대로 사용
    result = await _reason_pick(cb_update, context)
    assert result == ConversationHandler.END

    txs = [t for t in load_transactions() if t["type"] == "sell" and t["quantity"] == 5]
    assert len(txs) == 1
    assert txs[0]["sell_reason"] == "목표가 도달"


@pytest.mark.asyncio
async def test_reason_text_saves_typed_reason():
    """REASON 상태에서 텍스트 직접 입력 시 그 사유로 저장."""
    _seed_holding(quantity=10, avg_price=72000)

    update, context = _make_update_and_context("5주\n85000원")
    context.user_data["sell_name"] = "삼성전자"
    await _receive_sell_input(update, context)

    text_update, _ = _make_update_and_context("새로운 매도 사유")
    result = await _reason_text(text_update, context)
    assert result == ConversationHandler.END

    txs = load_transactions()
    sells = [t for t in txs if t["type"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["sell_reason"] == "새로운 매도 사유"
