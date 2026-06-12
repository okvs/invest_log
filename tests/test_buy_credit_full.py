"""전액 신용(자기융자, margin:0) 매수 회계 — KB 자기융자는 증거금률과 무관하게
매수금 전액이 융자로 잡히므로 credit=100%·현금차감 0 이어야 한다 (2026-06-12)."""
from __future__ import annotations

from bot.handlers.buy import _process_and_save
from parsers.input_parser import BuyInput
from storage.json_store import load_account, load_holdings, save_account


def _buy(qty=10, price=100_000.0):
    return BuyInput(name="테스트종목", ticker="000001.KS", sector="반도체",
                    quantity=qty, price=price, thesis="테스트")


def _setup_account():
    save_account({"initial_capital": 100_000_000, "cash": 50_000_000})


def test_margin_zero_full_credit_no_cash_deduction():
    _setup_account()
    _process_and_save(_buy(), margin_ratio=0)
    h = next(x for x in load_holdings() if x["name"] == "테스트종목")
    assert h["credit_loan"] == 1_000_000  # 10 × 100,000 전액 신용
    assert load_account()["cash"] == 50_000_000  # 현금 차감 없음


def test_margin_45_partial_credit():
    _setup_account()
    _process_and_save(_buy(), margin_ratio=45)
    h = next(x for x in load_holdings() if x["name"] == "테스트종목")
    assert h["credit_loan"] == 1_000_000 * 0.55
    assert load_account()["cash"] == 50_000_000 - 1_000_000 * 0.45


def test_margin_100_cash_only():
    _setup_account()
    _process_and_save(_buy(), margin_ratio=100)
    h = next(x for x in load_holdings() if x["name"] == "테스트종목")
    assert h["credit_loan"] == 0
    assert load_account()["cash"] == 49_000_000


def test_margin_zero_transaction_persists_ratio():
    """margin_ratio=0 이 falsy 라고 기본값 100 으로 둔갑하면 안 된다."""
    _setup_account()
    _process_and_save(_buy(), margin_ratio=0)
    from storage.json_store import load_transactions
    tx = [t for t in load_transactions() if t.get("name") == "테스트종목"][-1]
    assert tx.get("margin_ratio") == 0
