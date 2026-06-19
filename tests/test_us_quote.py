"""미국주식 시세 선택 로직 + Holding 통화 라운드트립 (네트워크 없음)."""
from __future__ import annotations

import bot.html_report as hr
from bot.us_quote import _pick_price
from models.portfolio import Holding


def test_pick_price_marketstate():
    # 애프터장/마감 → 포스트마켓 우선
    assert _pick_price({"marketState": "POST", "regularMarketPrice": 100, "postMarketPrice": 99}) == (99.0, "post")
    assert _pick_price({"marketState": "CLOSED", "regularMarketPrice": 100, "postMarketPrice": 99}) == (99.0, "post")
    # 프리장 → 프리마켓 우선
    assert _pick_price({"marketState": "PRE", "regularMarketPrice": 100, "preMarketPrice": 101}) == (101.0, "pre")
    # 정규장 → 정규가
    assert _pick_price({"marketState": "REGULAR", "regularMarketPrice": 100}) == (100.0, "reg")
    # 포스트가 없으면 정규가로 폴백
    assert _pick_price({"marketState": "CLOSED", "regularMarketPrice": 100, "postMarketPrice": None}) == (100.0, "reg")
    # 아무것도 없으면 (None, "none")
    assert _pick_price({"marketState": "CLOSED"}) == (None, "none")


def test_holding_currency_roundtrip():
    h = Holding(name="MULL", sector="미국주식", buy_date="2026-06-18",
                avg_price=867.0, quantity=7, total_invested=6069.0,
                ticker="MULL", currency="USD")
    d = h.to_dict()
    assert d["currency"] == "USD"
    assert Holding.from_dict(d).currency == "USD"
    # 기존(통화 필드 없는) 데이터는 KRW 로 하위호환
    legacy = {"name": "삼성전자", "sector": "반도체", "buy_date": "2026-01-01",
              "avg_price": 70000, "quantity": 10, "total_invested": 700000}
    assert Holding.from_dict(legacy).currency == "KRW"


def test_dashboard_renders_usd_holding_in_krw(monkeypatch):
    """USD 보유가 USD가격으로 표시되고 평가/NAV는 KRW 환산되는지 (네트워크 모킹)."""
    monkeypatch.setattr(hr, "fetch_usdkrw", lambda: 1500.0)
    monkeypatch.setattr(hr, "fetch_us_quotes",
                        lambda tks: {"MULL": {"price": 900.0, "change_pct": 3.0, "source": "post"}})
    monkeypatch.setattr(hr, "fetch_current_quotes", lambda tks: {})  # 국내 시세 호출 차단

    holds = [{"name": "MULL", "sector": "미국주식", "quantity": 10,
              "avg_price": 800.0, "total_invested": 8000.0, "currency": "USD", "ticker": "MULL"}]
    html = hr.build_html_report(
        holds, initial_capital=20000000, show_cash=True,
        cash_override=0, usd_cash=1000.0, cash_by_account={},
    ).getvalue().decode("utf-8")

    assert "$900.00" in html and "$800.00" in html   # USD 가격/평단 표기
    assert "🇺🇸" in html and "나무" in html
    # 평가 = 10 * 900 * 1500 = 13,500,000 (KRW), 예수금 미국 = 1000*1500 = 1,500,000
    assert "13,500,000" in html or "1,350만" in html
    assert "1,500,000" in html  # 미국 예수금 KRW 환산
