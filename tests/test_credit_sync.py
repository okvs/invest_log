"""`융자` 동기화 — 입력 파싱 + credit_loan 적용 순수 로직 테스트."""
from __future__ import annotations

import pytest

from bot.handlers.credit_sync import apply_credit_sync, parse_credit_sync_input


# ── parse_credit_sync_input ─────────────────────────────────────────────

def test_parse_basic_lines():
    out = parse_credit_sync_input(
        "SK하이닉스 61,391,161\n삼성전자 3914만\n삼성전기 0"
    )
    assert out["SK하이닉스"] == 61_391_161
    assert out["삼성전자"] == 39_140_000
    assert out["삼성전기"] == 0


def test_parse_name_with_spaces():
    out = parse_credit_sync_input("TIGER 삼성전자단일종목레버리지 1억")
    assert out["TIGER 삼성전자단일종목레버리지"] == 100_000_000


def test_parse_skips_empty_lines():
    out = parse_credit_sync_input("\n삼성전자 100만\n\n")
    assert out == {"삼성전자": 1_000_000}


def test_parse_rejects_garbage_amount():
    with pytest.raises(ValueError):
        parse_credit_sync_input("삼성전자 십만원")


def test_parse_rejects_single_token_line():
    with pytest.raises(ValueError):
        parse_credit_sync_input("삼성전자")


def test_parse_rejects_empty_input():
    with pytest.raises(ValueError):
        parse_credit_sync_input("   \n  ")


# ── apply_credit_sync ───────────────────────────────────────────────────

def _holdings():
    return [
        {"name": "SK하이닉스", "quantity": 35, "credit_loan": 61_391_161},
        {"name": "삼성전자", "quantity": 196, "credit_loan": 39_135_601},
        {"name": "TIGER삼성전자단일종목레버리지", "quantity": 565, "credit_loan": 0},
        {"name": "팔린종목", "quantity": 0, "credit_loan": 999},  # 비활성 — 매칭 제외
    ]


def test_apply_updates_matched_and_reports_changes():
    hs = _holdings()
    changes, unmatched = apply_credit_sync(hs, {"삼성전자": 40_000_000})
    assert changes == [("삼성전자", 39_135_601, 40_000_000)]
    assert unmatched == []
    assert hs[1]["credit_loan"] == 40_000_000
    assert hs[0]["credit_loan"] == 61_391_161  # 입력 없는 종목은 유지


def test_apply_matches_spaceless_and_case():
    hs = _holdings()
    changes, _ = apply_credit_sync(hs, {"tiger 삼성전자단일종목레버리지": 5_000_000})
    assert hs[2]["credit_loan"] == 5_000_000
    assert changes[0][0] == "TIGER삼성전자단일종목레버리지"


def test_apply_zero_clears_credit():
    hs = _holdings()
    apply_credit_sync(hs, {"SK하이닉스": 0})
    assert hs[0]["credit_loan"] == 0


def test_apply_unmatched_collected_inactive_excluded():
    hs = _holdings()
    changes, unmatched = apply_credit_sync(hs, {"없는종목": 1_000, "팔린종목": 1_000})
    assert set(unmatched) == {"없는종목", "팔린종목"}
    assert changes == []
    assert hs[3]["credit_loan"] == 999  # 비활성 보유는 건드리지 않음


def test_apply_same_value_not_reported_as_change():
    hs = _holdings()
    changes, _ = apply_credit_sync(hs, {"SK하이닉스": 61_391_161})
    assert changes == []
