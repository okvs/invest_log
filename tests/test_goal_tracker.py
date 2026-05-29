"""순자산 10억 트래커 순수 계산 유닛 테스트."""
from __future__ import annotations

import math

import pytest

from bot.goal_tracker import (
    GOAL_KRW,
    annual_to_monthly,
    compute_margin_call,
    drawdown_from_peak,
    margin_call_move,
    realized_cagr,
    required_cagr,
    trajectory_value,
    years_to_goal,
)


# ── required_cagr ──────────────────────────────────────────────────────────

def test_required_cagr_double_in_one_year():
    assert required_cagr(5e8, 1e9, 1) == pytest.approx(1.0)


def test_required_cagr_quadruple_in_two_years():
    # 4배를 2년 → 매년 2배 (+100%)
    assert required_cagr(2.5e8, 1e9, 2) == pytest.approx(1.0)


def test_required_cagr_known_28pct_for_5y():
    # 2.85억 → 10억 5년: (10/2.85)^(1/5)-1 ≈ 28.5%
    r = required_cagr(2.85e8, 1e9, 5)
    assert r == pytest.approx(0.285, abs=0.01)


def test_required_cagr_invalid_returns_inf():
    assert required_cagr(0, 1e9, 2) == math.inf
    assert required_cagr(2e8, 1e9, 0) == math.inf


# ── annual_to_monthly ──────────────────────────────────────────────────────

def test_annual_to_monthly_compounds_back():
    monthly = annual_to_monthly(1.0)  # +100%/년
    assert (1 + monthly) ** 12 == pytest.approx(2.0)


def test_annual_to_monthly_passthrough_inf():
    assert annual_to_monthly(math.inf) == math.inf


# ── realized_cagr ──────────────────────────────────────────────────────────

def test_realized_cagr_one_year_doubling():
    assert realized_cagr(1e8, 2e8, 365.25) == pytest.approx(1.0, abs=1e-6)


def test_realized_cagr_short_sample_annualizes_high():
    # 57일 +83.7% → 연환산은 매우 큼 (표본 짧음의 함정 확인)
    r = realized_cagr(1.55e8, 2.85e8, 57)
    assert r is not None and r > 5.0


def test_realized_cagr_guards():
    assert realized_cagr(0, 1e8, 100) is None
    assert realized_cagr(1e8, 1e8, 0) is None


# ── years_to_goal ──────────────────────────────────────────────────────────

def test_years_to_goal_consistent_with_required_cagr():
    # 2.85억에서 연 28.5%면 10억까지 약 5년
    r = required_cagr(2.85e8, 1e9, 5)
    assert years_to_goal(2.85e8, 1e9, r) == pytest.approx(5.0, abs=0.05)


def test_years_to_goal_already_reached():
    assert years_to_goal(1.2e9, 1e9, 0.3) == 0.0


def test_years_to_goal_nonpositive_rate_none():
    assert years_to_goal(2e8, 1e9, 0.0) is None
    assert years_to_goal(2e8, 1e9, -0.1) is None


# ── trajectory_value ───────────────────────────────────────────────────────

def test_trajectory_endpoints():
    # 시작점은 start_nav, 종점은 goal
    assert trajectory_value(2.85e8, 1e9, 5, 0) == pytest.approx(2.85e8)
    assert trajectory_value(2.85e8, 1e9, 5, 5) == pytest.approx(1e9)


def test_trajectory_midpoint_is_geometric_mean():
    mid = trajectory_value(1e8, 1e9, 2, 1)
    assert mid == pytest.approx(math.sqrt(1e8 * 1e9))


# ── drawdown_from_peak ─────────────────────────────────────────────────────

def test_drawdown_at_peak_is_zero():
    peak, dd = drawdown_from_peak([1.0, 2.0, 3.0])
    assert peak == 3.0 and dd == pytest.approx(0.0)


def test_drawdown_off_peak():
    peak, dd = drawdown_from_peak([1.0, 4.0, 3.0])
    assert peak == 4.0 and dd == pytest.approx(-0.25)


def test_drawdown_empty():
    assert drawdown_from_peak([]) == (0.0, 0.0)


# ── margin_call_move ───────────────────────────────────────────────────────

def test_margin_call_move_long():
    # 순롱: 순자산 100, 유지 70, 명목 200 → 15% 하락 시 마진콜
    assert margin_call_move(100, 70, 200) == pytest.approx(0.15)


def test_margin_call_move_zero_notional_none():
    assert margin_call_move(100, 70, 0) is None


# ── compute_margin_call ────────────────────────────────────────────────────

def _pos(symbol, cm, direction, contracts, avg, margin, mult=10):
    return {
        "symbol": symbol, "contract_month": cm, "direction": direction,
        "contracts": contracts, "avg_entry_price": avg,
        "initial_margin": margin, "multiplier": mult,
    }


def test_compute_margin_call_basic_long():
    positions = [_pos("005930", "202607", "long", 10, 295750, 10647000)]
    prices = {"005930|202607": {"price": 295750}}
    mc = compute_margin_call(positions, prices, futures_cash=0, maintenance_ratio=0.7444)
    assert mc is not None
    # 미실현 0 (현재가=진입가) → equity = margin
    assert mc["unrealized"] == pytest.approx(0.0)
    assert mc["equity_now"] == pytest.approx(10647000)
    assert mc["maint_total"] == pytest.approx(10647000 * 0.7444)
    # signed_notional = 295750*10*10
    assert mc["signed_notional"] == pytest.approx(295750 * 100)
    assert mc["net_long"] is True
    assert mc["priced"] == 1


def test_compute_margin_call_unpriced_excluded():
    positions = [_pos("005930", "202607", "long", 10, 295750, 10647000)]
    mc = compute_margin_call(positions, {}, futures_cash=0)
    assert mc["priced"] == 0
    assert mc["signed_notional"] == 0.0
    assert mc["x_call"] is None  # notional 0 → 트리거 없음


def test_compute_margin_call_no_positions_none():
    assert compute_margin_call([], {}, 0) is None
    assert compute_margin_call([_pos("005930", "202607", "long", 0, 1, 1)], {}, 0) is None


def test_goal_constant():
    assert GOAL_KRW == 1_000_000_000
