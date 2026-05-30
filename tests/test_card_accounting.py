"""잔고 카드 회계 정합 테스트.

핵심 불변식(레버리지 포함 ↔ 전부청산 자산):
    총평가금(gross) − 신용차입 − 선물차입 = 포지션 순자산
    포지션 순자산 + 예수금(현+선)        = 총자산(전부청산) = compute_balance_nav

build_html_report 가 렌더한 카드 헤드라인 숫자가 compute_balance_nav 와
실제로 일치하는지 end-to-end 로 검증한다.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from bot.goal_tracker import compute_balance_nav
from bot.html_report import build_html_report


# ── 합성 데이터 (실잔고 구조 모사: 신용·선물 둘 다 존재) ──────────────────────
def _holdings():
    return [
        {"name": "삼성전자", "ticker": "005930.KS", "sector": "반도체",
         "quantity": 110, "avg_price": 286000, "total_invested": 286000 * 110,
         "credit_loan": 9444875.0},
        {"name": "SK하이닉스", "ticker": "000660.KS", "sector": "반도체",
         "quantity": 18, "avg_price": 1649885, "total_invested": 1649885 * 18,
         "credit_loan": 16333865.625},
        {"name": "제주반도체", "ticker": "080220.KQ", "sector": "반도체",
         "quantity": 150, "avg_price": 108183, "total_invested": 108183 * 150,
         "credit_loan": 0.0},
    ]


def _futures():
    return [
        {"name": "삼성전자", "symbol": "005930", "contract_month": "202607",
         "direction": "long", "contracts": 10, "multiplier": 10,
         "avg_entry_price": 296000.0, "initial_margin": 10656000, "sector": "반도체"},
        {"name": "SK하이닉스", "symbol": "000660", "contract_month": "202606",
         "direction": "long", "contracts": 6, "multiplier": 10,
         "avg_entry_price": 2143166.6666666665, "initial_margin": 47449710.0,
         "sector": "반도체"},
    ]


SPOT_PRICES = {"005930.KS": 290000, "000660.KS": 1700000, "080220.KQ": 110000}
FUT_PRICES = {"005930|202607": {"price": 300000}, "000660|202606": {"price": 2200000}}
CASH = 16492093.0
FCASH = 7660212.0
INITIAL = 155000000.0


def _render(holdings, futures, *, show_cash=True, cash=CASH, fcash=FCASH):
    """fetch_current_quotes / _resolve_tickers 를 막고 카드 HTML 을 렌더."""
    name_to_ticker = {h["name"]: h["ticker"] for h in holdings}
    spot_quotes = {h["ticker"]: {"price": SPOT_PRICES[h["ticker"]], "change_pct": 0.0}
                   for h in holdings}
    with patch("bot.html_report.fetch_current_quotes", return_value=spot_quotes), \
         patch("bot.html_report._resolve_tickers", return_value=(name_to_ticker, [])):
        buf = build_html_report(
            holdings,
            show_cash=show_cash,
            initial_capital=INITIAL,
            cash_override=(cash if show_cash else None),
            futures_positions=futures,
            futures_prices=FUT_PRICES,
            futures_cash=(fcash if show_cash else None),
        )
    return buf.getvalue().decode()


def _num(html: str, label: str) -> int:
    m = re.search(re.escape(label) + r"</div><div class='value'>([\d,]+)원", html)
    assert m, f"카드 라벨 '{label}' 의 헤드라인 숫자를 찾지 못함"
    return int(m.group(1).replace(",", ""))


def _nav(holdings, futures, *, cash=CASH, fcash=FCASH):
    spot_q = {h["ticker"]: {"price": SPOT_PRICES[h["ticker"]]} for h in holdings}
    return compute_balance_nav(
        spot_q, FUT_PRICES,
        holdings=holdings, futures_positions=futures,
        account={"cash": cash, "futures_cash": fcash, "initial_capital": INITIAL},
    )


# ── 1) 총자산 헤드라인 == compute_balance_nav ────────────────────────────────
def test_total_asset_card_equals_balance_nav():
    holds, futs = _holdings(), _futures()
    html = _render(holds, futs)
    nav = _nav(holds, futs)
    card = _num(html, "총 자산 · 전부 청산 시 예수금")
    assert abs(card - int(nav["nav"])) <= 2


# ── 2) 총평가금 헤드라인 == 현물평가 + 선물 현재 명목금 (예수금 제외, gross) ──
def test_gross_eval_headline_is_spot_plus_futures_notional():
    holds, futs = _holdings(), _futures()
    html = _render(holds, futs)
    spot_eval = sum(SPOT_PRICES[h["ticker"]] * h["quantity"] for h in holds)
    fut_notional = sum(FUT_PRICES[f"{f['symbol']}|{f['contract_month']}"]["price"]
                       * f["contracts"] * f["multiplier"] for f in futs)
    gross = _num(html, "총 평가금 · 레버리지 포함")
    assert abs(gross - int(spot_eval + fut_notional)) <= 2


# ── 3) 핵심 불변식: gross − 신용 − 선물차입 + 예수금 == NAV ───────────────────
def test_gross_minus_loans_plus_cash_reconciles_to_nav():
    holds, futs = _holdings(), _futures()
    nav = _nav(holds, futs)["nav"]

    spot_eval = sum(SPOT_PRICES[h["ticker"]] * h["quantity"] for h in holds)
    fut_notional = sum(FUT_PRICES[f"{f['symbol']}|{f['contract_month']}"]["price"]
                       * f["contracts"] * f["multiplier"] for f in futs)
    credit = sum(h["credit_loan"] for h in holds)
    entry_notional = sum(f["avg_entry_price"] * f["contracts"] * f["multiplier"] for f in futs)
    margin = sum(f["initial_margin"] for f in futs)
    fut_financing = entry_notional - margin

    gross = spot_eval + fut_notional
    pos_equity = gross - credit - fut_financing
    assert abs((pos_equity + CASH + FCASH) - nav) <= 2


# ── 4) "현재 평가금 − 증거금" 으로 빼면 미실현손익만큼 틀린다 (선물 차입금 ≠ 현재명목−증거금) ──
def test_current_notional_minus_margin_drops_unrealized():
    holds, futs = _holdings(), _futures()
    fut_notional = sum(FUT_PRICES[f"{f['symbol']}|{f['contract_month']}"]["price"]
                       * f["contracts"] * f["multiplier"] for f in futs)
    entry_notional = sum(f["avg_entry_price"] * f["contracts"] * f["multiplier"] for f in futs)
    margin = sum(f["initial_margin"] for f in futs)
    unreal = fut_notional - entry_notional  # all-long

    correct_financing = entry_notional - margin
    naive_financing = fut_notional - margin  # 사용자가 말한 "현재 평가금 − 증거금"
    # 차이는 정확히 선물 미실현손익 → 자산에서 그만큼 사라진다(이게 버그).
    assert abs((naive_financing - correct_financing) - unreal) <= 2
    assert unreal != 0  # 가격이 진입가와 달라 차이가 실제로 발생함을 확인


# ── 5) 엣지: 선물 없음 (현물+신용만) ─────────────────────────────────────────
def test_edge_no_futures():
    holds = _holdings()
    html = _render(holds, [])
    nav = _nav(holds, [])
    assert abs(_num(html, "총 자산 · 전부 청산 시 예수금") - int(nav["nav"])) <= 2
    # 선물이 없으면 라벨은 여전히 신용 때문에 '레버리지 포함'
    spot_eval = sum(SPOT_PRICES[h["ticker"]] * h["quantity"] for h in holds)
    assert abs(_num(html, "총 평가금 · 레버리지 포함") - int(spot_eval)) <= 2


# ── 6) 엣지: 신용·선물 모두 없음 (순수 현물) → 평가금 라벨 단순 ───────────────
def test_edge_pure_spot_label():
    holds = [{"name": "제주반도체", "ticker": "080220.KQ", "sector": "반도체",
              "quantity": 150, "avg_price": 108183, "total_invested": 108183 * 150,
              "credit_loan": 0.0}]
    html = _render(holds, [])
    spot_eval = SPOT_PRICES["080220.KQ"] * 150
    # 레버리지 전혀 없음 → '총 평가금' 단순 라벨
    assert "총 평가금</div>" in html
    assert abs(_num(html, "총 평가금") - int(spot_eval)) <= 2
