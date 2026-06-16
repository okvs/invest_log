"""카톡 체결 알림 → 잔고 자동반영(scripts/kakao_apply.py) 테스트.

수동 봇 경로(parse_broker_message)와 동일한 산식으로 반영되는지,
선물 방향 자동판정/부분·전량·초과 청산/추가진입/신규skip,
주식 매수/매도/초과·미보유, 비거래 메시지 None 처리를 검증한다.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import kakao_apply as ka  # noqa: E402

from storage.json_store import (  # noqa: E402
    load_account,
    load_futures_positions,
    load_futures_transactions,
    load_holdings,
    load_transactions,
    save_account,
    save_futures_positions,
    save_holdings,
)


# ---------------------------------------------------------------------------
# 메시지 픽스처
# ---------------------------------------------------------------------------
def kb_fut(side: str, name: str, cm: str, qty: int, amount: int, mult: int = 10) -> str:
    content = "매도체결" if side == "sell" else "매수체결"
    return (
        "[KB증권] 선물옵션 체결 안내\n\n"
        f"고객님, 주문하신 {name} F {cm} (  {mult}) 선물옵션이 체결됐으니 확인해주세요.\n\n"
        "■ 계좌: 384-***-*28 [01] \n"
        f"■ 종목명: {name} F {cm} (  {mult}) \n"
        f"■ 주문수량: {qty}계약 \n"
        f"■ 체결금액: {amount:,}원\n"
        f"■ 내용: {content}(1234)"
    )


def kb_stock(side: str, name: str, qty: int, price: int) -> str:
    content = "매도체결" if side == "sell" else "매수체결"
    return (
        "[KB증권] 주식 체결 안내\n\n"
        f"고객님, 주문하신 {name} 주식이 체결됐으니 확인해주세요.\n\n"
        "■ 계좌: 277-***-*12 [01] \n"
        f"■ 종목명: {name} \n"
        f"■ 주문수량: {qty}주 \n"
        f"■ 체결금액: {price:,}원 \n"
        f"■ 내용: {content}(20113711)"
    )


def shinhan_stock(side: str, name: str, code: str, qty: int, price: int) -> str:
    content = "매도체결" if side == "sell" else "매수체결"
    return (
        "계좌명 : 정승민\n"
        "계좌번호 : 270-82-8***75\n"
        f"종목명 : {name}\n"
        f"종목코드 : {code}\n"
        f"체결구분 : {content}\n"
        f"체결수량 : {qty}주\n"
        f"체결단가 : {price}원\n"
        "-------------------------------\n"
        f"주문수량 : {qty}주"
    )


def seed_sk_long(contracts: int = 3) -> None:
    """SK하이닉스 long 포지션 + 계좌 시드 (실제 06-16 상태)."""
    save_futures_positions([{
        "id": "pos-sk",
        "name": "SK하이닉스",
        "symbol": "000660",
        "contract_code": "",
        "contract_month": "202607",
        "expiry_date": "2026-07-09",
        "direction": "long",
        "contracts": contracts,
        "multiplier": 10,
        "avg_entry_price": 1984000.0,
        "initial_margin": 21962880.0,
        "maintenance_margin": 0.0,
        "entry_date": "2026-06-11",
        "sector": "반도체",
        "thesis": "에이전트 토큰 롱",
        "transaction_ids": [],
    }])
    save_account({
        "initial_capital": 155000000.0,
        "cash": 56874048,
        "futures_cash": 37480762.0,
    })


# ---------------------------------------------------------------------------
# 선물 청산
# ---------------------------------------------------------------------------
def test_futures_close_partial():
    seed_sk_long(3)
    res = ka.apply_message(kb_fut("sell", "SK하이닉스", "202607", 2, 2393000))
    assert res is not None and res.applied
    assert res.action == "선물청산"

    positions = load_futures_positions()
    assert len(positions) == 1
    assert positions[0]["contracts"] == 1
    # 증거금 비례 환급 후 잔여 = 21,962,880 * 1/3
    assert positions[0]["initial_margin"] == pytest.approx(7320960.0)

    txs = load_futures_transactions()
    assert len(txs) == 1
    assert txs[0]["type"] == "close"
    assert txs[0]["pnl"] == pytest.approx(8180000.0)   # (2,393,000-1,984,000)*2*10
    assert txs[0]["reason"] == ka.APPLY_REASON

    # 선물 가용예수금 += 환급증거금 + 실현손익
    acc = load_account()
    assert acc["futures_cash"] == pytest.approx(37480762.0 + 14641920.0 + 8180000.0)


def test_futures_close_full_removes_position():
    seed_sk_long(3)
    res = ka.apply_message(kb_fut("sell", "SK하이닉스", "202607", 3, 2393000))
    assert res.applied
    assert load_futures_positions() == []   # 전량청산 → 포지션 제거
    acc = load_account()
    # pnl=(2,393,000-1,984,000)*3*10=12,270,000, 환급=21,962,880 전액
    assert acc["futures_cash"] == pytest.approx(37480762.0 + 21962880.0 + 12270000.0)


def test_futures_close_oversize_warns_and_closes_held():
    seed_sk_long(3)
    res = ka.apply_message(kb_fut("sell", "SK하이닉스", "202607", 5, 2393000))
    assert res.applied
    assert "초과" in res.warning
    assert load_futures_positions() == []   # 보유 3계약만 청산 → 제거
    txs = load_futures_transactions()
    assert txs[0]["contracts"] == 3


def test_futures_add_same_direction():
    seed_sk_long(3)
    res = ka.apply_message(kb_fut("buy", "SK하이닉스", "202607", 2, 2000000))
    assert res.applied
    assert res.action == "선물추가진입"
    positions = load_futures_positions()
    assert positions[0]["contracts"] == 5
    # 증거금 추정만큼 선물 가용예수금 차감 (감소)
    assert load_account()["futures_cash"] < 37480762.0


def test_futures_new_is_skipped():
    seed_sk_long(3)
    res = ka.apply_message(kb_fut("buy", "삼성SDI", "202609", 1, 5000000))
    assert res is not None and not res.applied
    assert res.action == "skip-신규선물"
    # 데이터 무변동
    assert len(load_futures_positions()) == 1
    assert load_futures_transactions() == []


def test_futures_dry_run_no_write():
    seed_sk_long(3)
    res = ka.apply_message(kb_fut("sell", "SK하이닉스", "202607", 2, 2393000), dry_run=True)
    assert res.applied                       # 결과는 계산되지만
    assert load_futures_positions()[0]["contracts"] == 3   # 저장 안 됨
    assert load_futures_transactions() == []
    assert load_account()["futures_cash"] == pytest.approx(37480762.0)


# ---------------------------------------------------------------------------
# 주식
# ---------------------------------------------------------------------------
def test_stock_buy_new_holding():
    save_account({"initial_capital": 155000000.0, "cash": 100000000.0})
    save_holdings([])
    res = ka.apply_message(shinhan_stock("buy", "삼성전기", "009150", 10, 1761000))
    assert res.applied and res.action == "주식매수"
    holdings = load_holdings()
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 10
    assert holdings[0]["avg_price"] == 1761000
    # 현금 100% 차감
    assert load_account()["cash"] == pytest.approx(100000000.0 - 17610000)


def test_stock_buy_adds_to_existing_avg():
    save_account({"initial_capital": 155000000.0, "cash": 100000000.0})
    save_holdings([{
        "id": "h1", "name": "삼성전기", "ticker": "", "sector": "반도체",
        "buy_date": "2026-06-01", "avg_price": 1700000, "quantity": 10,
        "total_invested": 17000000.0, "credit_loan": 0.0, "buy_thesis": "MLCC",
        "research_notes": "", "transaction_ids": [],
    }])
    res = ka.apply_message(kb_stock("buy", "삼성전기", 10, 1800000))
    assert res.applied
    h = load_holdings()[0]
    assert h["quantity"] == 20
    assert h["avg_price"] == round((17000000 + 18000000) / 20)  # 평단 재계산
    assert h["sector"] == "반도체"   # 기존 섹터 유지


def test_stock_sell_partial():
    save_account({"initial_capital": 155000000.0, "cash": 50000000.0})
    save_holdings([{
        "id": "h1", "name": "삼성전기", "ticker": "", "sector": "반도체",
        "buy_date": "2026-06-01", "avg_price": 1700000, "quantity": 10,
        "total_invested": 17000000.0, "credit_loan": 0.0, "buy_thesis": "MLCC",
        "research_notes": "", "transaction_ids": [],
    }])
    res = ka.apply_message(kb_stock("sell", "삼성전기", 5, 1800000))
    assert res.applied and res.action == "주식매도"
    h = load_holdings()[0]
    assert h["quantity"] == 5
    total = 1800000 * 5
    fee = round(total * ka.SELL_FEE_RATE)
    assert load_account()["cash"] == pytest.approx(50000000.0 + total - fee)
    tx = load_transactions()[-1]
    assert tx["type"] == "sell"
    assert tx["profit_loss"] == pytest.approx((1800000 - 1700000) * 5)


def test_stock_sell_no_holding_is_skipped():
    save_account({"initial_capital": 155000000.0, "cash": 50000000.0})
    save_holdings([])
    res = ka.apply_message(kb_stock("sell", "없는종목", 5, 1000))
    assert res is not None and not res.applied
    assert load_transactions() == []


def test_stock_sell_oversize_is_skipped():
    save_account({"initial_capital": 155000000.0, "cash": 50000000.0})
    save_holdings([{
        "id": "h1", "name": "삼성전기", "ticker": "", "sector": "반도체",
        "buy_date": "2026-06-01", "avg_price": 1700000, "quantity": 3,
        "total_invested": 5100000.0, "credit_loan": 0.0, "buy_thesis": "",
        "research_notes": "", "transaction_ids": [],
    }])
    res = ka.apply_message(kb_stock("sell", "삼성전기", 5, 1800000))
    assert not res.applied
    assert load_holdings()[0]["quantity"] == 3   # 무변동


# ---------------------------------------------------------------------------
# 비거래 / 파싱 실패
# ---------------------------------------------------------------------------
def test_non_trade_message_returns_none():
    deposit = (
        "[KB증권] 입출금 안내\n\n"
        "고객님 계좌에 입금이 완료되었습니다.\n"
        "■ 금액: 1,000,000원"
    )
    assert ka.apply_message(deposit) is None


def test_unsupported_message_returns_none():
    assert ka.apply_message("그냥 광고 메시지입니다") is None
