"""PWA 백엔드용 거래 서비스 — 텔레그램 비의존.

봇 핸들러(bot/handlers/*)가 하던 매수/매도/회고 '쓰기'를 동일한 산식으로
순수 함수화한다. json_store(파일 락) + models 를 그대로 재사용하므로 카톡
자동반영·시세 갱신과 같은 데이터 스토어를 공유한다.

PWA-only 전환의 핵심: 이 레이어가 있으면 텔레그램 없이도 PWA → FastAPI →
이 함수들로 잔고를 쓸 수 있다.
"""
from __future__ import annotations

from datetime import datetime

from models.portfolio import Holding
from models.retrospective import Retrospective
from models.transaction import Transaction
from parsers.input_parser import norm_stock_name
from storage import json_store as store

SELL_FEE_RATE = 0.002  # 매도세+수수료 근사 (bot.handlers.sell 과 동일)


# ---------------------------------------------------------------------------
# 매수
# ---------------------------------------------------------------------------
def record_buy(
    name: str, quantity: int, price: float, *,
    sector: str = "", thesis: str = "", ticker: str = "",
    margin_ratio: int = 100, research_notes: str = "",
) -> dict:
    """현물 매수 기록. 봇 buy._process_and_save 와 동일 로직(평단 재계산·예수금 차감)."""
    name = (name or "").replace(" ", "")
    qty = int(quantity)
    price = float(price)
    if qty <= 0 or price <= 0:
        raise ValueError("수량/단가는 0보다 커야 합니다.")

    tx = Transaction(
        type="buy", name=name, sector=sector, price=price, quantity=qty,
        total_amount=price * qty, thesis=thesis, research_notes=research_notes,
        margin_ratio=margin_ratio,
    )

    holdings = store.load_holdings()
    idx = None
    if ticker:
        idx = next((i for i, h in enumerate(holdings) if h.get("ticker", "") == ticker), None)
    if idx is None:
        idx = next(
            (i for i, h in enumerate(holdings)
             if norm_stock_name(h.get("name", "")) == norm_stock_name(name)),
            None,
        )

    if idx is not None:
        h = Holding.from_dict(holdings[idx])
        h.add_buy(price, qty, tx.id, margin_ratio)
        if ticker and not h.ticker:
            h.ticker = ticker
        if sector:
            h.sector = sector
        if thesis:
            h.buy_thesis = thesis
        h.name = h.name.replace(" ", "")
        holdings[idx] = h.to_dict()
    else:
        buy_amount = price * qty
        credit_loan = buy_amount * (1 - margin_ratio / 100) if margin_ratio < 100 else 0.0
        h = Holding(
            name=name, ticker=ticker, sector=sector,
            buy_date=datetime.now().strftime("%Y-%m-%d"),
            avg_price=price, quantity=qty, total_invested=buy_amount,
            credit_loan=credit_loan, buy_thesis=thesis, research_notes=research_notes,
            transaction_ids=[tx.id],
        )
        holdings.append(h.to_dict())
    store.save_holdings(holdings)

    if ticker:
        tmap = store.load_ticker_map()
        tmap[name] = ticker
        store.save_ticker_map(tmap)

    txs = store.load_transactions()
    txs.append(tx.to_dict())
    store.save_transactions(txs)

    acc = store.load_account()
    if acc.get("initial_capital"):
        cash = acc.get("cash", acc["initial_capital"])
        acc["cash"] = cash - tx.total_amount * (margin_ratio / 100)
        store.save_account(acc)

    return tx.to_dict()


# ---------------------------------------------------------------------------
# 매도
# ---------------------------------------------------------------------------
def record_sell(name: str, quantity: int, price: float, *, reason: str = "") -> dict:
    """현물 매도 기록. 봇 sell 과 동일(보유 차감·예수금 가산−매도비용−대출상환·실현손익)."""
    qty = int(quantity)
    price = float(price)
    if qty <= 0 or price <= 0:
        raise ValueError("수량/단가는 0보다 커야 합니다.")

    holdings = store.load_holdings()
    idx = next(
        (i for i, h in enumerate(holdings)
         if norm_stock_name(h.get("name", "")) == norm_stock_name(name)),
        None,
    )
    if idx is None:
        raise ValueError(f"보유 종목이 없습니다: {name}")
    hd = holdings[idx]
    if qty > hd.get("quantity", 0):
        raise ValueError(f"보유량({hd.get('quantity', 0)})을 초과합니다.")

    avg = hd.get("avg_price", 0)
    total = price * qty
    pnl = (price - avg) * qty
    pnl_pct = (pnl / (avg * qty) * 100) if (avg and qty) else 0.0

    h = Holding.from_dict(hd)
    loan_repay = h.remove_sell(qty)
    if h.quantity > 0:
        holdings[idx] = h.to_dict()
    else:
        holdings.pop(idx)
    store.save_holdings(holdings)

    sell_cost = round(total * SELL_FEE_RATE)
    acc = store.load_account()
    if acc.get("initial_capital"):
        cash = acc.get("cash", acc["initial_capital"])
        acc["cash"] = cash + total - sell_cost - loan_repay
        store.save_account(acc)

    tx = Transaction(
        type="sell", name=name, sector=hd.get("sector", ""),
        price=price, quantity=qty, total_amount=total,
        profit_loss=pnl, profit_loss_pct=round(pnl_pct, 2),
        sell_reason=reason, holding_id=hd.get("id", ""),
        buy_thesis=hd.get("buy_thesis", ""),
    )
    txs = store.load_transactions()
    txs.append(tx.to_dict())
    store.save_transactions(txs)

    return tx.to_dict()


# ---------------------------------------------------------------------------
# 회고
# ---------------------------------------------------------------------------
def record_retro(
    transaction_id: str, *,
    thesis_correct: bool | None = None, what_went_well: str = "",
    regrets: str = "", avoidable: str = "", lessons: str = "",
) -> dict:
    """매도 거래에 회고를 연결. 봇 retro._save 와 동일."""
    txs = store.load_transactions()
    tx = next((t for t in txs if t.get("id") == transaction_id), None)
    if tx is None:
        raise ValueError("거래를 찾을 수 없습니다.")
    if tx.get("retrospective_id"):
        raise ValueError("이미 회고한 거래입니다.")

    retro = Retrospective(
        transaction_id=tx["id"], stock_name=tx.get("name", ""),
        sell_date=tx.get("date", ""), original_thesis=tx.get("buy_thesis", ""),
        thesis_correct=thesis_correct, what_went_well=what_went_well,
        regrets=regrets, avoidable=avoidable, lessons=lessons,
    )
    retros = store.load_retrospectives()
    retros.append(retro.to_dict())
    store.save_retrospectives(retros)

    for t in txs:
        if t["id"] == tx["id"]:
            t["retrospective_id"] = retro.id
            break
    store.save_transactions(txs)
    return retro.to_dict()


# ---------------------------------------------------------------------------
# 섹터 보정
# ---------------------------------------------------------------------------
def record_sector(sector: str, *, ticker: str = "", name: str = "") -> dict:
    """보유 종목의 섹터를 설정/수정. ticker 우선 매칭(미국주식), 없으면 name 매칭.

    save_holdings 가 대시보드 자동 재발행을 트리거하므로, 저장 즉시 '확인 필요'
    탭에서 해당 종목이 빠지고 비중 차트에 새 섹터가 반영된다.
    """
    sector = (sector or "").strip()
    if not sector:
        raise ValueError("섹터를 입력하세요.")

    holdings = store.load_holdings()
    idx = None
    if ticker:
        idx = next((i for i, h in enumerate(holdings) if h.get("ticker", "") == ticker), None)
    if idx is None and name:
        idx = next(
            (i for i, h in enumerate(holdings)
             if norm_stock_name(h.get("name", "")) == norm_stock_name(name)),
            None,
        )
    if idx is None:
        raise ValueError("보유 종목을 찾을 수 없습니다.")

    holdings[idx]["sector"] = sector
    store.save_holdings(holdings)
    return {
        "name": holdings[idx].get("name", ""),
        "ticker": holdings[idx].get("ticker", ""),
        "sector": sector,
    }


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------
def get_state() -> dict:
    """PWA 초기 로드용 상태 — 보유/계좌/최근거래/미회고 매도."""
    holdings = [h for h in store.load_holdings() if h.get("quantity", 0) > 0]
    acc = store.load_account()
    txs = store.load_transactions()
    recent = sorted(txs, key=lambda t: t.get("date", ""), reverse=True)[:30]
    unreviewed = [
        t for t in txs if t.get("type") == "sell" and not t.get("retrospective_id")
    ]
    return {
        "holdings": holdings,
        "account": {k: acc.get(k) for k in ("initial_capital", "cash", "futures_cash")},
        "recent_transactions": recent,
        "unreviewed_sells": unreviewed,
    }
