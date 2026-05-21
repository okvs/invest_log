"""선물 모델/저장소/만기 계산 유닛 테스트."""
from __future__ import annotations

from datetime import date

import pytest

from models.futures_position import FuturesPosition, DEFAULT_MULTIPLIER
from models.futures_transaction import FuturesTransaction
from parsers.expiry import (
    parse_contract_month,
    second_thursday,
    upcoming_quarterly_months,
)
from storage.json_store import (
    get_recent_futures_reasons,
    load_futures_positions,
    load_futures_transactions,
    save_futures_positions,
    save_futures_transactions,
)


# ── 만기 계산 ──────────────────────────────────────────────────────────────


def test_second_thursday_known_dates():
    # 2026년 6월의 두 번째 목요일은 11일
    assert second_thursday(2026, 6) == date(2026, 6, 11)
    # 2026년 3월의 두 번째 목요일은 12일
    assert second_thursday(2026, 3) == date(2026, 3, 12)
    # 2026년 12월의 두 번째 목요일은 10일
    assert second_thursday(2026, 12) == date(2026, 12, 10)


def test_upcoming_quarterly_returns_only_future():
    today = date(2026, 5, 21)
    months = upcoming_quarterly_months(today=today, count=4)
    # 모든 만기일이 today 이후
    for m in months:
        assert m.expiry_date >= today
    # 첫 만기는 2026-06
    assert months[0].contract_month == "202606"
    assert months[1].contract_month == "202609"
    assert months[2].contract_month == "202612"
    assert months[3].contract_month == "202703"


def test_parse_contract_month_accepts_separators():
    fm = parse_contract_month("2026-06")
    assert fm.contract_month == "202606"
    assert fm.expiry_date == date(2026, 6, 11)


def test_parse_contract_month_rejects_non_quarterly():
    with pytest.raises(ValueError):
        parse_contract_month("202605")  # 5월은 분기물 아님


# ── FuturesPosition 동작 ─────────────────────────────────────────────────


def _make_long_position(contracts=3, entry=70000.0) -> FuturesPosition:
    return FuturesPosition(
        name="삼성전자",
        symbol="005930",
        contract_code="1AB6000",
        contract_month="202606",
        expiry_date="2026-06-11",
        direction="long",
        contracts=contracts,
        avg_entry_price=entry,
        initial_margin=entry * contracts * DEFAULT_MULTIPLIER * 0.18,
        thesis="HBM 수요 증가",
    )


def test_add_entry_recomputes_average():
    pos = _make_long_position(contracts=2, entry=70000.0)
    pos.add_entry(price=72000.0, contracts=2, margin=72000 * 2 * 10 * 0.18, transaction_id="tx-2")
    assert pos.contracts == 4
    # 평균 = (70000*2 + 72000*2)/4 = 71000
    assert pos.avg_entry_price == 71000.0
    assert "tx-2" in pos.transaction_ids


def test_close_long_profit():
    pos = _make_long_position(contracts=3, entry=70000.0)
    pnl, margin_release, closed = pos.close(price=72000.0, contracts=2)
    # 롱: (72000-70000) * 2 * 10 = 40000
    assert pnl == 40000.0
    assert closed == 2.0
    assert pos.contracts == 1
    # 증거금은 비례 환급 (2/3)
    assert margin_release > 0


def test_close_short_profit():
    pos = _make_long_position(contracts=2, entry=70000.0)
    pos.direction = "short"
    pnl, _, _ = pos.close(price=68000.0, contracts=2)
    # 숏: (68000-70000) * 2 * 10 * -1 = 40000
    assert pnl == 40000.0
    assert pos.contracts == 0


def test_close_more_than_held_raises():
    pos = _make_long_position(contracts=1, entry=70000.0)
    with pytest.raises(ValueError):
        pos.close(price=72000, contracts=2)


def test_unrealized_pnl_long():
    pos = _make_long_position(contracts=2, entry=70000.0)
    # (72000-70000) * 2 * 10 = 40000
    assert pos.unrealized_pnl(72000.0) == 40000.0


def test_unrealized_pnl_short():
    pos = _make_long_position(contracts=2, entry=70000.0)
    pos.direction = "short"
    # (68000-70000) * 2 * 10 * -1 = 40000
    assert pos.unrealized_pnl(68000.0) == 40000.0


def test_position_dict_roundtrip():
    pos = _make_long_position()
    restored = FuturesPosition.from_dict(pos.to_dict())
    assert restored.to_dict() == pos.to_dict()


# ── FuturesTransaction ───────────────────────────────────────────────────


def test_tx_dict_roundtrip_open():
    tx = FuturesTransaction(
        type="open",
        name="삼성전자",
        symbol="005930",
        contract_code="1AB6000",
        contract_month="202606",
        expiry_date="2026-06-11",
        direction="long",
        contracts=2,
        price=70000.0,
        margin=2520000.0,
        thesis="HBM",
        position_id="pos-1",
    )
    restored = FuturesTransaction.from_dict(tx.to_dict())
    assert restored.to_dict() == tx.to_dict()
    # open에는 pnl/buy_thesis 키가 없어야 한다 (close 전용)
    assert "pnl" not in tx.to_dict()
    assert "buy_thesis" not in tx.to_dict()


def test_tx_dict_roundtrip_close_has_pnl_keys():
    tx = FuturesTransaction(
        type="close",
        name="삼성전자",
        symbol="005930",
        contract_code="1AB6000",
        contract_month="202606",
        expiry_date="2026-06-11",
        direction="long",
        contracts=2,
        price=72000.0,
        margin=2520000.0,
        pnl=40000.0,
        pnl_pct=2.86,
        buy_thesis="HBM",
        position_id="pos-1",
    )
    d = tx.to_dict()
    assert d["pnl"] == 40000.0
    assert d["buy_thesis"] == "HBM"
    restored = FuturesTransaction.from_dict(d)
    assert restored.pnl == 40000.0


# ── 저장소 / 최근 사유 ───────────────────────────────────────────────────


def test_futures_positions_persist():
    pos = _make_long_position()
    save_futures_positions([pos.to_dict()])
    loaded = load_futures_positions()
    assert len(loaded) == 1
    assert loaded[0]["name"] == "삼성전자"


def test_recent_futures_reasons_open_and_close():
    save_futures_transactions([
        {
            "id": "1", "type": "open", "name": "삼성전자",
            "date": "2026-05-01T10:00:00", "thesis": "HBM 수요",
        },
        {
            "id": "2", "type": "open", "name": "SK하이닉스",
            "date": "2026-05-10T10:00:00", "thesis": "HBM 수요",  # 중복 사유
        },
        {
            "id": "3", "type": "open", "name": "삼성전자",
            "date": "2026-05-15T10:00:00", "thesis": "HBM 수요 강화",
        },
        {
            "id": "4", "type": "close", "name": "삼성전자",
            "date": "2026-05-20T10:00:00", "reason": "목표가 도달",
        },
        {
            "id": "5", "type": "roll_close", "name": "삼성전자",
            "date": "2026-05-19T10:00:00", "reason": "롤오버: 6월물 청산",
        },
    ])

    open_reasons = get_recent_futures_reasons("open")
    # 최신순, 중복 사유 1번만
    assert open_reasons[0] == "HBM 수요 강화"
    assert open_reasons[1] == "HBM 수요"
    assert len(open_reasons) == 2

    close_reasons = get_recent_futures_reasons("close", pinned=["자동손절"])
    assert close_reasons[0] == "자동손절"
    assert "목표가 도달" in close_reasons
    assert "롤오버: 6월물 청산" in close_reasons
