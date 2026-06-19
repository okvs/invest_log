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

from storage.json_store import load_holdings, save_holdings  # noqa: E402


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
