"""회계 정본(canonical ledger) — 봇·PWA·카톡 세 경로가 공유하는 매매 산식.

같은 매도 산식이 세 곳(bot/handlers/sell.py · server/service.py ·
scripts/kakao_apply.py)에 "~과 동일"이라는 주석으로만 동기화돼 있었고,
실제로 봇 경로만 종목 매칭이 .lower()(공백 무시 안 함)로 어긋나 있었다
(2026-07-03 발견). 산식 수정은 반드시 여기서만 — 호출자는 입력 검증/응답
포맷/스킵 정책만 가진다.
"""
from __future__ import annotations

from models.portfolio import Holding
from models.transaction import Transaction
from parsers.input_parser import norm_stock_name
from storage import json_store

SELL_FEE_RATE = 0.002  # 매도세+수수료 근사


def _sell_by_account(holdings: list[dict], name: str, account: str, qty: int) -> None:
    """매도분을 해당 계좌의 by_account 분해에서 차감(평단 유지, 전량 시 항목 제거).

    그 계좌 분해 기록이 없으면 건너뛴다(스샷 reconcile 전 미기록).
    종목 전량매도로 holding 자체가 사라졌으면 분해도 함께 사라져 처리 불필요.
    """
    for h in holdings:
        if norm_stock_name(h.get("name", "")) != norm_stock_name(name):
            continue
        ba = h.get("by_account") or []
        ent = next((x for x in ba if x.get("account") == account), None)
        if not ent:
            return
        rem = int(ent.get("quantity", 0)) - qty
        if rem > 0:
            ent["quantity"] = rem
            ent["total_invested"] = ent.get("avg_price", 0) * rem
        else:
            ba.remove(ent)
        h["by_account"] = ba
        return


def sell_spot(
    name: str, quantity: int, price: float, *,
    reason: str = "", date: str = "", account: str = "",
) -> dict:
    """현물 매도 원장 반영(정본) — 보유 차감·융자 비례상환·예수금 가산·거래 기록.

    account 가 주어지면(카톡 자동반영 — 출처 증권사를 아는 유일한 경로) 그 계좌의
    by_account 보유 분해와 cash_by_account 예수금 버킷도 함께 델타 갱신한다.
    보유가 없거나 수량 초과면 ValueError — 정책(에러 응답/스킵/연금 orphan)은
    호출자가 결정한다. 반환: 기록된 거래 dict.
    """
    qty = int(quantity)
    price = float(price)
    if qty <= 0 or price <= 0:
        raise ValueError("수량/단가는 0보다 커야 합니다.")

    holdings = json_store.load_holdings()
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

    holding = Holding.from_dict(hd)
    loan_repay = holding.remove_sell(qty)
    if holding.quantity > 0:
        holdings[idx] = holding.to_dict()
    else:
        holdings.pop(idx)
    if account:
        _sell_by_account(holdings, name, account, qty)
    json_store.save_holdings(holdings)

    sell_cost = round(total * SELL_FEE_RATE)
    proceeds = total - sell_cost - loan_repay
    acc = json_store.load_account()
    if acc.get("initial_capital"):
        acc["cash"] = acc.get("cash", acc["initial_capital"]) + proceeds
        cba = acc.get("cash_by_account")
        if account and isinstance(cba, dict) and account in cba:
            cba[account] = float(cba[account] or 0) + proceeds
        json_store.save_account(acc)

    kwargs = {"date": date} if date else {}
    tx = Transaction(
        type="sell", name=name, sector=hd.get("sector", ""),
        price=price, quantity=qty, total_amount=total,
        profit_loss=pnl, profit_loss_pct=round(pnl_pct, 2),
        sell_reason=reason, holding_id=hd.get("id", ""),
        buy_thesis=hd.get("buy_thesis", ""), **kwargs,
    )
    txs = json_store.load_transactions()
    txs.append(tx.to_dict())
    json_store.save_transactions(txs)
    return tx.to_dict()
