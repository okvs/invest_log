"""bot.audit — 회계 불변식 감사."""
from __future__ import annotations

import pytest

import bot.audit as audit
from bot.audit import CASH_DRIFT_LIMIT, audit_and_notify, run_audit
from storage.json_store import save_account, save_futures_positions, save_holdings


def _healthy():
    save_holdings([{
        "name": "삼성전자", "quantity": 10, "avg_price": 70000,
        "total_invested": 700000, "credit_loan": 300000,
        "by_account": [
            {"account": "KB", "quantity": 6, "credit": 300000},
            {"account": "신한", "quantity": 4, "credit": 0},
        ],
    }, {
        # USD 종목 — by_account 미사용 + 반올림 오차(1% 이내)는 허용
        "name": "마이크론2배", "currency": "USD", "quantity": 1748,
        "avg_price": 26, "total_invested": 45379, "credit_loan": 0,
    }])
    save_account({
        "initial_capital": 155000000.0, "cash": 100000000.0,
        "cash_by_account": {"KB": 40000000.0, "신한": 60000000.0},
        "futures_cash": 0.0, "usd_cash": 7105.06,
    })
    save_futures_positions([])


def test_healthy_ledger_passes():
    _healthy()
    assert run_audit() == []


def test_negative_quantity_is_error():
    _healthy()
    save_holdings([{"name": "X", "quantity": -3, "avg_price": 100, "total_invested": 0}])
    codes = {v.code for v in run_audit()}
    assert "qty-nonpositive" in codes


def test_credit_mismatch_is_error():
    _healthy()
    save_holdings([{
        "name": "삼성전자", "quantity": 10, "avg_price": 70000,
        "total_invested": 700000, "credit_loan": 500000,
        "by_account": [{"account": "KB", "quantity": 10, "credit": 300000}],
    }])
    v = {x.code: x.severity for x in run_audit()}
    assert v.get("credit-mismatch") == "error"


def test_byaccount_qty_mismatch_is_error():
    _healthy()
    save_holdings([{
        "name": "삼성전자", "quantity": 10, "avg_price": 70000,
        "total_invested": 700000, "credit_loan": 0,
        "by_account": [{"account": "KB", "quantity": 7, "credit": 0}],
    }])
    assert any(x.code == "byaccount-qty-mismatch" for x in run_audit())


def test_cash_drift_over_limit_is_warn():
    _healthy()
    save_account({
        "initial_capital": 1.0, "cash": 100000000.0 + CASH_DRIFT_LIMIT + 1,
        "cash_by_account": {"KB": 100000000.0},
    })
    v = {x.code: x.severity for x in run_audit()}
    assert v.get("cash-drift") == "warn"


def test_cash_drift_under_limit_ok():
    _healthy()
    save_account({
        "initial_capital": 1.0, "cash": 100000000.0 + CASH_DRIFT_LIMIT - 1,
        "cash_by_account": {"KB": 100000000.0},
    })
    assert not any(x.code == "cash-drift" for x in run_audit())


def test_dead_letter_is_warn(tmp_data_dir):
    _healthy()
    from storage.json_store import save
    save("kakao_apply_state.json", {"123": 5, "_failed": {"123": [4]}})
    assert any(x.code == "kakao-dead-letter" for x in run_audit())


def test_notify_dedup(monkeypatch):
    """같은 위반 조합은 푸시 1회, 해소 후 재발 시 다시 푸시."""
    _healthy()
    pushes = []
    import bot.push_service as ps
    monkeypatch.setattr(ps, "send_push", lambda title, body: pushes.append(title))

    save_holdings([{"name": "X", "quantity": -1, "avg_price": 0, "total_invested": 0}])
    audit_and_notify()
    audit_and_notify()  # 같은 위반 → 추가 푸시 없음
    assert len(pushes) == 1

    save_holdings([])   # 해소
    audit_and_notify()  # 위반 0 → 푸시 없음, fingerprint 초기화
    assert len(pushes) == 1

    save_holdings([{"name": "X", "quantity": -1, "avg_price": 0, "total_invested": 0}])
    audit_and_notify()  # 재발 → 다시 푸시
    assert len(pushes) == 2
