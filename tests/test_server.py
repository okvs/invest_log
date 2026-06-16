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


def test_get_state_shape():
    _seed()
    service.record_buy("삼성전자", 10, 70000)
    service.record_sell("삼성전자", 4, 80000)
    st = service.get_state()
    assert st["holdings"][0]["name"] == "삼성전자"
    assert st["account"]["cash"] is not None
    assert len(st["unreviewed_sells"]) == 1  # 미회고 매도 1


# ---------------------------------------------------------------------------
# FastAPI 엔드포인트
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    app.dependency_overrides[require_user] = lambda: "testuid"
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_requires_auth():
    c = TestClient(app)  # override 없음 + 토큰 없음
    assert c.get("/api/state").status_code == 401
    assert c.post("/api/buy", json={"name": "x", "quantity": 1, "price": 1}).status_code == 401


def test_api_health_open():
    assert TestClient(app).get("/api/health").json()["ok"] is True


def test_password_login_flow():
    """첫 로그인=비번 설정, 토큰으로 보호 API 접근, 틀린 비번 401."""
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
