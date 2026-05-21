"""선물 회고 + 대시보드 HTML 섹션 테스트."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ConversationHandler

from bot.futures_report import build_futures_section
from bot.handlers.futures_retro import (
    SELECT,
    THESIS,
    _select_tx,
    _start as _retro_start,
    _save,
    _lessons_skip,
    _regrets_skip,
    _avoidable,
    _thesis_eval,
    _well,
)
from bot.html_report import build_html_report
from bot.keyboards import (
    AVOIDABLE_YES,
    FUTURES_RETRO_PREFIX,
    THESIS_CORRECT,
)
from models.futures_position import FuturesPosition
from models.futures_transaction import FuturesTransaction
from storage.json_store import (
    load_futures_transactions,
    load_retrospectives,
    save_futures_positions,
    save_futures_transactions,
)


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


def _seed_close_tx(retrospective_id: str = "") -> FuturesTransaction:
    tx = FuturesTransaction(
        type="close",
        name="삼성전자",
        symbol="005930",
        contract_code="",
        contract_month="202606",
        expiry_date="2026-06-11",
        direction="long",
        contracts=2,
        price=72000.0,
        margin=2520000.0,
        pnl=40000.0,
        pnl_pct=2.86,
        buy_thesis="HBM 수요",
        position_id="pos-1",
    )
    if retrospective_id:
        tx.retrospective_id = retrospective_id
    save_futures_transactions([tx.to_dict()])
    return tx


# ── 카드 노출 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retro_empty_ends_early():
    save_futures_transactions([])
    update, ctx = _make_update()
    result = await _retro_start(update, ctx)
    assert result == ConversationHandler.END


@pytest.mark.asyncio
async def test_retro_shows_only_pending():
    """이미 회고된 거래는 카드에 노출되지 않는다."""
    _seed_close_tx(retrospective_id="already-done")
    update, ctx = _make_update()
    result = await _retro_start(update, ctx)
    assert result == ConversationHandler.END  # 미회고가 없으므로 종료


@pytest.mark.asyncio
async def test_retro_select_proceeds_to_thesis():
    tx = _seed_close_tx()
    update, ctx = _make_callback(f"{FUTURES_RETRO_PREFIX}{tx.id}")
    result = await _select_tx(update, ctx)
    assert result == THESIS
    assert ctx.user_data["fut_retro_tx"]["id"] == tx.id


# ── 회고 저장 — 전체 5단계 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_retro_flow_marks_tx_retrospective_id():
    tx = _seed_close_tx()

    # 1) 선택
    sel = _make_callback(f"{FUTURES_RETRO_PREFIX}{tx.id}")[0]
    ctx = sel.callback_query.message  # placeholder unused
    sel_update, ctx = _make_callback(f"{FUTURES_RETRO_PREFIX}{tx.id}")
    await _select_tx(sel_update, ctx)

    # 2) thesis_correct
    eval_update = _make_callback(THESIS_CORRECT)[0]
    await _thesis_eval(eval_update, ctx)

    # 3) what_went_well
    well_update = _make_update("타이밍 좋았음")[0]
    await _well(well_update, ctx)

    # 4) regrets /skip
    skip_update = _make_update("/skip")[0]
    await _regrets_skip(skip_update, ctx)

    # 5) avoidable
    av_update = _make_callback(AVOIDABLE_YES)[0]
    await _avoidable(av_update, ctx)

    # 6) lessons /skip → save
    save_update = _make_update("/skip")[0]
    result = await _lessons_skip(save_update, ctx)
    assert result == ConversationHandler.END

    retros = load_retrospectives()
    assert len(retros) == 1
    assert retros[0]["is_futures"] is True
    assert retros[0]["thesis_correct"] is True
    assert retros[0]["what_went_well"] == "타이밍 좋았음"
    assert retros[0]["avoidable"] == "피할 수 있었다"

    # Tx에 retrospective_id 연결
    txs = load_futures_transactions()
    assert txs[0]["retrospective_id"] == retros[0]["id"]


# ── HTML 선물 섹션 ──────────────────────────────────────────────────────


def test_build_futures_section_empty():
    assert build_futures_section([]) == ""


def test_build_futures_section_with_prices_long_profit():
    pos = FuturesPosition(
        name="삼성전자",
        symbol="005930",
        contract_code="",
        contract_month="202606",
        expiry_date="2026-06-11",
        direction="long",
        contracts=2,
        avg_entry_price=70000.0,
        initial_margin=2520000.0,
    )
    section = build_futures_section([pos.to_dict()], current_prices={"005930": 72000.0})
    assert "삼성전자" in section
    # 미실현 = (72000-70000) * 2 * 10 = 40000
    assert "40,000원" in section
    assert "롱" in section


def test_build_futures_section_short_loss_class():
    pos = FuturesPosition(
        name="삼성전자",
        symbol="005930",
        contract_code="",
        contract_month="202606",
        expiry_date="2026-06-11",
        direction="short",
        contracts=2,
        avg_entry_price=70000.0,
        initial_margin=2520000.0,
    )
    section = build_futures_section([pos.to_dict()], current_prices={"005930": 72000.0})
    # 숏인데 가격이 오름 → 손실
    assert "loss" in section


def test_build_html_report_includes_futures_section():
    pos = FuturesPosition(
        name="삼성전자",
        symbol="005930",
        contract_code="",
        contract_month="202606",
        expiry_date="2026-06-11",
        direction="long",
        contracts=2,
        avg_entry_price=70000.0,
        initial_margin=2520000.0,
    )
    holdings = [{
        "name": "SK하이닉스",
        "ticker": "000660.KS",
        "sector": "반도체",
        "buy_date": "2026-01-01",
        "avg_price": 100000,
        "quantity": 5,
        "total_invested": 500000,
        "buy_thesis": "HBM",
        "id": "h1",
        "transaction_ids": [],
    }]
    buf = build_html_report(
        holdings,
        futures_positions=[pos.to_dict()],
        futures_prices={"005930": 72000.0},
    )
    html = buf.getvalue().decode("utf-8")
    assert "선물 포지션" in html
    assert "삼성전자" in html  # 선물쪽
    assert "SK하이닉스" in html  # 현물쪽
