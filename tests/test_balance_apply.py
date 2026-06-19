"""잔고 스샷 → 융자 반영 헬퍼(scripts/balance_apply.py) 테스트.

state 출력 형태, apply 가 종목별 credit_loan 을 봇 `융자` 명령과 동일하게
set 하는지(미보유 종목 unmatched 처리 포함)를 검증한다. 대시보드 재발행은
네트워크 부작용이라 monkeypatch 로 차단한다.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import balance_apply as ba  # noqa: E402

from storage.json_store import (  # noqa: E402
    load_account,
    load_holdings,
    save_account,
    save_holdings,
)


def _seed():
    save_holdings([
        {"name": "SK하이닉스", "sector": "반도체", "quantity": 35,
         "avg_price": 2000000, "total_invested": 70000000, "credit_loan": 50000000},
        {"name": "삼성전자", "sector": "반도체", "quantity": 10,
         "avg_price": 70000, "total_invested": 700000, "credit_loan": 0},
    ])


def test_state_outputs_active_holdings(capsys):
    _seed()
    assert ba.cmd_state() == 0
    out = json.loads(capsys.readouterr().out)
    names = {h["name"]: h for h in out["holdings"]}
    assert names["SK하이닉스"]["credit_loan"] == 50000000
    assert names["삼성전자"]["credit_loan"] == 0


def test_apply_sets_credit_loan(capsys, monkeypatch):
    _seed()
    monkeypatch.setattr(ba, "_republish", lambda: False)  # 실제 Firebase 배포 차단
    loan_json = json.dumps({"SK하이닉스": 52000000, "삼성전자": 39140000})

    assert ba.cmd_apply("testreq", loan_json) == 0
    out = json.loads(capsys.readouterr().out)

    # 변경 2건 (이름/old/new)
    chg = {c["name"]: c for c in out["changes"]}
    assert chg["SK하이닉스"]["old"] == 50000000 and chg["SK하이닉스"]["new"] == 52000000
    assert chg["삼성전자"]["old"] == 0 and chg["삼성전자"]["new"] == 39140000
    assert out["unmatched"] == []
    assert out["total_loan"] == 52000000 + 39140000

    # 실제 저장 반영
    by_name = {h["name"]: h for h in load_holdings()}
    assert by_name["SK하이닉스"]["credit_loan"] == 52000000
    assert by_name["삼성전자"]["credit_loan"] == 39140000


def test_apply_unmatched_left_untouched(capsys, monkeypatch):
    _seed()
    monkeypatch.setattr(ba, "_republish", lambda: False)
    # 보유에 없는 종목 → unmatched, 기존 보유는 그대로
    assert ba.cmd_apply("testreq", json.dumps({"없는종목": 1000000})) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changes"] == []
    assert out["unmatched"] == ["없는종목"]
    by_name = {h["name"]: h for h in load_holdings()}
    assert by_name["SK하이닉스"]["credit_loan"] == 50000000  # 불변


def test_apply_rejects_negative(monkeypatch):
    _seed()
    monkeypatch.setattr(ba, "_republish", lambda: False)
    with pytest.raises(SystemExit):
        ba.cmd_apply("testreq", json.dumps({"SK하이닉스": -100}))


def _seed_by_account():
    """KB+신한 분해를 가진 보유(combined = 계좌별 합)."""
    save_holdings([
        {"name": "SK하이닉스", "sector": "반도체", "quantity": 35,
         "avg_price": 2000000, "total_invested": 70000000, "credit_loan": 57671800,
         "by_account": [
             {"account": "KB", "quantity": 30, "credit": 50693000, "funding": "현금+자기융자"},
             {"account": "신한", "quantity": 5, "credit": 6978800, "funding": "자기융자"},
         ]},
    ])


def test_state_includes_by_account(capsys):
    _seed_by_account()
    assert ba.cmd_state() == 0
    out = json.loads(capsys.readouterr().out)
    ba_list = out["holdings"][0]["by_account"]
    accts = {e["account"]: e for e in ba_list}
    assert accts["KB"]["credit"] == 50693000 and accts["신한"]["credit"] == 6978800


def test_apply_account_preserves_other_account(capsys, monkeypatch):
    """KB만 보이는 스샷 — KB만 갱신하고 신한 융자는 보존, combined = 합."""
    _seed_by_account()
    monkeypatch.setattr(ba, "_republish", lambda: False)
    # KB 융자가 50,693,000 → 60,000,000 으로 늘어난 스샷
    assert ba.cmd_apply("r", json.dumps({"SK하이닉스": 60000000}), account="KB") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["account"] == "KB"
    chg = out["changes"][0]
    assert chg["old"] == 57671800 and chg["new"] == 60000000 + 6978800  # 신한 보존

    h = load_holdings()[0]
    accts = {e["account"]: e for e in h["by_account"]}
    assert accts["KB"]["credit"] == 60000000
    assert accts["신한"]["credit"] == 6978800              # 불변
    assert h["credit_loan"] == 60000000 + 6978800          # combined = 합


def test_apply_account_creates_missing_entry(capsys, monkeypatch):
    """그 계좌 by_account 항목이 없으면 새로 만들고 warning."""
    save_holdings([
        {"name": "삼성전자우", "sector": "반도체", "quantity": 175,
         "avg_price": 227000, "total_invested": 39725000, "credit_loan": 0,
         "by_account": [{"account": "KB", "quantity": 175, "credit": 0, "funding": ""}]},
    ])
    monkeypatch.setattr(ba, "_republish", lambda: False)
    assert ba.cmd_apply("r", json.dumps({"삼성전자우": 39725000}), account="KB") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changes"][0]["new"] == 39725000
    assert load_holdings()[0]["credit_loan"] == 39725000


def test_cash_account_preserves_other_and_sums(capsys, monkeypatch):
    """KB D+2예수금만 갱신 → 신한 예수금 보존, 합산 cash = 계좌별 합."""
    save_account({
        "initial_capital": 155000000.0, "cash": 84258310.0,
        "cash_by_account": {"KB": 29757030, "신한": 67117018},
        "futures_cash": 48763854.0,
    })
    monkeypatch.setattr(ba, "_republish", lambda: False)
    assert ba.cmd_cash("r", "82685159", account="KB") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["account_change"] == {"account": "KB", "old": 29757030, "new": 82685159}
    assert out["new_cash"] == 82685159 + 67117018  # 신한 보존 + 합산

    acc = load_account()
    assert acc["cash_by_account"]["KB"] == 82685159
    assert acc["cash_by_account"]["신한"] == 67117018      # 불변
    assert acc["cash"] == 82685159 + 67117018
    assert acc["futures_cash"] == 48763854.0               # 선물 버킷 불변


def test_state_includes_cash(capsys):
    save_account({"initial_capital": 1.0, "cash": 123.0,
                  "cash_by_account": {"KB": 100, "신한": 23}, "futures_cash": 9.0})
    save_holdings([])
    assert ba.cmd_state() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cash"] == 123 and out["futures_cash"] == 9
    assert out["cash_by_account"] == {"KB": 100, "신한": 23}
