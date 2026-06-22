"""웹 푸시 서비스 — VAPID 키·구독 관리·발송·확인필요 알림 (네트워크는 모킹)."""
from __future__ import annotations

import base64

import pywebpush

from bot import push_service
from storage.json_store import save_holdings, save_transactions


class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_public_key_generated_and_b64url():
    k = push_service.public_key()
    # 65바이트 비압축 EC 포인트 = b64url(패딩없음) 약 87자
    raw = base64.urlsafe_b64decode(k + "=" * ((4 - len(k) % 4) % 4))
    assert len(raw) == 65 and raw[0] == 0x04
    assert push_service.public_key() == k  # 재호출 시 동일(파일 재사용)


def test_add_subscription_dedups_by_endpoint():
    push_service.add_subscription({"endpoint": "https://x", "keys": {"a": 1}})
    push_service.add_subscription({"endpoint": "https://x", "keys": {"a": 2}})  # 같은 endpoint
    push_service.add_subscription({"endpoint": "https://y", "keys": {}})
    subs = push_service.load_subscriptions()
    assert len(subs) == 2
    x = next(s for s in subs if s["endpoint"] == "https://x")
    assert x["keys"]["a"] == 2  # 최신으로 교체


def test_send_push_counts_and_prunes_expired(monkeypatch):
    push_service.save_subscriptions([
        {"endpoint": "https://a", "keys": {}},
        {"endpoint": "https://gone", "keys": {}},
    ])

    def fake_webpush(subscription_info, data, **kw):
        if subscription_info["endpoint"] == "https://gone":
            exc = pywebpush.WebPushException("gone")
            exc.response = _Resp(410)
            raise exc
        return True

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    n = push_service.send_push("제목", "본문")
    assert n == 1
    eps = {s["endpoint"] for s in push_service.load_subscriptions()}
    assert eps == {"https://a"}  # 만료(410) 제거


def test_send_push_no_subs_returns_zero():
    push_service.save_subscriptions([])
    assert push_service.send_push("t", "b") == 0


def test_pending_input_counts():
    save_holdings([
        {"name": "A", "sector": "", "quantity": 10},          # 섹터 필요
        {"name": "B", "sector": "반도체", "quantity": 5},       # ok
        {"name": "C", "sector": "미국주식", "quantity": 3},      # 섹터 필요(기본값)
        {"name": "D", "sector": "기타", "quantity": 0},          # qty 0 → 제외
    ])
    save_transactions([
        {"type": "sell", "retrospective_id": "", "is_pension": False},   # 회고 대기
        {"type": "sell", "retrospective_id": "r1", "is_pension": False},  # 회고됨
        {"type": "sell", "retrospective_id": "", "is_pension": True},     # 연금 → 제외
        {"type": "buy"},
    ])
    sector_n, retro_n = push_service.pending_input_counts()
    assert sector_n == 2
    assert retro_n == 1


def test_notify_pending_seeds_then_pushes_on_growth(monkeypatch):
    sent = []
    monkeypatch.setattr(push_service, "send_push", lambda t, b, url="": sent.append((t, b)) or 1)
    save_holdings([{"name": "A", "sector": "", "quantity": 10}])
    save_transactions([])
    # 첫 호출 = baseline 시드(미발송)
    assert push_service.notify_pending_inputs_if_new() is False
    assert sent == []
    # 섹터 필요 종목 1개 추가 → 증가 → 발송
    save_holdings([
        {"name": "A", "sector": "", "quantity": 10},
        {"name": "E", "sector": "", "quantity": 1},
    ])
    assert push_service.notify_pending_inputs_if_new() is True
    assert len(sent) == 1 and "확인 필요" in sent[0][0]
    # 변동 없음 → 미발송
    assert push_service.notify_pending_inputs_if_new() is False
    assert len(sent) == 1
