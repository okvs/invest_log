"""PWA 백엔드(server) — service 레이어 + FastAPI 엔드포인트 테스트."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import service
from server.app import app
from server.auth import require_user
from storage.json_store import (
    load_account,
    load_holdings,
    load_retrospectives,
    load_transactions,
    save_account,
    save_holdings,
)


def _seed():
    save_account({"initial_capital": 155000000.0, "cash": 100000000.0})
    save_holdings([])


# ---------------------------------------------------------------------------
# service 레이어
# ---------------------------------------------------------------------------
def test_record_buy_new_and_add():
    _seed()
    service.record_buy("삼성전자", 10, 70000, sector="반도체", thesis="AI")
    h = load_holdings()
    assert len(h) == 1 and h[0]["quantity"] == 10 and h[0]["avg_price"] == 70000
    assert load_account()["cash"] == pytest.approx(100000000 - 700000)

    service.record_buy("삼성전자", 10, 80000)  # 추가매수 → 평단 재계산
    h = load_holdings()[0]
    assert h["quantity"] == 20
    assert h["avg_price"] == round((700000 + 800000) / 20)
    assert h["sector"] == "반도체"  # 기존 유지


def test_record_sell_and_cash():
    _seed()
    service.record_buy("삼성전자", 10, 70000, sector="반도체")
    tx = service.record_sell("삼성전자", 5, 80000, reason="목표가")
    assert tx["type"] == "sell"
    assert tx["profit_loss"] == pytest.approx((80000 - 70000) * 5)
    h = load_holdings()[0]
    assert h["quantity"] == 5
    total = 80000 * 5
    fee = round(total * service.SELL_FEE_RATE)
    # 매수 후 cash = 1e8 - 700000, 매도 후 += total - fee
    assert load_account()["cash"] == pytest.approx(100000000 - 700000 + total - fee)


def test_record_sell_over_holding_raises():
    _seed()
    service.record_buy("삼성전자", 3, 70000)
    with pytest.raises(ValueError):
        service.record_sell("삼성전자", 5, 80000)


def test_record_retro_links_transaction():
    _seed()
    service.record_buy("삼성전자", 10, 70000)
    sell_tx = service.record_sell("삼성전자", 10, 80000)
    r = service.record_retro(sell_tx["id"], thesis_correct=True, lessons="좋았다")
    assert load_retrospectives()[0]["id"] == r["id"]
    linked = next(t for t in load_transactions() if t["id"] == sell_tx["id"])
    assert linked["retrospective_id"] == r["id"]
    # 이미 회고한 거래 → 거부
    with pytest.raises(ValueError):
        service.record_retro(sell_tx["id"])


def test_record_sector_by_name_and_ticker():
    _seed()
    # 국내: 이름 매칭 (섹터 비어 있던 종목 보정)
    service.record_buy("삼성전자우", 10, 60000)  # sector 미지정 → ""
    out = service.record_sector("반도체", name="삼성전자우")
    assert out["sector"] == "반도체"
    assert load_holdings()[0]["sector"] == "반도체"

    # 미국: ticker 우선 매칭 ("미국주식" 기본값 → 실섹터)
    save_holdings(load_holdings() + [
        {"name": "스페이스X", "ticker": "SPCX", "sector": "미국주식",
         "currency": "USD", "quantity": 5, "avg_price": 100.0},
    ])
    service.record_sector("우주", ticker="SPCX", name="틀린이름")
    spcx = next(h for h in load_holdings() if h.get("ticker") == "SPCX")
    assert spcx["sector"] == "우주"


def test_record_sector_errors():
    _seed()
    service.record_buy("삼성전자", 1, 70000, sector="반도체")
    with pytest.raises(ValueError):
        service.record_sector("", name="삼성전자")       # 빈 섹터
    with pytest.raises(ValueError):
        service.record_sector("반도체", name="없는종목")  # 매칭 실패


def test_api_sector_endpoint(client):
    _seed()
    service.record_buy("삼성전자우", 10, 60000)  # sector ""
    r = client.post("/api/sector", json={"sector": "반도체", "name": "삼성전자우"})
    assert r.status_code == 200 and r.json()["ok"]
    assert load_holdings()[0]["sector"] == "반도체"
    # 빈 섹터 → 400
    assert client.post("/api/sector", json={"sector": "", "name": "삼성전자우"}).status_code == 400


def test_get_state_shape():
    _seed()
    service.record_buy("삼성전자", 10, 70000)
    service.record_sell("삼성전자", 4, 80000)
    st = service.get_state()
    assert st["holdings"][0]["name"] == "삼성전자"
    assert st["account"]["cash"] is not None
    assert len(st["unreviewed_sells"]) == 1  # 미회고 매도 1


# ---------------------------------------------------------------------------
# 연금(pension) 토글 — 거래 단위로 켜면 보유/예수금에서 제외, 끄면 복원
# ---------------------------------------------------------------------------
def test_toggle_pension_buy_removes_holding_and_restores_cash():
    _seed()
    service.record_buy("KODEXAI반도체TOP2플러스", 829, 61810)
    cash_after_buy = load_account()["cash"]
    buy_tx = next(t for t in load_transactions() if t["type"] == "buy")

    out = service.toggle_pension(buy_tx["id"])
    assert out["is_pension"] is True
    # 보유 제거 + 예수금 전액(증거금100%) 복원
    assert load_holdings() == []
    assert load_account()["cash"] == pytest.approx(cash_after_buy + 829 * 61810)
    # 거래는 transactions.json 에 남아 있고 연금 플래그가 켜짐
    saved = next(t for t in load_transactions() if t["id"] == buy_tx["id"])
    assert saved["is_pension"] is True


def test_toggle_pension_buy_toggles_back():
    _seed()
    service.record_buy("삼성전자", 10, 70000, sector="반도체")
    buy_tx = next(t for t in load_transactions() if t["type"] == "buy")
    service.toggle_pension(buy_tx["id"])           # on → 제거
    assert load_holdings() == []
    out = service.toggle_pension(buy_tx["id"])     # off → 복원
    assert out["is_pension"] is False
    h = load_holdings()
    assert len(h) == 1 and h[0]["quantity"] == 10 and h[0]["avg_price"] == 70000
    assert h[0]["sector"] == "반도체"


def test_toggle_pension_does_not_touch_other_holdings():
    _seed()
    service.record_buy("삼성전자", 10, 70000, sector="반도체")
    service.record_buy("KODEX", 100, 10000)
    kodex_buy = next(t for t in load_transactions() if t["name"] == "KODEX")
    service.toggle_pension(kodex_buy["id"])
    names = [h["name"] for h in load_holdings()]
    assert "삼성전자" in names and "KODEX" not in names


def test_toggle_pension_sell_readds_shares_and_costbasis():
    _seed()
    service.record_buy("삼성전자", 10, 70000, sector="반도체")   # avg 70000
    sell = service.record_sell("삼성전자", 4, 80000)             # qty 6, pnl=40000
    cash_after_sell = load_account()["cash"]
    service.toggle_pension(sell["id"])                          # 매도를 연금 → 4주 복원
    h = load_holdings()[0]
    assert h["quantity"] == 10
    assert h["avg_price"] == 70000                              # 평단 원복
    total = 80000 * 4
    fee = round(total * service.SELL_FEE_RATE)
    assert load_account()["cash"] == pytest.approx(cash_after_sell - (total - fee))


def test_pension_excluded_from_realized_profit_trend(monkeypatch):
    """연금 매도의 실현손익은 자산그래프(누적 실현)에서 제외된다."""
    from bot import asset_history
    _seed()
    service.record_buy("삼성전자", 10, 70000)
    s1 = service.record_sell("삼성전자", 5, 90000)   # pnl=100000 (일반)
    service.record_buy("연금주", 10, 1000)
    s2 = service.record_sell("연금주", 10, 2000)      # pnl=10000 → 연금처리
    service.toggle_pension(s2["id"])
    # 종가 조회를 막아 순수 실현 누적만 계산되게 한다(미실현은 0/생략).
    monkeypatch.setattr(asset_history, "_fetch_pykrx_closes", lambda codes, s, e: {})
    rows = asset_history.compute_profit_trend()
    realized_final = rows[-1]["realized"] if rows else 0
    assert realized_final == pytest.approx(100000)   # 연금 매도(10000) 제외


# ---------------------------------------------------------------------------
# FastAPI 엔드포인트
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    app.dependency_overrides[require_user] = lambda: "testuid"
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_public_when_auth_disabled():
    """공개 모드(기본): 토큰 없이도 /api/* 사용 가능(200)."""
    _seed()
    c = TestClient(app)  # override 없음 + 토큰 없음
    assert c.get("/api/state").status_code == 200
    assert c.post("/api/buy", json={"name": "삼성전자", "quantity": 1, "price": 70000}).status_code == 200


def test_api_requires_auth_when_enabled(monkeypatch):
    """WEBAPP_AUTH 켜면 토큰 없는 요청은 401."""
    monkeypatch.setenv("WEBAPP_AUTH", "1")
    c = TestClient(app)  # override 없음 + 토큰 없음
    assert c.get("/api/state").status_code == 401
    assert c.post("/api/buy", json={"name": "x", "quantity": 1, "price": 1}).status_code == 401


def test_api_health_open():
    h = TestClient(app).get("/api/health").json()
    assert h["ok"] is True
    assert h["auth_required"] is False  # 기본 공개 모드


def test_password_login_flow(monkeypatch):
    """WEBAPP_AUTH 켠 상태: 첫 로그인=비번 설정, 토큰으로 보호 API, 틀린 비번 401."""
    monkeypatch.setenv("WEBAPP_AUTH", "1")
    _seed()
    c = TestClient(app)  # override 없이 실제 인증 경로 사용
    # 첫 로그인 → 비밀번호 설정 + 토큰
    r = c.post("/api/login", json={"password": "secret123"})
    assert r.status_code == 200
    token = r.json()["token"]
    # 토큰으로 보호 API 접근
    r = c.get("/api/state", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    # 틀린 비밀번호 → 401
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    # 맞는 비밀번호 재로그인 → 200
    assert c.post("/api/login", json={"password": "secret123"}).status_code == 200
    # 잘못된 토큰 → 401
    assert c.get("/api/state", headers={"Authorization": "Bearer bad.token"}).status_code == 401


def test_api_buy_sell_retro_flow(client):
    _seed()
    r = client.post("/api/buy", json={"name": "삼성전자", "quantity": 10, "price": 70000, "sector": "반도체"})
    assert r.status_code == 200 and r.json()["ok"]
    assert load_holdings()[0]["quantity"] == 10

    r = client.post("/api/sell", json={"name": "삼성전자", "quantity": 10, "price": 80000, "reason": "익절"})
    assert r.status_code == 200
    sell_id = r.json()["transaction"]["id"]

    r = client.post("/api/retro", json={"transaction_id": sell_id, "thesis_correct": True, "lessons": "ok"})
    assert r.status_code == 200 and r.json()["ok"]

    st = client.get("/api/state").json()
    assert st["unreviewed_sells"] == []  # 회고 완료로 비워짐


def test_api_buy_bad_input(client):
    _seed()
    r = client.post("/api/sell", json={"name": "없는종목", "quantity": 1, "price": 100})
    assert r.status_code == 400


def test_api_pension_endpoint(client):
    _seed()
    client.post("/api/buy", json={"name": "KODEX", "quantity": 100, "price": 10000})
    buy_id = next(t for t in load_transactions() if t["type"] == "buy")["id"]
    r = client.post("/api/pension", json={"transaction_id": buy_id})
    assert r.status_code == 200 and r.json()["ok"]
    assert r.json()["transaction"]["is_pension"] is True
    assert load_holdings() == []
    # 없는 거래 → 400
    assert client.post("/api/pension", json={"transaction_id": "nope"}).status_code == 400
