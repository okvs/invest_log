"""KB증권 선물옵션 체결 메시지 파싱 + broker.py 자동 분기 테스트."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ConversationHandler

from bot.handlers.broker import (
    FUT_CLOSE_REASON,
    FUT_MARGIN,
    FUT_SECTOR,
    FUT_THESIS,
    FUT_THESIS_APPEND,
    FUT_THESIS_CONFIRM,
    _fut_close_reason_input,
    _fut_margin_input,
    _fut_margin_rate_pick,
    _fut_sector_input,
    _fut_thesis_append_input,
    _fut_thesis_existing_confirm,
    _fut_thesis_input,
    _receive_broker_msg,
)
from bot.keyboards import (
    APPEND_THESIS,
    EDIT_THESIS,
    FUT_MARGIN_CUSTOM,
    FUT_MARGIN_RATE_PREFIX,
    KEEP_EXISTING,
)
from storage.json_store import load_margin_rate_pool
from models.futures_position import FuturesPosition
from parsers.input_parser import (
    FuturesBrokerMessage,
    _is_kb_futures,
    parse_broker_message,
)
from storage.json_store import (
    load_futures_positions,
    load_futures_transactions,
    save_futures_positions,
)


SAMPLE = """[KB증권] 선물옵션 체결 안내

고객님, 주문하신 현대모비스 F 202606 (  10) 선물옵션이 체결됐으니 확인해주세요.

■ 계좌: ***-***-*28 [01]
■ 종목명: 현대모비스 F 202606 (  10)
■ 주문수량: 2계약
■ 체결금액: 676,000원
■ 내용: 매수체결(15218)
"""


def _make_update(text: str = ""):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    context = MagicMock()
    context.user_data = {}
    context.application = MagicMock()
    context.application.handlers = {}
    return update, context


# ── 파서 ─────────────────────────────────────────────────────────────────


def test_is_kb_futures_recognises_header():
    assert _is_kb_futures(SAMPLE)


def test_is_kb_futures_rejects_cash():
    cash = """[KB증권] 체결 안내

■ 종목명: 삼성전자
■ 주문수량: 10주
■ 체결금액: 72000원
■ 내용: 매수체결
"""
    assert not _is_kb_futures(cash)


def test_parse_returns_futures_message():
    msg = parse_broker_message(SAMPLE)
    assert isinstance(msg, FuturesBrokerMessage)
    assert msg.name == "현대모비스"
    assert msg.contract_month == "202606"
    assert msg.multiplier == 10
    assert msg.quantity == 2
    assert msg.raw_amount == 676000.0
    assert msg.trade_type == "buy"
    assert msg.broker == "KB"


def test_price_per_share_is_raw_amount():
    """KB 선물 알림의 `체결금액`은 주당 단가 그대로다."""
    msg = parse_broker_message(SAMPLE)
    assert msg.price_per_share() == 676000.0
    # 총 매수대금 = 676,000 × 2계약 × 10승수 = 13,520,000원
    assert msg.total_amount() == 13_520_000.0


def test_sell_variant():
    text = SAMPLE.replace("매수체결", "매도체결")
    msg = parse_broker_message(text)
    assert msg.trade_type == "sell"


def test_invalid_kb_futures_raises():
    text = "[KB증권] 선물옵션 체결 안내\n\n■ 종목명: X\n■ 내용: 매수체결"
    with pytest.raises(ValueError):
        parse_broker_message(text)


# ── 자동 분기: 신규 진입 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buy_with_no_position_new_long_entry():
    save_futures_positions([])
    update, ctx = _make_update(SAMPLE)
    result = await _receive_broker_msg(update, ctx)
    assert result == FUT_MARGIN
    assert ctx.user_data["fut_action"] == "new"
    assert ctx.user_data["fut_direction"] == "long"
    # 단가·계약수·총대금이 그대로 노출되어 사용자가 즉시 검증 가능
    msg_text = update.message.reply_text.call_args[0][0]
    assert "676,000" in msg_text          # 단가
    assert "13,520,000" in msg_text       # 총 매수대금


@pytest.mark.asyncio
async def test_full_new_entry_flow():
    save_futures_positions([])
    update, ctx = _make_update(SAMPLE)
    await _receive_broker_msg(update, ctx)

    # 증거금률 직접 입력 → 섹터 단계 (rate 30% → margin 13,520,000 × 0.3 = 4,056,000)
    m_update = _make_update("30")[0]
    result = await _fut_margin_input(m_update, ctx)
    assert result == FUT_SECTOR

    # 섹터 입력
    s_update = _make_update("자동차")[0]
    result = await _fut_sector_input(s_update, ctx)
    assert result == FUT_THESIS

    # 사유 입력
    t_update = _make_update("HBM 수요")[0]
    result = await _fut_thesis_input(t_update, ctx)
    assert result == ConversationHandler.END

    positions = load_futures_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p["name"] == "현대모비스"
    assert p["direction"] == "long"
    assert p["contracts"] == 2
    assert p["avg_entry_price"] == 676000.0
    assert p["contract_month"] == "202606"
    assert p["expiry_date"] == "2026-06-11"  # 두 번째 목요일
    assert p["multiplier"] == 10
    assert p["initial_margin"] == 4_056_000.0
    assert p["thesis"] == "HBM 수요"
    assert p["sector"] == "자동차"

    txs = load_futures_transactions()
    assert any(t["type"] == "open" and t["thesis"] == "HBM 수요" for t in txs)


# ── 자동 분기: 추가 진입 ────────────────────────────────────────────────


def _seed(name="현대모비스", direction="long", contracts=2, cm="202606"):
    pos = FuturesPosition(
        name=name,
        symbol="",
        contract_code="",
        contract_month=cm,
        expiry_date="2026-06-11",
        direction=direction,
        contracts=contracts,
        avg_entry_price=670000.0,
        initial_margin=2400000.0,
        thesis="원래 사유",
    )
    save_futures_positions([pos.to_dict()])
    return pos


@pytest.mark.asyncio
async def test_buy_with_existing_long_is_add():
    # 시드 포지션이 sector="자동차" 라 새 진입은 섹터를 묻지 않고 자동 매칭
    _seed(direction="long", contracts=1)
    # 시드 sector 설정
    positions = load_futures_positions()
    positions[0]["sector"] = "자동차"
    save_futures_positions(positions)

    update, ctx = _make_update(SAMPLE)
    result = await _receive_broker_msg(update, ctx)
    assert result == FUT_MARGIN
    assert ctx.user_data["fut_action"] == "add"

    # 증거금률 30% → 섹터 자동 스킵 → 기존 사유 있으면 confirm 단계
    result = await _fut_margin_input(_make_update("30")[0], ctx)
    assert result == FUT_THESIS_CONFIRM
    assert ctx.user_data["fut_existing_thesis"] == "원래 사유"

    # "사유 새로쓰기" 선택 → 자유 입력
    cb_update = _make_callback_update(EDIT_THESIS)
    result = await _fut_thesis_existing_confirm(cb_update, ctx)
    assert result == FUT_THESIS
    await _fut_thesis_input(_make_update("추가 매수")[0], ctx)

    positions = load_futures_positions()
    assert len(positions) == 1
    p = positions[0]
    # (670000*1 + 676000*2) / 3 = 674000
    assert p["contracts"] == 3
    assert round(p["avg_entry_price"], 2) == round((670000.0 + 676000.0 * 2) / 3, 2)
    # 추가 margin = 13,520,000 × 0.3 = 4,056,000
    assert p["initial_margin"] == 2400000.0 + 4_056_000.0
    assert p["sector"] == "자동차"
    assert p["thesis"] == "추가 매수"


# ── 자동 분기: 추가 진입 + 기존 사유 옵션 ───────────────────────────────


def _make_callback_update(data: str):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.message = None
    return update


@pytest.mark.asyncio
async def test_buy_add_existing_thesis_keep():
    """추가 진입 시 '그대로 유지' 누르면 기존 사유 그대로 저장."""
    _seed(direction="long", contracts=1)
    positions = load_futures_positions()
    positions[0]["sector"] = "자동차"
    save_futures_positions(positions)

    update, ctx = _make_update(SAMPLE)
    await _receive_broker_msg(update, ctx)
    result = await _fut_margin_input(_make_update("30")[0], ctx)
    assert result == FUT_THESIS_CONFIRM

    # 그대로 유지
    cb = _make_callback_update(KEEP_EXISTING)
    result = await _fut_thesis_existing_confirm(cb, ctx)
    assert result == ConversationHandler.END

    p = load_futures_positions()[0]
    assert p["contracts"] == 3
    assert p["thesis"] == "원래 사유"


@pytest.mark.asyncio
async def test_buy_add_existing_thesis_append():
    """추가 진입 시 '사유 이어쓰기' 누르고 텍스트 입력하면 결합되어 저장."""
    _seed(direction="long", contracts=1)
    positions = load_futures_positions()
    positions[0]["sector"] = "자동차"
    save_futures_positions(positions)

    update, ctx = _make_update(SAMPLE)
    await _receive_broker_msg(update, ctx)
    await _fut_margin_input(_make_update("30")[0], ctx)

    # 이어쓰기
    cb = _make_callback_update(APPEND_THESIS)
    result = await _fut_thesis_existing_confirm(cb, ctx)
    assert result == FUT_THESIS_APPEND
    assert ctx.user_data["fut_append_base"] == "원래 사유"

    # 추가 텍스트 입력
    extra_update = _make_update("HBM 모멘텀")[0]
    result = await _fut_thesis_append_input(extra_update, ctx)
    assert result == ConversationHandler.END

    p = load_futures_positions()[0]
    assert p["thesis"] == "원래 사유\nHBM 모멘텀"


@pytest.mark.asyncio
async def test_buy_add_no_existing_thesis_skips_confirm():
    """기존 사유가 비어있으면 confirm 단계 없이 곧장 FUT_THESIS."""
    pos = FuturesPosition(
        name="현대모비스", symbol="", contract_code="",
        contract_month="202606", expiry_date="2026-06-11",
        direction="long", contracts=1,
        avg_entry_price=670000.0, initial_margin=2400000.0,
        sector="자동차", thesis="",
    )
    save_futures_positions([pos.to_dict()])

    update, ctx = _make_update(SAMPLE)
    await _receive_broker_msg(update, ctx)
    result = await _fut_margin_input(_make_update("30")[0], ctx)
    assert result == FUT_THESIS  # confirm 건너뛰고 바로 thesis


# ── 자동 분기: 청산 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sell_with_existing_long_is_close():
    pos = _seed(direction="long", contracts=2)
    text = SAMPLE.replace("매수체결", "매도체결")
    update, ctx = _make_update(text)
    result = await _receive_broker_msg(update, ctx)
    assert result == FUT_CLOSE_REASON
    assert ctx.user_data["fut_close_pos_id"] == pos.id
    assert ctx.user_data["fut_close_contracts"] == 2
    assert ctx.user_data["fut_close_price"] == 676000.0

    # 사유 입력 → 청산 처리
    r_update = _make_update("목표가 도달")[0]
    result = await _fut_close_reason_input(r_update, ctx)
    assert result == ConversationHandler.END

    positions = load_futures_positions()
    assert positions == []  # 전량 청산
    txs = load_futures_transactions()
    closes = [t for t in txs if t["type"] == "close"]
    assert len(closes) == 1
    # 롱: (676000-670000) * 2 * 10 = 120,000
    assert closes[0]["pnl"] == 120000.0
    assert closes[0]["reason"] == "목표가 도달"


@pytest.mark.asyncio
async def test_buy_with_existing_short_is_close():
    """기존 숏 포지션이 있는 상태에서 매수 체결 → 숏 청산."""
    _seed(direction="short", contracts=2)
    update, ctx = _make_update(SAMPLE)
    result = await _receive_broker_msg(update, ctx)
    assert result == FUT_CLOSE_REASON
    assert ctx.user_data["fut_direction"] == "short"

    await _fut_close_reason_input(_make_update("숏 청산")[0], ctx)

    txs = load_futures_transactions()
    closes = [t for t in txs if t["type"] == "close"]
    # 숏 (676000-670000)*2*10*-1 = -120,000 (가격이 오른 시점에 청산 = 손실)
    assert closes[0]["pnl"] == -120000.0


# ── 증거금률 카드 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_margin_rate_card_computes_margin_and_advances():
    """카드 클릭 → 단가×계약×승수×rate로 자동 계산 후 사유 단계."""
    save_futures_positions([])
    update, ctx = _make_update(SAMPLE)
    await _receive_broker_msg(update, ctx)
    # 사용자에게 키보드가 노출되어야 함
    kwargs = update.message.reply_text.call_args.kwargs
    assert kwargs.get("reply_markup") is not None

    # 32.85% 카드 클릭
    cb_update = MagicMock()
    cb_update.callback_query = MagicMock()
    cb_update.callback_query.data = f"{FUT_MARGIN_RATE_PREFIX}0.3285"
    cb_update.callback_query.answer = AsyncMock()
    cb_update.callback_query.edit_message_text = AsyncMock()
    result = await _fut_margin_rate_pick(cb_update, ctx)
    assert result == FUT_SECTOR
    # 676,000 × 2 × 10 × 0.3285 = 4,441,320
    assert ctx.user_data["fut_margin"] == 4_441_320

    # LRU 풀의 맨 앞에 0.3285 가 올라옴 (가장 최근 사용)
    pool = load_margin_rate_pool()
    assert abs(pool[0] - 0.3285) < 1e-6


@pytest.mark.asyncio
async def test_margin_rate_custom_keeps_state_for_text_input():
    """'증거금률 직접 입력 (%)' 카드 → FUT_MARGIN 유지, 다음 텍스트로 %를 입력."""
    save_futures_positions([])
    update, ctx = _make_update(SAMPLE)
    await _receive_broker_msg(update, ctx)

    cb_update = MagicMock()
    cb_update.callback_query = MagicMock()
    cb_update.callback_query.data = FUT_MARGIN_CUSTOM
    cb_update.callback_query.answer = AsyncMock()
    cb_update.callback_query.edit_message_text = AsyncMock()
    result = await _fut_margin_rate_pick(cb_update, ctx)
    assert result == FUT_MARGIN

    # rate "36.9" → 0.369 → margin = 13,520,000 × 0.369 = 4,988,880
    m_update = _make_update("36.9")[0]
    result = await _fut_margin_input(m_update, ctx)
    assert result == FUT_SECTOR
    assert ctx.user_data["fut_margin"] == 4_988_880
    assert abs(ctx.user_data["fut_margin_rate"] - 0.369) < 1e-6
    # LRU 풀에 36.9% 가 올라오고 맨 앞에 위치
    pool = load_margin_rate_pool()
    assert abs(pool[0] - 0.369) < 1e-6


@pytest.mark.asyncio
async def test_sell_with_no_position_new_short_entry():
    """포지션 없는데 매도 체결 → 신규 숏 진입."""
    save_futures_positions([])
    text = SAMPLE.replace("매수체결", "매도체결")
    update, ctx = _make_update(text)
    result = await _receive_broker_msg(update, ctx)
    assert result == FUT_MARGIN
    assert ctx.user_data["fut_action"] == "new"
    assert ctx.user_data["fut_direction"] == "short"

    await _fut_margin_input(_make_update("30")[0], ctx)
    await _fut_sector_input(_make_update("반도체")[0], ctx)
    await _fut_thesis_input(_make_update("숏 진입")[0], ctx)

    positions = load_futures_positions()
    assert positions[0]["direction"] == "short"
