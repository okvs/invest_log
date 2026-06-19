"""미국주식 시세 선택 로직 + Holding 통화 라운드트립 (네트워크 없음)."""
from __future__ import annotations

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
