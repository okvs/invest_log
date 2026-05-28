#!/usr/bin/env python3
"""각 과거일자의 포트폴리오를 동결했을 때 오늘 평가금 백테스트.

질문: "그때 포트폴리오를 지금까지 유지했으면 평가자산이 현재보다 높은 날이 있어?"

가정:
- 매 과거일자 D 의 *현물 보유량*을 그날 장 마감 시점으로 동결 (D 이후 매매 없음).
- 입출금(deposit/withdraw)은 실제와 동일하게 발생 — 매매 결정만 동결.
- 오늘 평가금 = sum(qty_D[t] × price_today[t]) + cash_D + Δdeposits(D→today)
  · cash_D = initial + cum_dep(≤D) − cum_withd(≤D) + cum_sells(≤D) − cum_buys(≤D)
- 선물은 제외 (만기/롤오버로 동결 가정과 안 맞음).

산출:
- 콘솔: 동결-오늘 NAV 가 현재 실측 NAV 보다 높은 모든 일자 표.
- PNG: 동결-오늘 NAV 시계열 + 현재 실측 NAV 수평선, 초과 일자 강조.
"""
from __future__ import annotations

import io
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import FuncFormatter

from storage.json_store import (
    load_account,
    load_cash_events,
    load_holdings,
    load_ticker_map,
    load_transactions,
)


_KO_FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/AppleGothic.ttf",
]


def _setup_korean_font() -> None:
    for path in _KO_FONT_CANDIDATES:
        if Path(path).exists():
            font_manager.fontManager.addfont(path)
            prop = font_manager.FontProperties(fname=path)
            plt.rcParams["font.family"] = prop.get_name()
            plt.rcParams["axes.unicode_minus"] = False
            return


def _date_only(s: str) -> str:
    return (s or "")[:10]


# 같은 종목이 잘린/다른 이름으로 기록된 케이스 → 정식 이름으로 통일.
# transactions.json 데이터 클린업 전까지 백테스트용 동치(alias) 처리.
_NAME_ALIAS = {
    "G넥스원": "LIG넥스원",                          # 파서 truncation
    "반도체레버리지": "KODEX반도체레버리지",            # 파서 truncation
    "LIG디펜스앤에어로스페이스": "LIG넥스원",           # 사명변경 (같은 종목)
}

# 거래내역에 ticker_map 누락된 종목 수동 보강.
_MANUAL_NAME_CODE = {
    "메리츠금융지주": "138040",
    "파두": "440110",
    "티엘비": "356860",
    "에스지헬스케어": "405100",   # 추정
    # "그래피", "디바이스", "엔비알모션" — 매핑 불확실해 last-known price 폴백
}


def _canon(name: str) -> str:
    """별칭 정규화."""
    return _NAME_ALIAS.get(name, name)


def _name_to_code() -> dict[str, str]:
    """종목명 → 6자리 코드. portfolio.holdings 와 ticker_map 모두 사용."""
    out: dict[str, str] = {}
    for h in load_holdings():
        n, t = h.get("name", ""), h.get("ticker", "")
        if n and t:
            out[n] = t.split(".")[0]
    for n, t in load_ticker_map().items():
        if t and n not in out:
            out[n] = t.split(".")[0]
    for n, c in _MANUAL_NAME_CODE.items():
        out.setdefault(n, c)
    return out


def _last_known_price(transactions: list[dict]) -> dict[str, float]:
    """종목명 → 마지막 거래가격. 시세 못 구한 종목의 today price 폴백."""
    out: dict[str, float] = {}
    for t in sorted(transactions, key=lambda x: x.get("date", "")):
        n = _canon(t.get("name", ""))
        p = float(t.get("price", 0) or 0)
        if n and p > 0:
            out[n] = p
    return out


def _reconstruct_daily_holdings(
    transactions: list[dict], dates: list[date]
) -> dict[date, dict[str, int]]:
    """각 date 시점의 보유 {종목: qty}."""
    ev: dict[str, list[dict]] = defaultdict(list)
    for t in sorted(transactions, key=lambda x: x.get("date", "")):
        if t.get("date"):
            ev[_canon(t.get("name", ""))].append(t)
    out: dict[date, dict[str, int]] = {d: {} for d in dates}
    for name, evs in ev.items():
        qty = 0
        idx = 0
        for d in dates:
            while idx < len(evs) and date.fromisoformat(_date_only(evs[idx]["date"])) <= d:
                e = evs[idx]
                q = int(e.get("quantity", 0))
                if e.get("type") == "buy":
                    qty += q
                elif e.get("type") == "sell":
                    qty = max(0, qty - q)
                idx += 1
            if qty > 0:
                out[d][name] = qty
    return out


def _daily_cash_balance(
    transactions: list[dict],
    cash_events: list[dict],
    today_cash: float,
    dates: list[date],
) -> dict[date, float]:
    """장 마감 시점 spot cash 잔고. 오늘 실측 cash 를 anchor 로 역산.

    cash_D = today_cash - (sells_after_D - buys_after_D)
                        - (dep_after_D - withd_after_D)

    초기자본 + 누적 거래로 forward 계산하면 spot↔futures 서브계좌 이체 등
    cash_events 에 안 잡힌 흐름 때문에 reality 와 어긋남 — 역산이 더 안전.
    """
    # 일별 net cash flow (>0 = cash 증가)
    flow: dict[date, float] = defaultdict(float)
    for e in cash_events:
        if e.get("type") == "seed":
            continue
        try:
            d = date.fromisoformat(e.get("date", ""))
        except (ValueError, TypeError):
            continue
        a = float(e.get("amount", 0))
        flow[d] += (-a if e.get("type") == "withdraw" else a)
    for t in transactions:
        if not t.get("date"):
            continue
        d = date.fromisoformat(_date_only(t["date"]))
        qty = float(t.get("quantity", 0))
        pr = float(t.get("price", 0))
        if t.get("type") == "buy":
            flow[d] -= qty * pr
        elif t.get("type") == "sell":
            flow[d] += qty * pr
    # 역산: today_cash 를 시작점으로 미래→과거로 차감
    out: dict[date, float] = {}
    cur = today_cash
    for d in sorted(dates, reverse=True):
        out[d] = cur
        cur -= flow.get(d, 0.0)  # 그날 flow 제거 → 전날 cash
    return out


def _net_deposits_after(cash_events: list[dict], after: date) -> float:
    """date>after 인 deposit−withdraw 합."""
    net = 0.0
    for e in cash_events:
        if e.get("type") == "seed":
            continue
        try:
            d = date.fromisoformat(e.get("date", ""))
        except (ValueError, TypeError):
            continue
        if d <= after:
            continue
        a = float(e.get("amount", 0))
        net += (-a if e.get("type") == "withdraw" else a)
    return net


def _fetch_pykrx_closes(
    codes, start: date, end: date
) -> dict[tuple[str, date], float]:
    """6자리 코드별 일별 종가 dict."""
    out: dict[tuple[str, date], float] = {}
    try:
        from pykrx import stock
    except ImportError:
        print("pykrx 미설치", file=sys.stderr)
        return out
    s8 = start.strftime("%Y%m%d")
    e8 = end.strftime("%Y%m%d")
    for code in {c for c in codes if c}:
        try:
            df = stock.get_market_ohlcv_by_date(s8, e8, code)
            for dt, row in df.iterrows():
                out[(code, dt.date())] = float(row["종가"])
        except Exception as exc:
            print(f"[warn] pykrx {code} 실패: {exc}", file=sys.stderr)
    return out


def _close_ffill(
    price: dict[tuple[str, date], float], code: str, target: date, lookback: int = 14
) -> float | None:
    cur = target
    for _ in range(lookback):
        if (code, cur) in price:
            return price[(code, cur)]
        cur -= timedelta(days=1)
    return None


def _fmt_krw(x: float) -> str:
    if abs(x) >= 1e8:
        return f"{x/1e8:.2f}억"
    if abs(x) >= 1e7:
        return f"{x/1e7:.1f}천만"
    if abs(x) >= 1e4:
        return f"{x/1e4:.0f}만"
    return f"{x:,.0f}원"


def _fmt_krw_axis(x: float, _pos) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1e8:
        eok = x / 1e8
        return f"{round(eok):.0f}억" if abs(eok - round(eok)) < 0.05 else f"{eok:.1f}억"
    return f"{x/1e7:.0f}천만"


def main() -> int:
    transactions = load_transactions()
    if not transactions:
        print("거래 내역 없음.")
        return 1
    account = load_account()
    initial = float(account.get("initial_capital") or 0)
    cash_events = load_cash_events()
    code_by_name = _name_to_code()
    last_known = _last_known_price(transactions)

    # 거래일(매수/매도가 있던 날)만 D 후보로. 그 외 날은 동결 의미가 없음.
    trade_dates_set = {
        date.fromisoformat(_date_only(t["date"]))
        for t in transactions
        if t.get("date") and t.get("type") in {"buy", "sell"}
    }
    start = min(trade_dates_set)
    today = date.today()
    # holdings/cash 재구성은 전체 캘린더 일자로(다만 평가는 거래일만)
    dates: list[date] = []
    d = start
    while d <= today:
        dates.append(d)
        d += timedelta(days=1)
    trade_dates = sorted(trade_dates_set)

    hold = _reconstruct_daily_holdings(transactions, dates)
    today_cash = float(account.get("cash") or 0)
    cash = _daily_cash_balance(transactions, cash_events, today_cash, dates)

    # 보정: cash_events 에 안 잡힌 spot→futures 서브계좌 이체.
    # 첫 선물 진입일을 경계로, 그 이전 cash 는 (forward 식으로) 초기자본 기반이 맞고
    # 그 이후 cash 는 (reverse 식으로) today_cash 기반이 맞다 — 두 추정의 차이가
    # 서브계좌로 빠진 금액. 그 차이를 first_fut_d 시점에 일시 출금으로 모델링.
    forward_cash_today = (
        initial
        + sum(
            (-float(e["amount"]) if e.get("type") == "withdraw" else float(e["amount"]))
            for e in cash_events
            if e.get("type") != "seed" and e.get("date", "")
        )
        + sum(
            float(t.get("quantity", 0)) * float(t.get("price", 0))
            * (1 if t.get("type") == "sell" else -1)
            for t in transactions
            if t.get("type") in {"buy", "sell"}
        )
    )
    sub_xfer = forward_cash_today - today_cash  # >0 = spot 에서 빠짐, <0 = 들어옴
    first_fut_d: date | None = None
    try:
        from storage.json_store import load
        ft = load("futures_transactions.json").get("transactions", [])
        if ft:
            first_fut_d = date.fromisoformat(
                min(t["date"] for t in ft if t.get("date"))[:10]
            )
    except Exception:
        pass
    if abs(sub_xfer) > 1e6 and first_fut_d:
        dir_label = "spot→sub" if sub_xfer > 0 else "sub→spot"
        print(f"[info] {dir_label} 추정 이체 {sub_xfer:+,.0f}원 → {first_fut_d} 시점 모델링. "
              f"(cash_events 에 미기록된 흐름)",
              file=sys.stderr)
        # first_fut_d 시점에 sub_xfer 만큼의 cash 변동이 있었다고 가정.
        # reverse 는 그 변동을 안 본 채 today_cash 에서 walk back 했으므로,
        # 그 이전 날들은 (sub_xfer) 만큼 보정 필요.
        # sub_xfer<0 (외부→spot 인플로우): 인플로우 이전엔 cash 가 더 적었어야 → 빼줌
        # sub_xfer>0 (spot→외부 아웃플로우): 아웃플로우 이전엔 cash 가 더 많았어야 → 더해줌
        for d_, v in list(cash.items()):
            if d_ < first_fut_d:
                cash[d_] = v + sub_xfer

    # 시세
    codes_needed = {code_by_name.get(n) for d_ in dates for n in hold[d_].keys()}
    codes_needed.discard(None)
    # 오늘 종가는 today, 과거 NAV 표시도 위해 시작일까지 전체 다운로드
    price = _fetch_pykrx_closes(codes_needed, start, today)

    # 오늘 가격 (오늘 거래 안 됐으면 가장 최근 영업일 종가)
    price_today: dict[str, float] = {}
    for code in codes_needed:
        p = _close_ffill(price, code, today, lookback=14)
        if p is not None:
            price_today[code] = p

    # 시세 누락 종목 → last-known-price 폴백 (0% 수익 가정)
    fallback: set[str] = set()
    for d_ in dates:
        for n in hold[d_].keys():
            code = code_by_name.get(n)
            if (not code or code not in price_today) and n in last_known:
                fallback.add(n)
    if fallback:
        print(f"[info] 오늘 시세 누락 → 마지막 거래가로 폴백: {sorted(fallback)}",
              file=sys.stderr)
    # 완전 누락(폴백조차 없는) 종목
    unmapped: set[str] = set()
    for d_ in dates:
        for n in hold[d_].keys():
            code = code_by_name.get(n)
            has_today = code and code in price_today
            has_fallback = n in last_known
            if not has_today and not has_fallback:
                unmapped.add(n)
    if unmapped:
        print(f"[warn] 매핑+폴백 모두 실패 (계산 제외): {sorted(unmapped)}", file=sys.stderr)

    # 현재 실측 NAV (현물만): 오늘 보유×오늘가 + 현재 cash
    current_holdings = {h.get("name", ""): int(h.get("quantity", 0)) for h in load_holdings()}
    cur_holdings_value = 0.0
    for n, q in current_holdings.items():
        code = code_by_name.get(n)
        p = price_today.get(code) if code else None
        if p is not None:
            cur_holdings_value += q * p
    cur_cash = float(account.get("cash") or 0)
    nav_actual_today = cur_holdings_value + cur_cash

    # 각 D(거래일만) 에 대해 동결-오늘 NAV
    rows: list[dict] = []
    for d_ in trade_dates:
        held = hold[d_]
        # 보유 0 종목인 날(완전 현금)도 의미 있으니 포함
        frozen_val = 0.0
        valued_any = False
        for n, q in held.items():
            code = code_by_name.get(n)
            p = price_today.get(code) if code else None
            if p is None:
                p = last_known.get(n)  # 0% 수익 가정 폴백
            if p is None:
                continue
            frozen_val += q * p
            valued_any = True
        # 동결-오늘 cash = D 시점 cash + D 이후 net deposits
        cash_d = cash.get(d_, initial)
        net_dep_after = _net_deposits_after(cash_events, d_)
        nav_frozen_today = frozen_val + cash_d + net_dep_after

        # 비교용: 그날 실제 NAV (그날 보유 × 그날 종가 + 그날 cash)
        nav_actual_d = cash_d
        for n, q in held.items():
            code = code_by_name.get(n)
            p = _close_ffill(price, code, d_, lookback=10) if code else None
            if p is None:
                p = last_known.get(n)
            if p is not None:
                nav_actual_d += q * p

        rows.append({
            "date": d_,
            "nav_frozen_today": nav_frozen_today,
            "nav_actual_d": nav_actual_d,
            "cash_d": cash_d,
            "net_dep_after": net_dep_after,
            "frozen_holdings_value_today": frozen_val,
            "valued_any": valued_any,
        })

    # 텍스트 표 — 모든 거래일을 날짜순으로
    print()
    print(f"=== 백테스트: 현물 동결 시 오늘 NAV ===")
    print(f"기간: {rows[0]['date']} ~ {rows[-1]['date']}  (거래일 {len(rows)}일)")
    print(f"현재 실측 NAV (현물): {_fmt_krw(nav_actual_today)}  "
          f"(보유 {_fmt_krw(cur_holdings_value)} + 현금 {_fmt_krw(cur_cash)})")
    print(f"초기자본: {_fmt_krw(initial)}")
    higher = [r for r in rows if r["nav_frozen_today"] > nav_actual_today]
    print(f"→ 현재 NAV 초과 거래일: {len(higher)}/{len(rows)}건")
    print()
    print(f"  {'날짜':<12} {'동결-오늘 NAV':>16} {'vs 현재':>14} {'그날 실제 NAV':>16}  {'표시'}")
    for r in rows:
        diff = r["nav_frozen_today"] - nav_actual_today
        sign = "+" if diff > 0 else ""
        marker = " ★ 초과" if diff > 0 else ""
        print(f"  {r['date'].isoformat():<12} "
              f"{_fmt_krw(r['nav_frozen_today']):>16} "
              f"{sign + _fmt_krw(diff):>14} "
              f"{_fmt_krw(r['nav_actual_d']):>16}"
              f"{marker}")

    # 그래프
    _setup_korean_font()
    xs = [r["date"] for r in rows]
    ys_frozen = [r["nav_frozen_today"] for r in rows]
    ys_actual_d = [r["nav_actual_d"] for r in rows]

    fig, ax = plt.subplots(figsize=(11, 5.8), dpi=120)
    ax.plot(xs, ys_actual_d, color="#9ca3af", linewidth=1.4, linestyle=":",
            label="실제 그날 NAV (참고)", zorder=3)
    ax.plot(xs, ys_frozen, color="#3b82f6", linewidth=2.4,
            label="그날 동결 → 오늘 NAV", zorder=5)
    ax.axhline(nav_actual_today, color="#f59e0b", linewidth=1.6, linestyle="--",
               label=f"현재 실측 NAV {_fmt_krw(nav_actual_today)}", zorder=4)
    ax.axhline(initial, color="#22c55e", linewidth=0.7, linestyle=":", alpha=0.5)
    ax.annotate(f"초기자본 {_fmt_krw(initial)}", xy=(xs[0], initial),
                xytext=(0, 6), textcoords="offset points",
                color="#22c55e", fontsize=8, va="bottom", ha="left")

    # 현재 초과 일자 강조
    over_xs = [r["date"] for r in higher]
    over_ys = [r["nav_frozen_today"] for r in higher]
    if over_xs:
        ax.scatter(over_xs, over_ys, color="#ef4444", s=36, zorder=6,
                   label=f"현재 초과 ({len(higher)}건)")

    ax.yaxis.set_major_formatter(FuncFormatter(_fmt_krw_axis))
    ax.set_facecolor("#0f0f14")
    fig.patch.set_facecolor("#0f0f14")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color("#444")
    ax.tick_params(colors="#9ca3af", labelsize=10)
    ax.set_ylabel("NAV (KRW)", color="#9ca3af", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=12))
    ax.legend(loc="lower right", facecolor="#16161e", edgecolor="#333",
              labelcolor="#ddd", fontsize=9)
    ax.set_title(
        f"포트폴리오 동결 백테스트 — 현물 only — {start:%Y-%m-%d} ~ {today:%Y-%m-%d}",
        color="#fff", fontsize=12, loc="left", pad=12,
    )
    fig.tight_layout()

    out = ROOT / "reports" / f"backtest_frozen_{today:%Y%m%d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    print()
    print(f"PNG 저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
