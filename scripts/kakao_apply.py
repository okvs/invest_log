#!/usr/bin/env python3
"""증권사 카카오톡 체결 알림 → invest_log 잔고 자동 반영.

맥 카톡 로컬 DB(KB증권/신한투자증권 채널)에 새로 도착한 체결 알림을 읽어
**수동으로 봇에 붙여넣는 것과 동일한 경로**(parsers.input_parser.parse_broker_message)로
파싱해 포트폴리오/거래/예수금에 그대로 반영한다.

- 주식 매수  → 보유/평단/예수금 갱신 (bot.handlers.buy._process_and_save 재사용, 증거금 100% 기본)
- 주식 매도  → 보유 차감·예수금 가산(매도비용 0.2%·대출 비례상환)·실현손익 기록
- 선물       → 보유 포지션 대조로 방향 자동판정(broker.py와 동일 규칙):
                 · 반대방향 보유 → 청산(부분/전량, 실현손익+환급증거금 → 선물 가용예수금)
                 · 같은방향 보유 → 추가진입(증거금률은 기존 포지션 기준 추정)
                 · 보유 없음     → **신규진입은 자동반영하지 않고 텔레그램 경고만** 보냄
                   (증거금률·만기·사유가 필요해 수동 기록이 안전 — 봇에 메시지 붙여넣기)

dedup: 채널별 watermark(logId)를 data/kakao_apply_state.json 에 저장. 텔레그램
포워더(kakao_forward_state.json)와 **별개 상태**라 서로 간섭하지 않는다.

⚠️ 자동반영을 켜면 **같은 거래를 봇에 수동으로도 기록하면 이중계상**된다.
   이 데몬을 쓰는 동안에는 수동 기록을 멈출 것.

자격증명: 기존 invest_log 봇 재사용 (.env BOT_TOKEN, data/account.json chat_id).
실행 파이썬: .venv (telegram/filelock 등 필요).

사용 예:
  python3 scripts/kakao_apply.py --init                 # baseline(과거 미반영, 전송X)
  python3 scripts/kakao_apply.py --from-today --dry-run  # 오늘 0시~ 반영 미리보기(쓰기X)
  python3 scripts/kakao_apply.py --from-today            # 오늘 0시~ 미반영분 실제 반영
  python3 scripts/kakao_apply.py                         # 1회 폴링(watermark 이후 반영)
  python3 scripts/kakao_apply.py --loop 60               # 60초 데몬
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (PROJECT_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 적용 로직은 봇 수동 경로와 동일한 파서/모델/스토리지를 그대로 재사용한다.
from parsers.input_parser import (  # noqa: E402
    BrokerMessage,
    BuyInput,
    FuturesBrokerMessage,
    parse_broker_message,
    resolve_name,
)
from models.futures_position import FuturesPosition  # noqa: E402
from models.futures_transaction import FuturesTransaction  # noqa: E402
from models.portfolio import Holding  # noqa: E402
from models.transaction import Transaction  # noqa: E402
from storage.json_store import (  # noqa: E402
    adjust_futures_cash,
    load_account,
    load_futures_positions,
    load_futures_transactions,
    load_holdings,
    load_margin_rate_pool,
    load_nickname_map,
    load_transactions,
    save_account,
    save_futures_positions,
    save_futures_transactions,
    save_holdings,
    save_transactions,
)

KST = timezone(timedelta(hours=9))
SELL_FEE_RATE = 0.002  # 매도세+수수료 근사 (bot.handlers.sell 과 동일)
APPLY_REASON = "카톡 자동반영"

STATE_FILE = os.path.join(PROJECT_ROOT, "data", "kakao_apply_state.json")
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "kakao_apply.log")

# 적용 대상 채널 (포워더와 동일 — 이름으로 재확인 후 폴백 id 사용)
TARGETS = {
    "KB증권": 4803250456343651,
    "신한투자증권": 4697684299181193,
    "NH투자증권": 4739904926139546,   # 나무 — 미국주식(USD)
}


def log(msg: str) -> None:
    line = f"{datetime.now(KST).isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _norm(s: str) -> str:
    return (s or "").replace(" ", "").casefold()


def _to_iso(ts_kst: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' → ISO. 비면 현재시각."""
    ts_kst = (ts_kst or "").strip()
    if ts_kst:
        try:
            return datetime.strptime(ts_kst, "%Y-%m-%d %H:%M:%S").isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.now(KST).replace(tzinfo=None).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 적용 결과
# ---------------------------------------------------------------------------
@dataclass
class ApplyResult:
    applied: bool
    action: str       # 주식매수 | 주식매도 | 선물청산 | 선물추가진입 | skip-신규선물 | skip
    summary: str
    warning: str = ""


# ---------------------------------------------------------------------------
# 계좌(KB/신한)별 by_account 분해 갱신 — 현물만. 카톡은 어느 증권사인지 알므로
# 체결을 해당 계좌에 귀속시켜 백데이터(Holding.by_account)를 유지한다.
# PWA/대시보드는 종목별 합산으로 통합표시(by_account는 표시에 쓰지 않음).
# ---------------------------------------------------------------------------
_ACCOUNT_BY_BROKER = {"KB증권": "KB", "신한투자증권": "신한"}


def _account_for_broker(broker: str) -> str:
    return _ACCOUNT_BY_BROKER.get(broker, "")


def _update_by_account_buy(name: str, account: str, qty: int, price: float) -> None:
    """매수분을 해당 계좌의 by_account 에 가산(평단 재계산)."""
    if not account:
        return
    holdings = load_holdings()
    for h in holdings:
        if _norm(h.get("name", "")) != _norm(name):
            continue
        ba = h.get("by_account") or []
        ent = next((x for x in ba if x.get("account") == account), None)
        amt = price * qty
        if ent:
            nq = int(ent.get("quantity", 0)) + qty
            prev = float(ent.get("total_invested") or ent.get("avg_price", 0) * ent.get("quantity", 0))
            nt = prev + amt
            ent["quantity"] = nq
            ent["total_invested"] = nt
            ent["avg_price"] = round(nt / nq) if nq else 0
        else:
            ba.append({"account": account, "quantity": qty,
                       "avg_price": round(price), "total_invested": amt, "funding": ""})
        h["by_account"] = ba
        save_holdings(holdings)
        return


def _update_by_account_sell(name: str, account: str, qty: int) -> None:
    """매도분을 해당 계좌의 by_account 에서 차감(평단 유지, 전량 시 항목 제거).

    해당 계좌에 by_account 기록이 없으면 건너뛴다(아직 미기록 — 추후 reconcile).
    종목 전량매도로 holding 자체가 사라졌으면 by_account도 함께 사라져 처리 불필요.
    """
    if not account:
        return
    holdings = load_holdings()
    for h in holdings:
        if _norm(h.get("name", "")) != _norm(name):
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
        save_holdings(holdings)
        return


# ---------------------------------------------------------------------------
# 주식
# ---------------------------------------------------------------------------
def _apply_stock(msg: BrokerMessage, *, ts_kst: str, dry_run: bool, account: str = "") -> ApplyResult:
    name = msg.name
    qty = int(msg.quantity)
    price = float(msg.price)
    total = price * qty

    if msg.trade_type == "buy":
        holdings = load_holdings()
        existing = next(
            (h for h in holdings if _norm(h.get("name", "")) == _norm(name)), None
        )
        summary = f"주식 매수 {name} {qty}주 @ {int(price):,}원 (총 {int(total):,}원)"
        warns = []
        if not existing:
            warns.append("신규 종목 — 섹터/매수사유 비어있음('수정'으로 보완)")
        warns.append("현금매수(증거금100%)로 기록 — 신용매수였다면 '융자'로 보정")
        if not dry_run:
            # 봇 수동 매수와 완전히 같은 경로 (평단 재계산·ticker_map·예수금 차감)
            from bot.handlers.buy import _process_and_save  # lazy: telegram import 회피
            buy_input = BuyInput(
                name=name,
                ticker=(existing or {}).get("ticker", ""),
                sector=(existing or {}).get("sector", ""),
                quantity=qty,
                price=price,
                thesis=(existing or {}).get("buy_thesis", ""),
            )
            _process_and_save(buy_input, margin_ratio=100)
            _update_by_account_buy(name, account, qty, price)  # 계좌별 분해 갱신
        if account:
            summary += f" · [{account}]"
        return ApplyResult(True, "주식매수", summary, " · ".join(warns))

    # sell
    holdings = load_holdings()
    hd = next((h for h in holdings if _norm(h.get("name", "")) == _norm(name)), None)
    if hd is None:
        # 보유 종목 없음 — 추적 안 하던 종목의 매도(연금계좌 전량매도 등).
        # 손익/예수금 계산 없이 '연금' orphan 거래로만 남겨 기록 탭에 보이게 한다.
        # (일반계좌 거래였다면 기록 탭에서 '연금' 칩을 꺼 일반으로 돌릴 수 있음)
        summary = f"주식 매도 {name} {qty}주 @ {int(price):,}원 · 연금(보유없음)·기록만"
        if not dry_run:
            tx = Transaction(
                type="sell", name=name, sector="",
                price=price, quantity=qty, total_amount=total,
                profit_loss=0.0, profit_loss_pct=0.0,
                sell_reason=APPLY_REASON, date=_to_iso(ts_kst),
                is_pension=True, orphan=True,
            )
            txs = load_transactions()
            txs.append(tx.to_dict())
            save_transactions(txs)
        if account:
            summary += f" · [{account}]"
        return ApplyResult(
            True, "연금매도", summary,
            "보유 없던 종목 — 연금으로 기록(보유/예수금/손익 미반영). 일반거래면 기록 탭에서 연금 해제",
        )
    if qty > hd.get("quantity", 0):
        return ApplyResult(
            False, "skip",
            f"주식 매도 {name} {qty}주 @ {int(price):,}원",
            f"보유 {hd.get('quantity', 0)}주 초과 → 미반영(수동 확인 필요)",
        )

    avg = hd.get("avg_price", 0)
    pnl = (price - avg) * qty
    pnl_pct = (pnl / (avg * qty) * 100) if (avg and qty) else 0.0
    summary = (
        f"주식 매도 {name} {qty}주 @ {int(price):,}원 · "
        f"손익 {int(pnl):+,}원 ({pnl_pct:+.2f}%)"
    )
    if not dry_run:
        holding = Holding.from_dict(hd)
        loan_repay = holding.remove_sell(qty)
        new_holdings = []
        for h in holdings:
            if _norm(h.get("name", "")) == _norm(name):
                if holding.quantity > 0:
                    new_holdings.append(holding.to_dict())
            else:
                new_holdings.append(h)
        save_holdings(new_holdings)

        sell_cost = round(total * SELL_FEE_RATE)
        acc = load_account()
        if acc.get("initial_capital"):
            cash = acc.get("cash", acc["initial_capital"])
            acc["cash"] = cash + total - sell_cost - loan_repay
            save_account(acc)

        tx = Transaction(
            type="sell", name=name, sector=hd.get("sector", ""),
            price=price, quantity=qty, total_amount=total,
            profit_loss=pnl, profit_loss_pct=round(pnl_pct, 2),
            sell_reason=APPLY_REASON, holding_id=hd.get("id", ""),
            buy_thesis=hd.get("buy_thesis", ""), date=_to_iso(ts_kst),
        )
        txs = load_transactions()
        txs.append(tx.to_dict())
        save_transactions(txs)
        _update_by_account_sell(name, account, qty)  # 계좌별 분해 차감
    if account:
        summary += f" · [{account}]"
    return ApplyResult(True, "주식매도", summary, "")


# ---------------------------------------------------------------------------
# 미국주식 (나무/NH) — USD. 티커로 매칭, account.usd_cash(미국 예수금) 가감.
# ---------------------------------------------------------------------------
def _find_us(holdings: list[dict], ticker: str) -> dict | None:
    tk = (ticker or "").upper()
    for h in holdings:
        if h.get("currency") == "USD" and (h.get("ticker", "") or "").upper() == tk:
            return h
    return None


def _adjust_usd_cash(delta: float) -> None:
    acc = load_account()
    acc["usd_cash"] = float(acc.get("usd_cash", 0) or 0) + delta
    save_account(acc)


def _apply_us_stock(msg: BrokerMessage, *, ts_kst: str, dry_run: bool) -> ApplyResult:
    ticker = (msg.ticker or "").upper()
    qty = int(msg.quantity)
    price = float(msg.price)        # USD 주당
    total = price * qty             # USD
    holdings = load_holdings()
    existing = _find_us(holdings, ticker)

    if msg.trade_type == "buy":
        summary = f"미국 매수 {ticker} {qty}주 @ ${price:,.2f} (${total:,.2f})"
        warns = []
        if not existing:
            warns.append("신규 미국종목 — 섹터 '미국주식' 기본('수정'으로 보완)")
        if not dry_run:
            tx = Transaction(
                type="buy", name=msg.name, sector=(existing or {}).get("sector", "미국주식"),
                price=price, quantity=qty, total_amount=total, currency="USD",
                date=_to_iso(ts_kst),
            )
            if existing:
                idx = holdings.index(existing)
                h = Holding.from_dict(existing)
                h.add_buy(price, qty, tx.id, margin_ratio=100)  # USD 평단 재계산
                if not h.ticker:
                    h.ticker = ticker
                holdings[idx] = h.to_dict()
            else:
                h = Holding(
                    name=msg.name, sector="미국주식", buy_date=_to_iso(ts_kst)[:10],
                    avg_price=price, quantity=qty, total_invested=total,
                    ticker=ticker, currency="USD", transaction_ids=[tx.id],
                )
                holdings.append(h.to_dict())
            save_holdings(holdings)
            _adjust_usd_cash(-total)         # 미국 예수금 차감(USD)
            txs = load_transactions()
            txs.append(tx.to_dict())
            save_transactions(txs)
        return ApplyResult(True, "미국매수", summary, " · ".join(warns))

    # sell
    if existing is None:
        return ApplyResult(
            False, "skip", f"미국 매도 {ticker} {qty}주 @ ${price:,.2f}",
            "보유 미국종목 없음 → 미반영(수동 확인)",
        )
    if qty > int(existing.get("quantity", 0)):
        return ApplyResult(
            False, "skip", f"미국 매도 {ticker} {qty}주 @ ${price:,.2f}",
            f"보유 {existing.get('quantity', 0)}주 초과 → 미반영(수동 확인)",
        )
    avg = float(existing.get("avg_price", 0) or 0)
    pnl = (price - avg) * qty                # USD
    pnl_pct = (pnl / (avg * qty) * 100) if (avg and qty) else 0.0
    summary = (
        f"미국 매도 {ticker} {qty}주 @ ${price:,.2f} · "
        f"손익 ${pnl:+,.2f} ({pnl_pct:+.2f}%)"
    )
    if not dry_run:
        h = Holding.from_dict(existing)
        h.remove_sell(qty)                   # 미국은 credit_loan 0 → 단순 차감
        new_holdings = []
        for x in holdings:
            if x.get("currency") == "USD" and (x.get("ticker", "") or "").upper() == ticker:
                if h.quantity > 0:
                    new_holdings.append(h.to_dict())
            else:
                new_holdings.append(x)
        save_holdings(new_holdings)
        _adjust_usd_cash(total)              # 매도대금 미국 예수금 가산(USD)
        tx = Transaction(
            type="sell", name=msg.name, sector=existing.get("sector", "미국주식"),
            price=price, quantity=qty, total_amount=total, profit_loss=pnl,
            profit_loss_pct=round(pnl_pct, 2), sell_reason=APPLY_REASON,
            holding_id=existing.get("id", ""), currency="USD", date=_to_iso(ts_kst),
        )
        txs = load_transactions()
        txs.append(tx.to_dict())
        save_transactions(txs)
    return ApplyResult(True, "미국매도", summary, "")


# ---------------------------------------------------------------------------
# 선물
# ---------------------------------------------------------------------------
def _find_fut_pos(name: str, cm: str, direction: str) -> dict | None:
    nk = _norm(name)
    for p in load_futures_positions():
        if p.get("contracts", 0) <= 0:
            continue
        if (
            _norm(p.get("name", "")) == nk
            and p.get("contract_month") == cm
            and p.get("direction") == direction
        ):
            return p
    return None


def _resolve_futures_action(msg: FuturesBrokerMessage) -> tuple[str, str, dict | None]:
    """broker.py._resolve_futures_action 과 동일 규칙. (action, direction, existing)."""
    name, cm = msg.name, msg.contract_month
    if msg.trade_type == "buy":
        s = _find_fut_pos(name, cm, "short")
        if s:
            return "close", "short", s
        l = _find_fut_pos(name, cm, "long")
        if l:
            return "add", "long", l
        return "new", "long", None
    else:
        l = _find_fut_pos(name, cm, "long")
        if l:
            return "close", "long", l
        s = _find_fut_pos(name, cm, "short")
        if s:
            return "add", "short", s
        return "new", "short", None


def _cm_label(cm: str) -> str:
    return f"{cm[2:4]}년{cm[4:6]}월물" if len(cm) == 6 else cm


def _apply_futures(msg: FuturesBrokerMessage, *, ts_kst: str, dry_run: bool) -> ApplyResult:
    action, direction, existing = _resolve_futures_action(msg)
    price = msg.price_per_share()          # KB '체결금액' = 주당단가(10배 우주, avg_entry와 동일 스케일)
    n = int(msg.quantity)
    dir_kr = "롱" if direction == "long" else "숏"

    if action == "close":
        positions = load_futures_positions()
        idx = next(i for i, p in enumerate(positions) if p["id"] == existing["id"])
        pos = FuturesPosition.from_dict(positions[idx])
        held = pos.contracts
        close_n = min(n, held)
        notional_before = price * close_n * pos.multiplier
        pnl, margin_release, _ = pos.close(price=price, contracts=close_n)  # pos 변형(dry-run이면 미저장)
        pnl_pct = (pnl / notional_before * 100) if notional_before else 0.0
        summary = (
            f"선물 청산 {msg.name} {dir_kr} {close_n}계약 @ {int(price):,}원 · "
            f"손익 {int(pnl):+,}원 ({pnl_pct:+.2f}%) · 환급증거금 {int(margin_release):,}원"
        )
        warning = ""
        if n > held:
            warning = (
                f"체결 {n}계약 > 보유 {held}계약 — {held}계약만 청산, "
                f"초과 {n - held}계약 미반영(방향전환?·수동 확인)"
            )
        if not dry_run:
            tx = FuturesTransaction(
                type="close", name=pos.name, symbol=pos.symbol,
                contract_code=pos.contract_code, contract_month=pos.contract_month,
                expiry_date=pos.expiry_date, direction=pos.direction,
                contracts=close_n, price=price, multiplier=pos.multiplier,
                margin=margin_release, sector=pos.sector, reason=APPLY_REASON,
                position_id=pos.id, pnl=pnl, pnl_pct=round(pnl_pct, 2),
                buy_thesis=pos.thesis, date=_to_iso(ts_kst),
            )
            if pos.contracts <= 0:
                positions.pop(idx)
            else:
                positions[idx] = pos.to_dict()
            save_futures_positions(positions)
            adjust_futures_cash(margin_release + pnl)
            txs = load_futures_transactions()
            txs.append(tx.to_dict())
            save_futures_transactions(txs)
        return ApplyResult(True, "선물청산", summary, warning)

    if action == "add":
        positions = load_futures_positions()
        idx = next(i for i, p in enumerate(positions) if p["id"] == existing["id"])
        pos = FuturesPosition.from_dict(positions[idx])
        notional = pos.avg_entry_price * pos.contracts * pos.multiplier
        rate = (pos.initial_margin / notional) if notional > 0 else (
            (load_margin_rate_pool() or [0.369])[0]
        )
        margin = price * n * pos.multiplier * rate
        summary = (
            f"선물 추가진입 {msg.name} {dir_kr} {n}계약 @ {int(price):,}원 · "
            f"증거금≈{int(margin):,}원(률 {rate * 100:.1f}% 추정)"
        )
        warning = "증거금률은 기존 포지션 기준 추정값 — 실제와 다르면 예수금 보정 필요"
        if not dry_run:
            tx = FuturesTransaction(
                type="open", name=pos.name, symbol=pos.symbol,
                contract_code=pos.contract_code, contract_month=pos.contract_month,
                expiry_date=pos.expiry_date, direction=pos.direction,
                contracts=n, price=price, multiplier=pos.multiplier,
                margin=margin, sector=pos.sector, thesis=pos.thesis,
                reason="", position_id=pos.id, date=_to_iso(ts_kst),
            )
            pos.add_entry(price=price, contracts=n, margin=margin, transaction_id=tx.id)
            positions[idx] = pos.to_dict()
            save_futures_positions(positions)
            adjust_futures_cash(-margin)
            txs = load_futures_transactions()
            txs.append(tx.to_dict())
            save_futures_transactions(txs)
        return ApplyResult(True, "선물추가진입", summary, warning)

    # new — 자동반영하지 않음(증거금률·만기·사유 필요). 텔레그램 경고만.
    summary = (
        f"선물 신규 {dir_kr} 진입 {msg.name} {_cm_label(msg.contract_month)} "
        f"{n}계약 @ {int(price):,}원"
    )
    warning = (
        "신규 선물 진입은 자동반영 제외 — 증거금률·진입사유가 필요합니다. "
        "봇에 이 메시지를 붙여넣어 '선물진입'으로 직접 기록해주세요."
    )
    return ApplyResult(False, "skip-신규선물", summary, warning)


# ---------------------------------------------------------------------------
# 메시지 → 적용
# ---------------------------------------------------------------------------
def apply_message(detail: str, *, ts_kst: str = "", dry_run: bool = False,
                  account: str = "") -> ApplyResult | None:
    """카톡 1건(원문 상세)을 적용. 체결이 아니면(파싱 실패) None 반환.

    account: 출처 계좌(KB/신한) — 현물 by_account 분해 갱신용(선물은 미사용).
    """
    try:
        msg = parse_broker_message(detail)
    except ValueError:
        return None  # 입출금/안내/비지원 등 — 적용 대상 아님
    if isinstance(msg, FuturesBrokerMessage):
        msg.name = resolve_name(msg.name, nickname_map=load_nickname_map())
        return _apply_futures(msg, ts_kst=ts_kst, dry_run=dry_run)
    if getattr(msg, "currency", "KRW") == "USD":
        # 미국주식(나무/NH) — 티커로 매칭, USD 예수금(usd_cash) 가감
        return _apply_us_stock(msg, ts_kst=ts_kst, dry_run=dry_run)
    msg.name = resolve_name(msg.name, nickname_map=load_nickname_map())
    return _apply_stock(msg, ts_kst=ts_kst, dry_run=dry_run, account=account)


# ---------------------------------------------------------------------------
# 상태(watermark)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 텔레그램 확인 메시지
# ---------------------------------------------------------------------------
def _confirm_text(broker: str, ts: str, res: ApplyResult) -> str:
    head = "✅ 잔고 자동반영 · " + broker + (f" · {ts} KST" if ts else "")
    body = f"{res.action}\n{res.summary}"
    if res.warning:
        body += f"\n⚠️ {res.warning}"
    return f"{head}\n{'─' * 20}\n{body}"


def _alert_text(broker: str, ts: str, res: ApplyResult) -> str:
    head = "⚠️ 자동반영 보류 · " + broker + (f" · {ts} KST" if ts else "")
    return f"{head}\n{'─' * 20}\n{res.summary}\n{res.warning}"


# ---------------------------------------------------------------------------
# 1회 폴링
# ---------------------------------------------------------------------------
def poll_once(
    token: str | None,
    chat_id: int | None,
    *,
    dry_run: bool = False,
    from_today_epoch: int | None = None,
    init_only: bool = False,
) -> list[tuple[str, int, str, ApplyResult]]:
    from kakao_trade_preview import find_kakaocli, load_auth, kc_query, detail_text
    from kakao_to_telegram import resolve_targets, current_max, tg_send

    cli = find_kakaocli()
    db, key = load_auth()
    targets = resolve_targets(cli, db, key)
    state = load_state()
    results: list[tuple[str, int, str, ApplyResult]] = []

    for broker, cid in targets.items():
        ks = str(cid)
        last = int(state.get(ks, -1))

        if init_only:
            mx = current_max(cli, db, key, cid)
            state[ks] = mx
            log(f"[{broker}] baseline 설정 logId={mx} (반영 생략)")
            continue

        # 첫 부트스트랩: '오늘 0시 이전'의 마지막 logId 로 watermark 시드
        if last < 0 and from_today_epoch is not None:
            rows = kc_query(
                cli, db, key,
                f"SELECT MAX(logId) FROM NTChatMessage "
                f"WHERE chatId={cid} AND sentAt < {from_today_epoch}",
            )
            last = int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0
            log(f"[{broker}] from-today watermark={last} (오늘 0시 이전 마지막)")
        elif last < 0:
            # init/from-today 없이 첫 실행 → baseline 만 잡고 과거 미반영
            mx = current_max(cli, db, key, cid)
            if not dry_run:
                state[ks] = mx
            log(f"[{broker}] baseline 설정 logId={mx} (반영 생략 — --from-today 로 오늘분 반영)")
            continue

        rows = kc_query(
            cli, db, key,
            f"SELECT logId, sentAt, message, attachment FROM NTChatMessage "
            f"WHERE chatId={cid} AND logId > {last} ORDER BY logId ASC LIMIT 100",
        )
        max_seen = last
        n_applied = 0
        for log_id, _sent_at, message, attachment in rows:
            max_seen = max(max_seen, int(log_id))
            detail, sent_kst = detail_text(message, attachment)
            if not detail or not detail.strip():
                continue
            res = apply_message(detail, ts_kst=sent_kst or "", dry_run=dry_run,
                                account=_account_for_broker(broker))
            if res is None:
                continue
            results.append((broker, int(log_id), sent_kst or "", res))
            if dry_run:
                continue
            if res.applied:
                n_applied += 1
                log(f"[{broker}] 반영 logId={log_id} {res.action}: {res.summary}")
                if token and chat_id:
                    tg_send(token, chat_id, _confirm_text(broker, sent_kst or "", res))
            else:
                log(f"[{broker}] 보류 logId={log_id} {res.action}: {res.summary} | {res.warning}")
                if token and chat_id and res.action.startswith("skip"):
                    tg_send(token, chat_id, _alert_text(broker, sent_kst or "", res))
            time.sleep(0.3)

        if not dry_run:
            state[ks] = max_seen
        if n_applied:
            log(f"[{broker}] {n_applied}건 반영")

    if not dry_run:
        save_state(state)

    # 반영분이 있으면 즉시 대시보드 재발행해 잔고를 바로 갱신 (장중 15분 주기 대기 없이)
    n_total = sum(1 for *_, r in results if r.applied)
    if n_total and not dry_run:
        _republish_dashboard()
    return results


def _republish_dashboard() -> None:
    """적용 직후 Firebase Hosting 대시보드를 1회 재발행(현재가+신규 잔고 반영)."""
    try:
        from dashboard_refresh import refresh_once
        if refresh_once(force=True):
            log("대시보드 재발행 완료")
    except Exception as e:  # noqa: BLE001
        log(f"대시보드 재발행 실패(다음 주기 dash-refresh가 처리): {e}")


def _print_dry(results: list[tuple[str, int, str, ApplyResult]]) -> None:
    if not results:
        print("반영할 신규 체결 없음.")
        return
    print("=" * 64)
    print("  카톡 체결 → 잔고 반영 미리보기 (DRY-RUN, 쓰기 없음)")
    print("=" * 64)
    for broker, log_id, ts, res in results:
        flag = "반영" if res.applied else "보류"
        print(f"\n[{broker}] {ts}  ({flag}) {res.action}")
        print(f"  {res.summary}")
        if res.warning:
            print(f"  ⚠ {res.warning}")
        print(f"  (logId={log_id})")
    n_ok = sum(1 for *_, r in results if r.applied)
    print(f"\n{'-' * 64}\n반영 {n_ok}건 / 보류 {len(results) - n_ok}건")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="증권사 카톡 체결 알림 → invest_log 잔고 자동 반영")
    ap.add_argument("--init", action="store_true", help="baseline 만 설정(과거 미반영)")
    ap.add_argument("--from-today", action="store_true", help="오늘 0시(KST)부터 미반영분 반영")
    ap.add_argument("--dry-run", action="store_true", help="미리보기(데이터/텔레그램 쓰기 없음)")
    ap.add_argument("--loop", type=int, default=0, help="N초 간격 데몬(0=1회만)")
    args = ap.parse_args(argv)

    from_today_epoch = None
    if args.from_today:
        now = datetime.now(KST)
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        from_today_epoch = int(today0.timestamp())

    token = chat_id = None
    if not args.dry_run:
        from kakao_to_telegram import load_credentials
        try:
            token, chat_id = load_credentials()
        except SystemExit as e:
            log(f"자격증명 경고(텔레그램 확인 메시지 생략): {e}")

    if args.loop > 0:
        log(f"=== 카톡 자동반영 데몬 시작: {args.loop}초 간격 ===")
        while True:
            try:
                poll_once(token, chat_id, from_today_epoch=from_today_epoch)
            except SystemExit as e:
                log(f"치명적 오류(다음 주기 재시도): {e}")
            except Exception as e:  # noqa: BLE001
                log(f"폴링 예외: {e}")
            from_today_epoch = None  # 1회만 부트스트랩
            time.sleep(args.loop)

    results = poll_once(
        token, chat_id,
        dry_run=args.dry_run, from_today_epoch=from_today_epoch, init_only=args.init,
    )
    if args.dry_run:
        _print_dry(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
