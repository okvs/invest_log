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
    load,
    load_account,
    load_cash_events,
    load_holdings,
    load_ticker_map,
    load_transactions,
)

# 자산그래프와 동일한 식의 일별 NAV 시계열을 흰점선에 쓰기 위해
# (initial + 누적 입출금 + 누적 실현 + 그날 미실현)
from bot.asset_history import compute_profit_trend


def _load_futures_transactions() -> list[dict]:
    return load("futures_transactions.json").get("transactions", [])


def _load_futures_positions() -> list[dict]:
    return load("futures_positions.json").get("positions", [])


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


def _reconstruct_daily_holdings_with_avg(
    transactions: list[dict], dates: list[date]
) -> dict[date, dict[str, tuple[int, float]]]:
    """각 date 시점 {종목: (qty, avg_price)}.

    매수 시 가중평균으로 평단 갱신, 매도 시 수량만 차감(평단 유지), 0주면 리셋.
    """
    ev: dict[str, list[dict]] = defaultdict(list)
    for t in sorted(transactions, key=lambda x: x.get("date", "")):
        if t.get("date"):
            ev[_canon(t.get("name", ""))].append(t)
    out: dict[date, dict[str, tuple[int, float]]] = {d: {} for d in dates}
    for name, evs in ev.items():
        qty = 0
        avg = 0.0
        idx = 0
        for d in dates:
            while idx < len(evs) and date.fromisoformat(_date_only(evs[idx]["date"])) <= d:
                e = evs[idx]
                q = int(e.get("quantity", 0))
                pr = float(e.get("price", 0))
                if e.get("type") == "buy":
                    newq = qty + q
                    avg = (avg * qty + pr * q) / newq if newq > 0 else 0.0
                    qty = newq
                elif e.get("type") == "sell":
                    qty = max(0, qty - q)
                    if qty == 0:
                        avg = 0.0
                idx += 1
            if qty > 0:
                out[d][name] = (qty, avg)
    return out


def _reconstruct_daily_credit(
    transactions: list[dict], dates: list[date]
) -> tuple[dict[date, float], dict[date, dict[str, float]]]:
    """각 date 시점의 신용대출 (총잔액, 종목별 dict).

    매수 시 qty × price × (100 − margin_ratio)/100 만큼 종목 credit 누적.
    margin_ratio 가 None 또는 ≥100 이면 자기자본 100%로 보고 신용 0.
    매도 시 (credit / qty) 비례 차감.
    """
    ev: dict[str, list[dict]] = defaultdict(list)
    for t in sorted(transactions, key=lambda x: x.get("date", "")):
        if t.get("date"):
            ev[_canon(t.get("name", ""))].append(t)
    per_name: dict[date, dict[str, float]] = {d: {} for d in dates}
    for name, evs in ev.items():
        qty = 0
        credit = 0.0
        idx = 0
        for d in dates:
            while idx < len(evs) and date.fromisoformat(_date_only(evs[idx]["date"])) <= d:
                e = evs[idx]
                q = int(e.get("quantity", 0))
                pr = float(e.get("price", 0))
                mr = e.get("margin_ratio")
                if e.get("type") == "buy":
                    if mr is None or float(mr) >= 100:
                        buy_credit = 0.0
                    else:
                        buy_credit = q * pr * (100.0 - float(mr)) / 100.0
                    credit += buy_credit
                    qty += q
                elif e.get("type") == "sell":
                    if qty > 0:
                        cps = credit / qty
                        credit -= q * cps
                        qty -= q
                        if qty <= 0:
                            qty = 0
                            credit = 0.0
                idx += 1
            if credit > 0:
                per_name[d][name] = credit
    totals = {d: sum(v.values()) for d, v in per_name.items()}
    return totals, per_name


def _seed_futures_synthetic_opens(
    positions: list[dict], transactions: list[dict]
) -> list[dict]:
    """transactions 에 없는 백로딩된 시드 포지션을 synthetic open tx 로 변환.

    entry_date 시점에 (전량) open 된 것처럼 처리. position_id 기준으로 transactions
    에 한 건이라도 매칭되는 게 있으면 시드 아님(스킵).
    """
    tx_pids = {t.get("position_id") for t in transactions if t.get("position_id")}
    out: list[dict] = []
    for p in positions:
        pid = p.get("id")
        if pid in tx_pids:
            continue
        out.append({
            "id": f"seed-{pid}",
            "type": "open",
            "name": p.get("name", ""),
            "symbol": p.get("symbol", ""),
            "contract_month": p.get("contract_month", ""),
            "expiry_date": p.get("expiry_date", ""),
            "direction": p.get("direction", "long"),
            "contracts": int(p.get("contracts", 0)),
            "price": float(p.get("avg_entry_price", 0)),
            "multiplier": int(p.get("multiplier", 10)),
            "margin": float(p.get("initial_margin", 0)),
            "sector": p.get("sector", ""),
            "date": (p.get("entry_date", "") or "") + "T00:00:00",
            "position_id": pid,
            "thesis": p.get("thesis", ""),
            "_seed": True,
        })
    return out


def _reconstruct_daily_futures_positions(
    futures_tx: list[dict], dates: list[date]
) -> dict[date, dict[str, dict]]:
    """각 date 시점의 선물 포지션 {position_id: {name, dir, contracts, avg, mult, margin}}.

    open/roll_open → 가중평균 진입가 갱신 + 계약수 + margin 증가.
    close/roll_close → 계약수 감소(0 되면 제거). margin 비례 감소.
    """
    txs = sorted(futures_tx, key=lambda x: x.get("date", ""))
    out: dict[date, dict[str, dict]] = {d: {} for d in dates}
    state: dict[str, dict] = {}
    idx = 0
    for d in dates:
        while idx < len(txs) and date.fromisoformat(_date_only(txs[idx]["date"])) <= d:
            t = txs[idx]
            pid = t.get("position_id") or t.get("id")
            ctr = int(t.get("contracts", 0))
            pr = float(t.get("price", 0))
            mult = int(t.get("multiplier", 10))
            mgn = float(t.get("margin", 0))
            ttype = t.get("type", "")
            if ttype in {"open", "roll_open"}:
                cur = state.get(pid)
                if cur is None:
                    state[pid] = {
                        "name": t.get("name", ""),
                        "symbol": t.get("symbol", ""),
                        "direction": t.get("direction", "long"),
                        "contracts": ctr,
                        "avg_entry": pr,
                        "multiplier": mult,
                        "margin": mgn,
                    }
                else:
                    nc = cur["contracts"] + ctr
                    if nc > 0:
                        cur["avg_entry"] = (
                            cur["avg_entry"] * cur["contracts"] + pr * ctr
                        ) / nc
                    cur["contracts"] = nc
                    cur["margin"] += mgn
            elif ttype in {"close", "roll_close"}:
                cur = state.get(pid)
                if cur:
                    # margin 은 계약수 비례로 환원
                    if cur["contracts"] > 0:
                        mgn_per = cur["margin"] / cur["contracts"]
                    else:
                        mgn_per = 0
                    cur["contracts"] -= ctr
                    cur["margin"] -= mgn_per * ctr
                    if cur["contracts"] <= 0:
                        state.pop(pid, None)
            idx += 1
        # snapshot
        out[d] = {pid: dict(v) for pid, v in state.items()}
    return out


def _daily_cash_balance(
    transactions: list[dict],
    cash_events: list[dict],
    futures_tx: list[dict],
    today_cash: float,
    dates: list[date],
) -> dict[date, float]:
    """장 마감 시점 통합 cash 잔고 (spot + futures sub-account).

    today_cash 를 anchor 로 역산. 거래 후 누적 cash 변동을 일별로 빼나가
    이전 일자 cash 를 복원.

    Cash flow events:
      - spot buy/sell: ±qty×price
      - futures open/roll_open: -margin (마진 묶임)
      - futures close/roll_close: +margin + pnl (마진 회수 + 실현손익)
      - deposit/withdraw: ±amount
    """
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
    for t in futures_tx:
        if not t.get("date"):
            continue
        d = date.fromisoformat(_date_only(t["date"]))
        mgn = float(t.get("margin", 0))
        pnl = float(t.get("pnl", 0) or 0)
        ttype = t.get("type", "")
        if ttype in {"open", "roll_open"}:
            flow[d] -= mgn
        elif ttype in {"close", "roll_close"}:
            flow[d] += mgn + pnl
    out: dict[date, float] = {}
    cur = today_cash
    for d in sorted(dates, reverse=True):
        out[d] = cur
        cur -= flow.get(d, 0.0)
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


def run_backtest() -> dict | None:
    """백테스트 실행. 결과 dict 또는 None 반환.

    dict: {rows, nav_actual_today, cur_holdings_value, cur_futures_value,
           today_total_cash, initial, png_buf, summary_text, table_text}
    """
    import io as _io

    transactions = load_transactions()
    if not transactions:
        return None
    account = load_account()
    initial = float(account.get("initial_capital") or 0)
    cash_events = load_cash_events()
    code_by_name = _name_to_code()
    last_known = _last_known_price(transactions)

    # 선물: 백로딩된 시드 포지션 → synthetic open 으로 변환해 합침
    raw_futures_tx = _load_futures_transactions()
    futures_seeded = _seed_futures_synthetic_opens(
        _load_futures_positions(), raw_futures_tx
    )
    futures_tx = list(raw_futures_tx) + futures_seeded

    # 거래일 D 후보 = 현물 buy/sell + 선물 open/close/roll
    trade_dates_set = {
        date.fromisoformat(_date_only(t["date"]))
        for t in transactions
        if t.get("date") and t.get("type") in {"buy", "sell"}
    }
    trade_dates_set |= {
        date.fromisoformat(_date_only(t["date"]))
        for t in futures_tx
        if t.get("date")
        and t.get("type") in {"open", "close", "roll_open", "roll_close"}
    }
    start = min(trade_dates_set)
    today = date.today()
    dates: list[date] = []
    d = start
    while d <= today:
        dates.append(d)
        d += timedelta(days=1)
    trade_dates = sorted(trade_dates_set)

    hold = _reconstruct_daily_holdings(transactions, dates)
    hold_avg = _reconstruct_daily_holdings_with_avg(transactions, dates)
    credit_by_d, credit_per_name_by_d = _reconstruct_daily_credit(transactions, dates)
    fut_hold = _reconstruct_daily_futures_positions(futures_tx, dates)
    today_total_cash = (
        float(account.get("cash") or 0) + float(account.get("futures_cash") or 0)
    )
    cash = _daily_cash_balance(
        transactions, cash_events, futures_tx, today_total_cash, dates
    )

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

    def _futures_value(positions: dict[str, dict], price_lookup) -> tuple[float, bool]:
        """positions(pid→dict) 를 오늘 시점으로 평가.

        포지션 가치 = margin + (today_underlying − avg_entry) × contracts × mult × dir
        (롤오버 가정: 만기 무관하게 기초자산 가격을 그대로 사용)
        price_lookup(name) → float | None
        """
        tot = 0.0
        any_valued = False
        for pid, p in positions.items():
            ctr = int(p.get("contracts", 0))
            if ctr <= 0:
                continue
            tp = price_lookup(p.get("name", ""))
            if tp is None:
                continue
            avg = float(p.get("avg_entry", 0))
            mult = int(p.get("multiplier", 10))
            dir_sign = 1 if p.get("direction") == "long" else -1
            mgn = float(p.get("margin", 0))
            tot += mgn + (tp - avg) * ctr * mult * dir_sign
            any_valued = True
        return tot, any_valued

    def _today_price_for(name: str) -> float | None:
        code = code_by_name.get(name)
        if code and code in price_today:
            return price_today[code]
        return last_known.get(name)

    def _close_for(name: str, dd: date) -> float | None:
        code = code_by_name.get(name)
        p = _close_ffill(price, code, dd, lookback=10) if code else None
        if p is None:
            p = last_known.get(name)
        return p

    # 자산그래프와 동일한 회계 식을 백테스트 전체에 적용:
    #   asset = initial + 누적 입출금 + 누적 실현 + 미실현
    # 동결-오늘(D) 라인 식:
    #   = initial + 총 입출금 (동결이라도 입출금은 계속)
    #     + cum_realized_thru_D (D 까지의 실현)
    #     + unrealized_today_with_D_qty (D 시점 보유를 오늘 가격으로 평가)

    # 1) 총 입출금
    total_dep = sum(
        (-float(e["amount"]) if e.get("type") == "withdraw" else float(e["amount"]))
        for e in cash_events
        if e.get("type") != "seed" and e.get("date")
    )

    # 2) 누적 실현 by date (현물 매도 + 선물 청산)
    realized_events: list[tuple[date, float]] = []
    for t in transactions:
        if t.get("type") == "sell" and t.get("date"):
            realized_events.append(
                (date.fromisoformat(_date_only(t["date"])),
                 float(t.get("profit_loss", 0) or 0))
            )
    for t in raw_futures_tx:
        if t.get("type") in {"close", "roll_close"} and t.get("date"):
            realized_events.append(
                (date.fromisoformat(_date_only(t["date"])),
                 float(t.get("pnl", 0) or 0))
            )
    realized_events.sort()
    cum_realized_by_d: dict[date, float] = {}
    running = 0.0
    idx = 0
    for d_ in dates:
        while idx < len(realized_events) and realized_events[idx][0] <= d_:
            running += realized_events[idx][1]
            idx += 1
        cum_realized_by_d[d_] = running
    total_realized = running  # 전체 누적 실현

    # 3) D 시점 보유 → 오늘 가격으로 미실현 평가
    def _unrealized_frozen(d_: date) -> float:
        val = 0.0
        # 현물
        for n, (q, avg) in hold_avg.get(d_, {}).items():
            p_now = _today_price_for(n)
            if p_now is None:
                continue
            val += (p_now - avg) * q
        # 선물 (mark-to-market, margin 은 cash 의 일부라 미실현에 안 들어감)
        for pid, fp in fut_hold.get(d_, {}).items():
            ctr = int(fp.get("contracts", 0))
            if ctr <= 0:
                continue
            p_now = _today_price_for(fp.get("name", ""))
            if p_now is None:
                continue
            avg = float(fp.get("avg_entry", 0))
            mult = int(fp.get("multiplier", 10))
            dir_sign = 1 if fp.get("direction") == "long" else -1
            val += (p_now - avg) * ctr * mult * dir_sign
        return val

    # 4) 오늘 시점 미실현 (실측) — 현재 보유·포지션을 오늘 가격으로
    current_holdings = {
        h.get("name", ""): {
            "qty": int(h.get("quantity", 0)),
            "avg": float(h.get("avg_price", 0)),
        } for h in load_holdings()
    }
    cur_holdings_value = 0.0
    cur_unrealized_spot = 0.0
    for n, info in current_holdings.items():
        p_now = _today_price_for(n)
        if p_now is None:
            continue
        cur_holdings_value += info["qty"] * p_now
        cur_unrealized_spot += (p_now - info["avg"]) * info["qty"]
    cur_fut_positions: dict[str, dict] = {}
    for p in _load_futures_positions():
        cur_fut_positions[p.get("id")] = {
            "name": p.get("name", ""),
            "direction": p.get("direction", "long"),
            "contracts": int(p.get("contracts", 0)),
            "avg_entry": float(p.get("avg_entry_price", 0)),
            "multiplier": int(p.get("multiplier", 10)),
            "margin": float(p.get("initial_margin", 0)),
        }
    cur_futures_value, _ = _futures_value(cur_fut_positions, _today_price_for)
    cur_unrealized_futures = 0.0
    for fp in cur_fut_positions.values():
        ctr = int(fp.get("contracts", 0))
        if ctr <= 0:
            continue
        p_now = _today_price_for(fp.get("name", ""))
        if p_now is None:
            continue
        avg = float(fp.get("avg_entry", 0))
        mult = int(fp.get("multiplier", 10))
        dir_sign = 1 if fp.get("direction") == "long" else -1
        cur_unrealized_futures += (p_now - avg) * ctr * mult * dir_sign
    cur_credit = sum(
        float(h.get("credit_loan", 0) or 0) for h in load_holdings()
    )

    # 5) 자산그래프와 동일한 식: NAV = initial + 누적 실현(현물+선물) + 미실현
    # 입출금·신용 차감 무시 (사용자 요청: 순수 손익 NAV)
    nav_actual_today_gross = (
        initial + total_realized + cur_unrealized_spot + cur_unrealized_futures
    )
    nav_actual_today = nav_actual_today_gross  # 신용 차감 안 함 (gross = net)

    # 6) 흰점선용 — 자산그래프 라인 그대로 (gross, 현물만, 신용 미차감)
    try:
        graph_rows = compute_profit_trend()
        graph_asset_by_d = {r["date"]: float(r["asset"]) for r in graph_rows}
    except Exception:
        print("[warn] compute_profit_trend 실패", file=sys.stderr)
        graph_asset_by_d = {}

    # 각 D(거래일만) 에 대해 동결-오늘 NAV — 식 통일
    # NAV = initial + 누적 실현(D 까지, 현물+선물) + 미실현(D 보유 × 오늘가, 현물+선물)
    rows: list[dict] = []
    for d_ in trade_dates:
        unreal_frozen = _unrealized_frozen(d_)
        cum_real_d = cum_realized_by_d.get(d_, 0.0)
        credit_d = credit_by_d.get(d_, 0.0)  # 표시용 (식에는 안 들어감)
        nav_frozen_today_gross = initial + cum_real_d + unreal_frozen
        nav_frozen_today = nav_frozen_today_gross  # 신용 차감 안 함

        # 그날 실제 NAV (흰점선) — 자산그래프 와 동일 식 (선물 포함, 신용 미차감)
        nav_actual_d = graph_asset_by_d.get(d_, nav_frozen_today_gross)

        rows.append({
            "date": d_,
            "nav_frozen_today": nav_frozen_today,
            "nav_frozen_today_gross": nav_frozen_today_gross,
            "nav_actual_d": nav_actual_d,
            "credit_d": credit_d,
            "cum_realized_d": cum_real_d,
            "unrealized_frozen": unreal_frozen,
        })

    higher = [r for r in rows if r["nav_frozen_today"] > nav_actual_today]

    # 상위 3 거래일에 대해 그날 잔고 상세 (현물+선물+신용)
    top3 = sorted(higher, key=lambda r: r["nav_frozen_today"], reverse=True)[:3]
    top3_details: list[dict] = []
    for r in top3:
        d_ = r["date"]
        spot_items = []
        for n, (q, avg) in hold_avg.get(d_, {}).items():
            p_then = _close_for(n, d_)
            p_now = _today_price_for(n)
            eval_then = q * p_then if p_then is not None else None
            eval_now = q * p_now if p_now is not None else None
            spot_items.append({
                "name": n,
                "qty": q,
                "avg": avg,
                "price_then": p_then,
                "price_now": p_now,
                "eval_then": eval_then,
                "eval_now": eval_now,
                "credit_then": credit_per_name_by_d.get(d_, {}).get(n, 0.0),
            })
        spot_items.sort(
            key=lambda x: (x["eval_now"] or 0), reverse=True
        )
        fut_items = []
        for pid, fp in fut_hold.get(d_, {}).items():
            ctr = int(fp.get("contracts", 0))
            if ctr <= 0:
                continue
            name = fp.get("name", "")
            avg = float(fp.get("avg_entry", 0))
            mult = int(fp.get("multiplier", 10))
            dir_sign = 1 if fp.get("direction") == "long" else -1
            mgn = float(fp.get("margin", 0))
            p_then = _close_for(name, d_)
            p_now = _today_price_for(name)
            val_then = (
                mgn + (p_then - avg) * ctr * mult * dir_sign
                if p_then is not None else None
            )
            val_now = (
                mgn + (p_now - avg) * ctr * mult * dir_sign
                if p_now is not None else None
            )
            fut_items.append({
                "name": name,
                "direction": fp.get("direction", "long"),
                "contracts": ctr,
                "avg_entry": avg,
                "multiplier": mult,
                "margin": mgn,
                "price_then": p_then,
                "price_now": p_now,
                "val_then": val_then,
                "val_now": val_now,
            })
        fut_items.sort(key=lambda x: (x["val_now"] or 0), reverse=True)
        top3_details.append({
            "date": d_,
            "nav_frozen_today": r["nav_frozen_today"],
            "nav_actual_d": r["nav_actual_d"],
            "diff": r["nav_frozen_today"] - nav_actual_today,
            "cum_realized_d": r["cum_realized_d"],
            "unrealized_frozen": r["unrealized_frozen"],
            "credit_d": r["credit_d"],
            "spot": spot_items,
            "futures": fut_items,
        })

    summary_text = (
        f"=== 백테스트: 포트폴리오 동결 시 오늘 NAV ===\n"
        f"식: initial + 누적 실현(현물+선물) + 미실현(현물+선물)\n"
        f"기간: {rows[0]['date']} ~ {rows[-1]['date']}  (거래일 {len(rows)}일)\n"
        f"현재 NAV: {_fmt_krw(nav_actual_today)}  "
        f"(= 초기자본 {_fmt_krw(initial)} + 실현 {_fmt_krw(total_realized)} "
        f"+ 미실현 {_fmt_krw(cur_unrealized_spot + cur_unrealized_futures)})\n"
        f"수익률: {(nav_actual_today/initial-1)*100:+.1f}%\n"
        f"→ 현재 NAV 초과 거래일: {len(higher)}/{len(rows)}건"
    )
    table_lines = [
        f"  {'날짜':<12} {'동결-오늘 NAV':>16} {'vs 현재':>14} {'그날 실제 NAV':>16}  {'표시'}",
    ]
    for r in rows:
        diff = r["nav_frozen_today"] - nav_actual_today
        sign = "+" if diff > 0 else ""
        marker = " ★ 초과" if diff > 0 else ""
        table_lines.append(
            f"  {r['date'].isoformat():<12} "
            f"{_fmt_krw(r['nav_frozen_today']):>16} "
            f"{sign + _fmt_krw(diff):>14} "
            f"{_fmt_krw(r['nav_actual_d']):>16}"
            f"{marker}"
        )
    table_text = "\n".join(table_lines)

    # 그래프
    _setup_korean_font()
    xs = [r["date"] for r in rows]
    ys_frozen = [r["nav_frozen_today"] for r in rows]
    ys_actual_d = [r["nav_actual_d"] for r in rows]

    fig, ax = plt.subplots(figsize=(11, 5.8), dpi=120)
    ax.plot(xs, ys_actual_d, color="#9ca3af", linewidth=1.4, linestyle=":",
            label="실제 그날 자산 (자산그래프와 동일)", zorder=3)
    ax.plot(xs, ys_frozen, color="#3b82f6", linewidth=2.4,
            label="그날 동결 → 오늘 순자산", zorder=5)
    ax.axhline(nav_actual_today, color="#f59e0b", linewidth=1.6, linestyle="--",
               label=f"현재 NAV {_fmt_krw(nav_actual_today)}", zorder=4)
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
    ax.set_ylabel("NAV (KRW) = 초기 + 실현 + 미실현", color="#9ca3af", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=12))
    ax.legend(loc="lower right", facecolor="#16161e", edgecolor="#333",
              labelcolor="#ddd", fontsize=9)
    ax.set_title(
        f"포트폴리오 동결 백테스트 — 현물+선물 — "
        f"{start:%Y-%m-%d} ~ {today:%Y-%m-%d}",
        color="#fff", fontsize=12, loc="left", pad=12,
    )
    fig.tight_layout()

    # PNG → 메모리 + 디스크 양쪽
    png_buf = _io.BytesIO()
    fig.savefig(png_buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    png_buf.seek(0)
    out = ROOT / "reports" / f"backtest_frozen_{today:%Y%m%d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png_buf.getvalue())
    png_buf.seek(0)

    return {
        "rows": rows,
        "nav_actual_today": nav_actual_today,            # 신용 제외 순자산
        "nav_actual_today_gross": nav_actual_today_gross,  # 총자산 (신용 포함)
        "cur_credit": cur_credit,
        "cur_holdings_value": cur_holdings_value,
        "cur_futures_value": cur_futures_value,
        "today_total_cash": today_total_cash,
        "initial": initial,
        "higher": higher,
        "top3_details": top3_details,
        "png_buf": png_buf,
        "png_path": out,
        "summary_text": summary_text,
        "table_text": table_text,
        "start": start,
        "today": today,
    }


def main() -> int:
    res = run_backtest()
    if res is None:
        print("거래 내역 없음.")
        return 1
    print()
    print(res["summary_text"])
    print()
    print(res["table_text"])
    print()
    print(f"PNG 저장: {res['png_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
