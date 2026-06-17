#!/usr/bin/env python3
"""융자(신용) 잔액 정정 — KB증권 + 신한 융자금액 스크린샷(2026-06-17) 기준.

각 종목의 credit_loan(combined)과 by_account 항목의 credit/funding 을 증권사
표시 융자금액으로 맞춘다. credit_loan 은 NAV(− 신용)에 들어가므로 정확값 중요.
아이티센글로벌은 화면 잘림 — 손익분기(54,734)≈평단(54,700)으로 이자 거의 0 →
현금(융자 0)으로 판단(기존 2,418,373 은 stale 로 간주).

  python scripts/reconcile_credit_20260617.py            # dry-run
  python scripts/reconcile_credit_20260617.py --apply    # 저장
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.json_store import load_holdings, save_holdings

# 종목 → [(account, 융자금액, funding)]
CREDIT: dict[str, list[tuple]] = {
    "SK하이닉스": [("KB", 50_693_000, "현금+자기융자"), ("신한", 6_978_800, "자기융자")],
    "삼성전자": [("KB", 50_658_000, "현금+자기융자")],
    "삼성전기": [("KB", 5_190_000, "현금+자기융자"), ("신한", 13_647_300, "유통융자")],
    "TIGER삼성전자단일종목레버리지": [("KB", 0, "현금")],
    "TIGERSK하이닉스단일종목레버리지": [("KB", 0, "현금")],
    "아이티센글로벌": [("KB", 0, "현금")],
    "KODEX반도체레버리지": [("신한", 0, "현금")],
}


def main() -> int:
    apply = "--apply" in sys.argv
    holdings = load_holdings()
    by_name = {h.get("name", ""): h for h in holdings}

    print(f"{'종목':24s} {'기존융자':>14s} → {'새융자':>14s}   계좌별")
    print("-" * 84)
    old_tot = new_tot = 0
    for name, rows in CREDIT.items():
        h = by_name.get(name)
        if not h:
            print(f"⚠️ 시스템에 없음: {name}")
            continue
        old = float(h.get("credit_loan", 0) or 0)
        new = sum(amt for _, amt, _ in rows)
        old_tot += old
        new_tot += new
        # by_account 항목에 credit/funding 반영
        ba = {x.get("account"): x for x in (h.get("by_account") or [])}
        detail = []
        for acct, amt, funding in rows:
            ent = ba.get(acct)
            if ent is not None:
                ent["credit"] = amt
                ent["funding"] = funding
            detail.append(f"{acct}:{amt:,}")
        h["credit_loan"] = new
        h["by_account"] = list(ba.values()) if ba else h.get("by_account", [])
        mark = "" if abs(old - new) < 1 else "  *변경"
        print(f"{name:24s} {old:>14,.0f} → {new:>14,.0f}   {' + '.join(detail)}{mark}")

    print("-" * 84)
    print(f"{'합계':24s} {old_tot:>14,.0f} → {new_tot:>14,.0f}   (NAV 영향 = −신용 변화 {new_tot-old_tot:+,.0f})")

    if apply:
        save_holdings(holdings)
        print("\n✅ 저장 완료 (portfolio.json)")
    else:
        print("\n(dry-run — 저장 안 함. --apply 로 적용)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
