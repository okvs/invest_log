"""선물 핸들러 단위 테스트 — 텔레그램 연결 없이 로직만 검증."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram.ext import ConversationHandler

from bot.handlers.futures_buy import (
    BODY as ENTRY_BODY,
    SECTOR as ENTRY_SECTOR,
    DIRECTION as ENTRY_DIRECTION,
    MONTH as ENTRY_MONTH,
    NAME as ENTRY_NAME,
    REASON as ENTRY_REASON,
    _pick_direction,
    _pick_month,
    _receive_body as _entry_receive_body,
    _receive_name,
    _receive_sector as _entry_receive_sector,
    _reason_pick as _entry_reason_pick,
    _reason_text as _entry_reason_text,
    _start as _entry_start,
)
from bot.handlers.futures_sell import (
    BODY as CLOSE_BODY,
    REASON as CLOSE_REASON,
    SELECT as CLOSE_SELECT,
    _receive_body as _close_receive_body,
    _reason_text as _close_reason_text,
    _select_position as _close_select,
    _start as _close_start,
)
from bot.handlers.futures_roll import (
    BODY as ROLL_BODY,
    MONTH as ROLL_MONTH,
    SELECT as ROLL_SELECT,
    REASON as ROLL_REASON,
    _pick_month as _roll_pick_month,
    _receive_body as _roll_receive_body,
    _reason_text as _roll_reason_text,
    _select_position as _roll_select,
    _start as _roll_start,
)
from bot.keyboards import (
    FUTURES_LONG,
    FUTURES_MONTH_PREFIX,
    FUTURES_POS_PREFIX,
    FUTURES_SHORT,
    REASON_PICK_PREFIX,
)
from models.futures_position import FuturesPosition
from parsers.expiry import upcoming_quarterly_months
from storage.json_store import (
    load_futures_positions,
    load_futures_transactions,
    save_futures_positions,
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


def _seed_position(
    name="삼성전자", contracts=2, direction="long",
    contract_month="202606", avg=70000.0, margin=2520000.0,
) -> FuturesPosition:
    pos = FuturesPosition(
        name=name,
        symbol="005930",
        contract_code="",
        contract_month=contract_month,
        expiry_date="2026-06-11",
        direction=direction,
        contracts=contracts,
        avg_entry_price=avg,
        initial_margin=margin,
        thesis="테스트 진입사유",
    )
    save_futures_positions([pos.to_dict()])
    return pos


# ─── 진입 (futures_buy) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entry_start_asks_name():
    update, ctx = _make_update()
    result = await _entry_start(update, ctx)
    assert result == ENTRY_NAME
    assert "기초자산" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_entry_receive_name_no_ticker_proceeds():
    """ticker 없을 때도 방향 선택으로 진행."""
    update, ctx = _make_update("삼성전자")
    # search_stocks 가짜 (subprocess 호출 우회)
    from unittest.mock import patch
    with patch("bot.handlers.futures_buy.search_stocks", return_value=[]):
        result = await _receive_name(update, ctx)
    assert result == ENTRY_DIRECTION
    assert ctx.user_data["fut_entry"]["name"] == "삼성전자"


@pytest.mark.asyncio
async def test_entry_pick_direction_then_month():
    update, ctx = _make_callback(FUTURES_LONG)
    ctx.user_data["fut_entry"] = {"name": "삼성전자", "symbol": "005930", "ticker": ""}
    result = await _pick_direction(update, ctx)
    assert result == ENTRY_MONTH
    assert ctx.user_data["fut_entry"]["direction"] == "long"
    assert "fut_months" in ctx.user_data


@pytest.mark.asyncio
async def test_entry_pick_month_then_body():
    months = upcoming_quarterly_months(count=4)
    cm = months[0].contract_month
    update, ctx = _make_callback(f"{FUTURES_MONTH_PREFIX}{cm}")
    ctx.user_data["fut_entry"] = {
        "name": "삼성전자", "symbol": "005930", "direction": "long",
    }
    ctx.user_data["fut_months"] = {m.contract_month: m for m in months}
    result = await _pick_month(update, ctx)
    assert result == ENTRY_BODY
    assert ctx.user_data["fut_entry"]["contract_month"] == cm


@pytest.mark.asyncio
async def test_entry_body_with_reason_saves_after_sector():
    update, ctx = _make_update("2\n70000\n2520000\nHBM 수요")
    ctx.user_data["fut_entry"] = {
        "name": "삼성전자", "symbol": "005930", "direction": "long",
        "contract_month": "202606", "expiry_date": "2026-06-11",
    }
    result = await _entry_receive_body(update, ctx)
    # 본문에 사유가 있어도 섹터 단계는 거친다
    assert result == ENTRY_SECTOR

    s_update, _ = _make_update("반도체")
    result = await _entry_receive_sector(s_update, ctx)
    assert result == ConversationHandler.END

    positions = load_futures_positions()
    assert len(positions) == 1
    assert positions[0]["contracts"] == 2
    assert positions[0]["avg_entry_price"] == 70000.0
    assert positions[0]["direction"] == "long"
    assert positions[0]["thesis"] == "HBM 수요"
    assert positions[0]["sector"] == "반도체"

    txs = load_futures_transactions()
    assert len(txs) == 1
    assert txs[0]["type"] == "open"
    assert txs[0]["thesis"] == "HBM 수요"


@pytest.mark.asyncio
async def test_entry_body_without_reason_goes_through_sector_then_reason():
    update, ctx = _make_update("2\n70000\n2520000")
    ctx.user_data["fut_entry"] = {
        "name": "삼성전자", "symbol": "005930", "direction": "long",
        "contract_month": "202606", "expiry_date": "2026-06-11",
    }
    result = await _entry_receive_body(update, ctx)
    assert result == ENTRY_SECTOR

    s_update, _ = _make_update("반도체")
    result = await _entry_receive_sector(s_update, ctx)
    assert result == ENTRY_REASON


@pytest.mark.asyncio
async def test_entry_additional_entry_recomputes_average():
    """같은 종목·방향·결제월이면 추가 진입으로 평균진입가 재계산."""
    _seed_position(contracts=2, avg=70000.0, margin=2520000.0)

    update, ctx = _make_update("2\n72000\n2592000\nHBM 추가")
    ctx.user_data["fut_entry"] = {
        "name": "삼성전자", "symbol": "005930", "direction": "long",
        "contract_month": "202606", "expiry_date": "2026-06-11",
    }
    await _entry_receive_body(update, ctx)
    s_update, _ = _make_update(".")  # 기존 섹터 유지
    result = await _entry_receive_sector(s_update, ctx)
    assert result == ConversationHandler.END

    positions = load_futures_positions()
    assert len(positions) == 1
    assert positions[0]["contracts"] == 4
    assert positions[0]["avg_entry_price"] == 71000.0


@pytest.mark.asyncio
async def test_entry_short_direction_saves():
    update, ctx = _make_update("2\n70000\n2520000\n숏 헷지")
    ctx.user_data["fut_entry"] = {
        "name": "삼성전자", "symbol": "005930", "direction": "short",
        "contract_month": "202606", "expiry_date": "2026-06-11",
    }
    await _entry_receive_body(update, ctx)
    s_update, _ = _make_update("반도체")
    await _entry_receive_sector(s_update, ctx)
    positions = load_futures_positions()
    assert positions[0]["direction"] == "short"


# ─── 청산 (futures_sell) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_empty_ends_early():
    save_futures_positions([])
    update, ctx = _make_update()
    result = await _close_start(update, ctx)
    assert result == ConversationHandler.END


@pytest.mark.asyncio
async def test_close_full_long_profit():
    pos = _seed_position(contracts=2, direction="long", avg=70000.0)
    # 종목 선택
    sel_update, ctx = _make_callback(f"{FUTURES_POS_PREFIX}{pos.id}")
    await _close_select(sel_update, ctx)
    assert ctx.user_data["fut_close"]["position_id"] == pos.id

    body_update = _make_update("2\n72000\n목표가 도달")[0]
    result = await _close_receive_body(body_update, ctx)
    assert result == ConversationHandler.END

    positions = load_futures_positions()
    assert len(positions) == 0  # 전량 청산이면 제거
    txs = load_futures_transactions()
    closes = [t for t in txs if t["type"] == "close"]
    assert len(closes) == 1
    # 롱: (72000-70000) * 2 * 10 = 40000
    assert closes[0]["pnl"] == 40000.0


@pytest.mark.asyncio
async def test_close_partial_keeps_position():
    pos = _seed_position(contracts=3, direction="long", avg=70000.0)
    sel_update, ctx = _make_callback(f"{FUTURES_POS_PREFIX}{pos.id}")
    await _close_select(sel_update, ctx)

    body_update = _make_update("1\n72000\n부분 청산")[0]
    await _close_receive_body(body_update, ctx)

    positions = load_futures_positions()
    assert len(positions) == 1
    assert positions[0]["contracts"] == 2


@pytest.mark.asyncio
async def test_close_short_profit():
    pos = _seed_position(contracts=2, direction="short", avg=70000.0)
    sel_update, ctx = _make_callback(f"{FUTURES_POS_PREFIX}{pos.id}")
    await _close_select(sel_update, ctx)

    body_update = _make_update("2\n68000\n숏 익절")[0]
    await _close_receive_body(body_update, ctx)

    txs = load_futures_transactions()
    closes = [t for t in txs if t["type"] == "close"]
    # 숏: (68000-70000) * 2 * 10 * -1 = 40000
    assert closes[0]["pnl"] == 40000.0


@pytest.mark.asyncio
async def test_close_exceeds_contracts_blocked():
    pos = _seed_position(contracts=1)
    sel_update, ctx = _make_callback(f"{FUTURES_POS_PREFIX}{pos.id}")
    await _close_select(sel_update, ctx)
    body_update = _make_update("2\n72000\n초과")[0]
    result = await _close_receive_body(body_update, ctx)
    assert result == CLOSE_BODY
    assert "초과" in body_update.message.reply_text.call_args[0][0]


# ─── 롤오버 (futures_roll) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_roll_flow_full():
    pos = _seed_position(contracts=2, contract_month="202606")

    # 포지션 선택
    sel_update, ctx = _make_callback(f"{FUTURES_POS_PREFIX}{pos.id}")
    await _roll_select(sel_update, ctx)
    assert ctx.user_data["fut_roll"]["position_id"] == pos.id

    # 차월물 선택 (202609)
    months_map = ctx.user_data["fut_roll_months"]
    next_cm = next(iter(months_map))  # 가까운 차월물 = 202609 또는 202612
    month_update = _make_callback(f"{FUTURES_MONTH_PREFIX}{next_cm}")[0]
    await _roll_pick_month(month_update, ctx)
    assert ctx.user_data["fut_roll"]["new_contract_month"] == next_cm

    # 본문 입력: close 72000, open 71500, 증거금 추가 0
    body_update = _make_update("72000\n71500\n0\n롤오버 사유")[0]
    result = await _roll_receive_body(body_update, ctx)
    assert result == ConversationHandler.END

    # 포지션은 차월물로 옮겨졌고 당월물 6월은 없음
    positions = load_futures_positions()
    assert len(positions) == 1
    assert positions[0]["contract_month"] == next_cm
    assert positions[0]["contracts"] == 2

    txs = load_futures_transactions()
    types = [t["type"] for t in txs]
    assert "roll_close" in types
    assert "roll_open" in types

    close_tx = next(t for t in txs if t["type"] == "roll_close")
    open_tx = next(t for t in txs if t["type"] == "roll_open")
    assert close_tx["linked_tx_id"] == open_tx["id"]
    assert open_tx["linked_tx_id"] == close_tx["id"]
    # 롱: (72000-70000) * 2 * 10 = 40000
    assert close_tx["pnl"] == 40000.0
