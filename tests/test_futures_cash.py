"""선물 매매 시 futures_cash(가용예수금) 자동 차감/가산 + account merge 헬퍼 테스트.

근본원인: 기존엔 선물 진입/청산이 account.futures_cash 를 전혀 갱신하지 않아
증거금을 사도 가용현금이 안 줄었다. 이 테스트가 그 회귀를 막는다.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ConversationHandler

from bot.handlers.futures_buy import (
    SECTOR as ENTRY_SECTOR,
    _receive_body as _entry_receive_body,
    _receive_sector as _entry_receive_sector,
)
from bot.handlers.futures_sell import (
    _receive_body as _close_receive_body,
    _select_position as _close_select,
)
from bot.keyboards import FUTURES_POS_PREFIX
from models.futures_position import FuturesPosition
from storage.json_store import (
    adjust_futures_cash,
    load_account,
    save_account,
    save_futures_positions,
    update_account,
)


# ── update_account / adjust_futures_cash 단위 ────────────────────────────

def test_update_account_merges_preserving_other_keys():
    save_account({"initial_capital": 155e6, "cash": 10e6,
                  "futures_cash": 5e6, "chat_id": 7})
    update_account(cash=8e6)
    acc = load_account()
    assert acc["cash"] == 8e6
    assert acc["futures_cash"] == 5e6   # 보존돼야 함 (예전 버그: 통째 덮어써 삭제)
    assert acc["chat_id"] == 7
    assert acc["initial_capital"] == 155e6


def test_update_account_on_empty_creates_keys():
    update_account(initial_capital=100, cash=50)
    assert load_account() == {"initial_capital": 100, "cash": 50}


def test_adjust_futures_cash_subtracts_then_adds():
    save_account({"futures_cash": 10_000_000})
    adjust_futures_cash(-3_000_000)
    assert load_account()["futures_cash"] == 7_000_000
    adjust_futures_cash(1_500_000)
    assert load_account()["futures_cash"] == 8_500_000


def test_adjust_futures_cash_noop_when_key_absent():
    save_account({"initial_capital": 100})  # futures_cash 키 없음 → 추적 미사용
    adjust_futures_cash(-5000)
    assert "futures_cash" not in load_account()


def test_adjust_futures_cash_works_when_zero():
    save_account({"futures_cash": 0})
    adjust_futures_cash(2000)
    assert load_account()["futures_cash"] == 2000


# ── 핸들러 통합: 진입 시 증거금 차감 / 청산 시 환급+손익 가산 ──────────────

def _make_update(text: str = ""):
    u = MagicMock()
    u.message = MagicMock()
    u.message.text = text
    u.message.reply_text = AsyncMock()
    c = MagicMock()
    c.user_data = {}
    return u, c


def _make_callback(data: str):
    u = MagicMock()
    u.callback_query = MagicMock()
    u.callback_query.data = data
    u.callback_query.answer = AsyncMock()
    u.callback_query.edit_message_text = AsyncMock()
    u.callback_query.message = MagicMock()
    u.callback_query.message.reply_text = AsyncMock()
    c = MagicMock()
    c.user_data = {}
    return u, c


@pytest.mark.asyncio
async def test_futures_open_deducts_margin_from_futures_cash():
    save_account({"initial_capital": 155e6, "cash": 16e6, "futures_cash": 10_000_000})
    u, ctx = _make_update("2\n70000\n2520000\nHBM 수요")
    ctx.user_data["fut_entry"] = {
        "name": "삼성전자", "symbol": "005930", "direction": "long",
        "contract_month": "202606", "expiry_date": "2026-06-11",
    }
    assert await _entry_receive_body(u, ctx) == ENTRY_SECTOR
    s_update, _ = _make_update("반도체")
    assert await _entry_receive_sector(s_update, ctx) == ConversationHandler.END
    # 증거금 2,520,000 만큼 선물 가용예수금 차감
    assert load_account()["futures_cash"] == 10_000_000 - 2_520_000


@pytest.mark.asyncio
async def test_futures_close_adds_release_plus_pnl():
    save_account({"initial_capital": 155e6, "futures_cash": 5_000_000})
    pos = FuturesPosition(
        name="삼성전자", symbol="005930", contract_code="",
        contract_month="202606", expiry_date="2026-06-11", direction="long",
        contracts=2, avg_entry_price=70000.0, initial_margin=2_520_000.0,
        thesis="t",
    )
    save_futures_positions([pos.to_dict()])

    sel_update, ctx = _make_callback(f"{FUTURES_POS_PREFIX}{pos.id}")
    await _close_select(sel_update, ctx)
    body_update, _ = _make_update("2\n72000\n목표가")
    # _make_update 는 (update, ctx) 를 주지만 ctx 는 위 select 의 것을 써야 함
    assert await _close_receive_body(body_update, ctx) == ConversationHandler.END

    # 전량청산: 환급증거금 2,520,000 + 실현손익 (72000-70000)*2*10=40,000
    assert load_account()["futures_cash"] == 5_000_000 + 2_520_000 + 40_000


@pytest.mark.asyncio
async def test_futures_open_noop_when_no_futures_cash_tracked():
    """futures_cash 미설정 계좌면 진입해도 account 에 0 버킷을 만들지 않는다."""
    save_account({"initial_capital": 155e6, "cash": 16e6})
    u, ctx = _make_update("2\n70000\n2520000\nHBM")
    ctx.user_data["fut_entry"] = {
        "name": "삼성전자", "symbol": "005930", "direction": "long",
        "contract_month": "202606", "expiry_date": "2026-06-11",
    }
    await _entry_receive_body(u, ctx)
    s_update, _ = _make_update("반도체")
    await _entry_receive_sector(s_update, ctx)
    assert "futures_cash" not in load_account()
