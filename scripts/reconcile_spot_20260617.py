#!/usr/bin/env python3
"""현물 보유 정정 — KB증권 + 신한 잔고 스크린샷(2026-06-17) 기준.

수량은 기존 시스템과 일치하므로, **평단/총매입금액을 증권사 표시값으로 갱신**하고
각 종목에 **by_account 분해(KB/신한별 수량·평단·자금구분)** 를 기록한다.
현재가는 네이버라 평단 변경은 표시 평단/수익률만 바꾸고 NAV·예수금·신용은 불변.
credit_loan(융자)은 이번에 손대지 않는다(`융자` 명령으로 별도 정합).

  python scripts/reconcile_spot_20260617.py            # dry-run(미저장)
  python scripts/reconcile_spot_20260617.py --apply    # 저장
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.json_store import load_holdings, save_holdings

# 시스템 종목명 → 계좌별 lot (account, quantity, avg_price, funding)
# (KB는 한 종목에 현금/신용 lot 2줄이 있어 합산, 신한은 구분=자금유형)
TARGET: dict[str, list[dict]] = {
    "SK하이닉스": [
        {"account": "KB", "quantity": 30, "total": 5 * 2_060_000 + 25 * 2_027_720, "funding": ""},
        {"account": "신한", "quantity": 5, "total": 5 * 1_801_000, "funding": "자기융자"},
    ],
    "삼성전자": [
        {"account": "KB", "quantity": 196, "total": 30 * 325_500 + 166 * 305_168, "funding": ""},
    ],
    "삼성전기": [
        {"account": "KB", "quantity": 11, "total": 6 * 1_809_000 + 5 * 1_038_000, "funding": ""},
        {"account": "신한", "quantity": 10, "total": 10 * 1_761_000, "funding": "유통융자"},
    ],
    "TIGER삼성전자단일종목레버리지": [
        {"account": "KB", "quantity": 565, "total": 565 * 17_705, "funding": ""},
    ],
    "TIGERSK하이닉스단일종목레버리지": [
        {"account": "KB", "quantity": 1770, "total": 1770 * 18_020, "funding": ""},
    ],
    "아이티센글로벌": [
        {"account": "KB", "quantity": 90, "total": 90 * 54_700, "funding": ""},
    ],
    "KODEX반도체레버리지": [
        {"account": "신한", "quantity": 1, "total": 1 * 65_398, "funding": "현금"},
    ],
}


def build_by_account(lots: list[dict]) -> list[dict]:
    out = []
    for lot in lots:
        q, tot = lot["quantity"], lot["total"]
        out.append({
            "account": lot["account"],
            "quantity": q,
            "avg_price": round(tot / q) if q else 0,
            "total_invested": tot,
            "funding": lot["funding"],
        })
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    holdings = load_holdings()
    by_name = {h.get("name", ""): h for h in holdings}

    print(f"{'종목':24s} {'수량':>6s} {'기존평단':>12s} → {'새평단':>12s}  {'계좌분해'}")
    print("-" * 92)
    missing = [n for n in TARGET if n not in by_name]
    if missing:
        print("⚠️ 시스템에 없는 종목:", missing)

    for name, lots in TARGET.items():
        h = by_name.get(name)
        if not h:
            continue
        ba = build_by_account(lots)
        new_qty = sum(x["quantity"] for x in ba)
        new_total = sum(x["total_invested"] for x in ba)
        new_avg = round(new_total / new_qty) if new_qty else 0
        old_avg = h.get("avg_price", 0)
        old_qty = h.get("quantity", 0)
        flag = "" if old_qty == new_qty else f"  ⚠️수량 {old_qty}→{new_qty}"
        acct = " + ".join(f"{x['account']}:{x['quantity']}@{x['avg_price']:,}" for x in ba)
        print(f"{name:24s} {new_qty:>6,} {old_avg:>12,.0f} → {new_avg:>12,.0f}  {acct}{flag}")
        h["quantity"] = new_qty
        h["total_invested"] = new_total
        h["avg_price"] = new_avg
        h["by_account"] = ba

    # 시스템엔 있는데 스샷에 없는 종목(매도 완료 후보) 경고
    extra = [n for n in by_name if n not in TARGET and by_name[n].get("quantity", 0) > 0]
    if extra:
        print("\n⚠️ 스샷에 없는 보유종목(확인 필요):", extra)

    if apply:
        save_holdings(holdings)
        print("\n✅ 저장 완료 (portfolio.json)")
    else:
        print("\n(dry-run — 저장 안 함. 적용하려면 --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
