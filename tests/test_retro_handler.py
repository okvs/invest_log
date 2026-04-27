"""회고 핸들러 유닛 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ConversationHandler

from bot.handlers.retro import (
    SELECT,
    THESIS,
    _select_transaction,
    _start_retro,
)
from bot.keyboards import RETRO_SELECT_PREFIX
from models.transaction import Transaction
from storage.json_store import (
    load_retrospectives,
    load_transactions,
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
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    return update, context


def _seed_sell_tx(retrospective_id: str = "", buy_thesis: str = "원래 근거") -> Transaction:
    """회고 대상 매도 거래 시드."""
    tx = Transaction(
        type="sell",
        name="삼성전자",
        sector="반도체",
        price=85000,
        quantity=5,
        total_amount=85000 * 5,
        profit_loss=65000,
        profit_loss_pct=18.0,
        sell_reason="목표가 도달",
        buy_thesis=buy_thesis,
    )
    tx_dict = tx.to_dict()
    if retrospective_id:
        tx_dict["retrospective_id"] = retrospective_id
    save_transactions([tx_dict])
    return tx


# ── /회고 시작: 미회고 매도 카드 표시 ──


@pytest.mark.asyncio
async def test_start_retro_shows_pending_sells():
    _seed_sell_tx()
    update, context = _make_update_and_context()
    result = await _start_retro(update, context)
    assert result == SELECT

    call_kwargs = update.message.reply_text.call_args
    assert "선택" in call_kwargs[0][0]
    assert call_kwargs[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_start_retro_no_pending_sells():
    save_transactions([])
    update, context = _make_update_and_context()
    result = await _start_retro(update, context)
    assert result == ConversationHandler.END
    reply = update.message.reply_text.call_args[0][0]
    assert "회고할 매도 거래가 없습니다" in reply


@pytest.mark.asyncio
async def test_start_retro_excludes_already_retroed():
    _seed_sell_tx(retrospective_id="r1")
    update, context = _make_update_and_context()
    result = await _start_retro(update, context)
    # 이미 회고한 거래만 있으면 빈 상태와 동일
    assert result == ConversationHandler.END


# ── 거래 선택 → THESIS 단계 ──


@pytest.mark.asyncio
async def test_select_transaction_advances_to_thesis():
    tx = _seed_sell_tx(buy_thesis="AI 수요 증가")
    update, context = _make_callback_update(f"{RETRO_SELECT_PREFIX}{tx.id}")
    result = await _select_transaction(update, context)
    assert result == THESIS

    edit_text = update.callback_query.edit_message_text.call_args[0][0]
    assert "삼성전자" in edit_text
    assert "AI 수요 증가" in edit_text
    assert context.user_data["retro_tx"]["id"] == tx.id


@pytest.mark.asyncio
async def test_select_transaction_already_retroed():
    tx = _seed_sell_tx(retrospective_id="r1")
    update, context = _make_callback_update(f"{RETRO_SELECT_PREFIX}{tx.id}")
    result = await _select_transaction(update, context)
    assert result == ConversationHandler.END
    edit_text = update.callback_query.edit_message_text.call_args[0][0]
    assert "이미 회고가 작성된" in edit_text


@pytest.mark.asyncio
async def test_select_transaction_unknown_id():
    _seed_sell_tx()
    update, context = _make_callback_update(f"{RETRO_SELECT_PREFIX}does-not-exist")
    result = await _select_transaction(update, context)
    assert result == ConversationHandler.END


# ── 매도 후 retrospective 미연결 상태 검증 ──


@pytest.mark.asyncio
async def test_seeded_sell_has_no_retrospective():
    _seed_sell_tx()
    txs = load_transactions()
    assert txs[0]["retrospective_id"] == ""
    assert load_retrospectives() == []
