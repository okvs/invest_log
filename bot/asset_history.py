"""기록 첫날부터 현재까지 일별 NAV(총 자산) 재구성 + 그래프 렌더.

데이터 소스:
  - data/transactions.json — 현물 매수/매도
  - data/futures_transactions.json — 선물 open/close/roll
  - data/cash_events.json — 입출금 이벤트 (없으면 자동 시드)
  - data/account.json — 현재 cash·initial_capital anchor

산출물:
  - 일별 NAV = 현물평가금 + 현금 + 선물 미실현손익
  - PNG 그래프 (dual Y: 천만 KRW + 첫날 대비 %)
"""
from __future__ import annotations

import io
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import yfinance as yf
from matplotlib import font_manager
from matplotlib.ticker import FuncFormatter

from storage.json_store import (
    add_cash_event,
    load_account,
    load_cash_events,
    load_futures_transactions,
    load_holdings,
    load_ticker_map,
    load_transactions,
)

logger = logging.getLogger(__name__)

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
    """ISO 문자열에서 YYYY-MM-DD 추출."""
    return (s or "")[:10]


# ---------------------------------------------------------------------------
# Cash events 시드
# ---------------------------------------------------------------------------

def _ensure_seed_on_disk() -> None:
    """디스크에 seed 이벤트가 없으면 초기자본을 첫 거래일에 등록.

    user 이벤트만 디스크에 저장한다. auto 보정은 매번 in-memory로 계산.
    """
    events = load_cash_events()
    if any(e.get("type") == "seed" for e in events):
        return
    account = load_account()
    initial = float(account.get("initial_capital") or 0)
    if initial <= 0:
        return
    txs = load_transactions()
    first_iso = min(
        (_date_only(t.get("date", "")) for t in txs if t.get("date")),
        default=date.today().isoformat(),
    )
    add_cash_event(first_iso, initial, "seed", "초기자본 anchor")


def get_all_cash_events() -> list[dict]:
    """user 이벤트 + 자동 보정 이벤트 (in-memory). 디스크에는 user 만 저장.

    절차:
      1) seed 가 없으면 디스크에 추가
      2) user 이벤트로 cash 트래젝토리 시뮬레이션
      3) cash 가 음수가 되는 날마다 auto deposit 추가 (`source=auto`)
      4) 끝에서 account.cash 와 잔차가 있으면 today 에 auto 보정
    """
    _ensure_seed_on_disk()
    user_events = load_cash_events()
    account = load_account()
    current_cash = float(account.get("cash") or 0)
    initial = float(account.get("initial_capital") or 0)
    if initial <= 0 or not user_events:
        return [{**e, "source": "user"} for e in user_events]

    txs = load_transactions()
    buys_by_day: dict[date, float] = defaultdict(float)
    sells_by_day: dict[date, float] = defaultdict(float)
    for t in txs:
        d_str = _date_only(t.get("date", ""))
        if not d_str:
            continue
        try:
            td = date.fromisoformat(d_str)
        except ValueError:
            continue
        amt = float(t.get("total_amount", 0))
        if t.get("type") == "buy":
            buys_by_day[td] += amt
        elif t.get("type") == "sell":
            sells_by_day[td] += amt

    user_by_day: dict[date, float] = defaultdict(float)
    for e in user_events:
        try:
            ed = date.fromisoformat(e.get("date", ""))
        except (ValueError, TypeError):
            continue
        amt = float(e.get("amount", 0))
        if e.get("type") == "withdraw":
            amt = -amt
        user_by_day[ed] += amt

    first_d = min(
        (date.fromisoformat(_date_only(t.get("date", "")))
         for t in txs if t.get("date")),
        default=date.today(),
    )
    today = date.today()

    auto_events: list[dict] = []
    cash = 0.0
    d = first_d
    while d <= today:
        cash += user_by_day[d] + sells_by_day[d] - buys_by_day[d]
        if cash < 0:
            needed = -cash
            auto_events.append({
                "date": d.isoformat(), "amount": needed,
                "type": "deposit", "note": "기록 보정(자동 추정)",
                "source": "auto",
            })
            cash = 0
        d += timedelta(days=1)

    residual = current_cash - cash
    if abs(residual) > 1000:
        ev_type = "deposit" if residual > 0 else "withdraw"
        auto_events.append({
            "date": today.isoformat(), "amount": abs(residual),
            "type": ev_type, "note": "기록 보정(자동 추정)",
            "source": "auto",
        })

    combined = [{**e, "source": "user"} for e in user_events] + auto_events
    combined.sort(key=lambda e: e.get("date", ""))
    return combined


# 하위 호환용 alias — 이전 코드가 ensure_seed_cash_event() 호출하던 곳을 위해
def ensure_seed_cash_event() -> list[dict]:
    return get_all_cash_events()


# ---------------------------------------------------------------------------
# 일별 보유수량 재구성
# ---------------------------------------------------------------------------

def _name_to_ticker_map(holdings: list[dict]) -> dict[str, str]:
    """현재 보유 + ticker_map 캐시에서 종목명→ticker 매핑."""
    out: dict[str, str] = {}
    for h in holdings:
        n = h.get("name", "")
        t = h.get("ticker", "")
        if n and t:
            out[n] = t
    for n, t in load_ticker_map().items():
        out.setdefault(n, t)
    return out


def reconstruct_spot_holdings_by_date(
    transactions: list[dict], dates: list[date],
) -> tuple[dict[date, dict[str, int]], set[str]]:
    """각 date 종가 시점의 보유수량(name→qty).

    Returns: (holdings_by_date, names_seen)
    """
    # 종목별 매수/매도 이벤트
    by_name: dict[str, list[tuple[date, int]]] = defaultdict(list)
    names_seen: set[str] = set()
    for t in transactions:
        d = _date_only(t.get("date", ""))
        if not d:
            continue
        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue
        name = t.get("name", "")
        if not name:
            continue
        qty = int(t.get("quantity", 0))
        if t.get("type") == "sell":
            qty = -qty
        elif t.get("type") != "buy":
            continue
        by_name[name].append((day, qty))
        names_seen.add(name)

    out: dict[date, dict[str, int]] = {d: {} for d in dates}
    for name, evs in by_name.items():
        evs.sort(key=lambda x: x[0])
        running = 0
        idx = 0
        for d in dates:
            while idx < len(evs) and evs[idx][0] <= d:
                running += evs[idx][1]
                idx += 1
            # 누락된 buy 등으로 인해 음수가 되면 0으로 클램프
            qty = max(0, running)
            if qty > 0:
                out[d][name] = qty
    return out, names_seen


def reconstruct_futures_by_date(
    fut_transactions: list[dict],
    current_positions: list[dict],
    dates: list[date],
) -> dict[date, list[dict]]:
    """각 date 시점의 활성 선물 포지션 리스트.

    현재 활성 포지션(`current_positions`)을 기준으로, 해당 position_id의
    open/close/roll_* 이벤트를 정방향으로 적용해 일별 상태를 만든다.
    open tx가 없는 시드 포지션은 첫 거래일부터 현재 상태 그대로 활성으로 가정.

    각 포지션: {name, symbol, contract_month, direction, contracts, avg, multiplier}.
    """
    by_pid: dict[str, list[dict]] = defaultdict(list)
    for t in fut_transactions:
        pid = t.get("position_id", "")
        if pid:
            by_pid[pid].append(t)
    for evs in by_pid.values():
        evs.sort(key=lambda e: e.get("date", ""))

    out: dict[date, list[dict]] = {d: [] for d in dates}
    if not dates:
        return out

    for pos in current_positions:
        if pos.get("contracts", 0) <= 0:
            continue
        pid = pos.get("id", "")
        evs = by_pid.get(pid, [])
        first_open = next(
            (e for e in evs if e.get("type") in ("open", "roll_open")), None,
        )
        if first_open:
            try:
                start_d = date.fromisoformat(_date_only(first_open["date"]))
            except (ValueError, KeyError):
                start_d = dates[0]
            state = {"contracts": 0, "avg": 0.0}
        else:
            # 시드 포지션 — 첫 거래일부터 현재값 그대로 적용
            start_d = dates[0]
            state = {
                "contracts": int(pos.get("contracts", 0)),
                "avg": float(pos.get("avg_entry_price", 0)),
            }

        ev_idx = 0
        for d in dates:
            if d < start_d:
                continue
            while ev_idx < len(evs):
                try:
                    ed = date.fromisoformat(_date_only(evs[ev_idx]["date"]))
                except (ValueError, KeyError):
                    ev_idx += 1
                    continue
                if ed > d:
                    break
                e = evs[ev_idx]
                etype = e.get("type", "")
                qty = int(e.get("contracts", 0))
                price = float(e.get("price", 0))
                if etype in ("open", "roll_open"):
                    new_c = state["contracts"] + qty
                    if new_c > 0:
                        state["avg"] = (
                            state["avg"] * state["contracts"] + price * qty
                        ) / new_c
                    state["contracts"] = new_c
                elif etype in ("close", "roll_close"):
                    state["contracts"] = max(0, state["contracts"] - qty)
                ev_idx += 1
            if state["contracts"] > 0:
                out[d].append({
                    "name": pos.get("name", ""),
                    "symbol": pos.get("symbol", ""),
                    "contract_month": pos.get("contract_month", ""),
                    "direction": pos.get("direction", "long"),
                    "contracts": state["contracts"],
                    "avg": state["avg"],
                    "multiplier": int(pos.get("multiplier", 10)),
                })
    return out


# ---------------------------------------------------------------------------
# 가격 history
# ---------------------------------------------------------------------------

def fetch_price_history(
    tickers: list[str], start: date, end: date,
) -> pd.DataFrame:
    """yfinance batch download → date-indexed wide DataFrame (columns=tickers).

    영업일 외 weekend 행은 ffill (이전 종가 유지).
    """
    if not tickers:
        return pd.DataFrame()
    # end 는 inclusive 가 되도록 +1
    try:
        df = yf.download(
            tickers, start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            progress=False, auto_adjust=False, group_by="ticker",
            threads=True,
        )
    except Exception:
        logger.exception("yfinance.download 실패 — tickers=%s", tickers)
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()

    # 단일 ticker / 멀티 ticker 모두 → wide 형태 (date×ticker)
    if isinstance(df.columns, pd.MultiIndex):
        closes = df.xs("Close", axis=1, level=1)
    else:
        closes = df[["Close"]].rename(columns={"Close": tickers[0]})
    closes.index = pd.to_datetime(closes.index).date
    closes = closes.sort_index()
    return closes


# ---------------------------------------------------------------------------
# 일별 NAV
# ---------------------------------------------------------------------------

def compute_daily_nav() -> list[dict]:
    """일별 NAV row 리스트 반환.

    각 row: {date, nav, cash, spot_eval, fut_unrealized, deposits_today}
    """
    from storage.json_store import load_futures_positions  # noqa: E402

    transactions = load_transactions()
    fut_transactions = load_futures_transactions()
    cash_events = ensure_seed_cash_event()
    holdings = load_holdings()
    futures_positions = [
        p for p in load_futures_positions() if p.get("contracts", 0) > 0
    ]
    name_to_ticker = _name_to_ticker_map(holdings)

    # 날짜 범위
    all_dates = [_date_only(t.get("date", "")) for t in transactions]
    all_dates += [_date_only(t.get("date", "")) for t in fut_transactions]
    all_dates += [e.get("date", "") for e in cash_events]
    all_dates = [d for d in all_dates if d]
    if not all_dates:
        return []
    start = date.fromisoformat(min(all_dates))
    end = date.today()
    dates: list[date] = []
    d = start
    while d <= end:
        dates.append(d)
        d += timedelta(days=1)

    # 보유수량 재구성
    spot_by_date, names_seen = reconstruct_spot_holdings_by_date(transactions, dates)
    fut_by_date = reconstruct_futures_by_date(fut_transactions, futures_positions, dates)

    # 시드 포지션(거래내역에 open 이벤트 없음) 식별 — 첫날 종가를 baseline 로 사용해
    # 첫날부터의 가격 변화만 미실현으로 반영(과거 진입가에 의한 잠재 손익은 0으로 시작).
    seeded_keys: set[tuple[str, str, str]] = set()
    for pos in futures_positions:
        pid = pos.get("id", "")
        has_open = any(
            t.get("position_id") == pid and t.get("type") in ("open", "roll_open")
            for t in fut_transactions
        )
        if not has_open:
            seeded_keys.add((
                pos.get("name", ""), pos.get("contract_month", ""),
                pos.get("direction", ""),
            ))

    # 필요한 ticker 모음
    tickers: set[str] = set()
    for name in names_seen:
        t = name_to_ticker.get(name, "")
        if t:
            tickers.add(t)
    # 선물 기초자산 — symbol 있으면 symbol+suffix, 없으면 종목명→ticker_map fallback
    fut_ticker_by_pos: dict[tuple[str, str], str] = {}  # (name, symbol) → ticker
    for snaps in fut_by_date.values():
        for s in snaps:
            sym = (s.get("symbol") or "").strip()
            name = s.get("name", "")
            key = (name, sym)
            if key in fut_ticker_by_pos:
                continue
            resolved = ""
            if sym:
                # 6자리 symbol 이면 .KS / .KQ 추측 (KOSPI 우선)
                resolved = sym + ".KS"
            else:
                resolved = name_to_ticker.get(name, "")
            if resolved:
                tickers.add(resolved)
                fut_ticker_by_pos[key] = resolved

    price_df = fetch_price_history(sorted(tickers), start, end)

    # 일별 가격 lookup (영업일만 있으니 ffill)
    if not price_df.empty:
        full_index = pd.date_range(start, end, freq="D").date
        price_df = price_df.reindex(full_index).ffill()

    def _price(ticker: str, d: date) -> float | None:
        if price_df.empty or ticker not in price_df.columns:
            return None
        try:
            v = price_df.loc[d, ticker]
        except KeyError:
            return None
        if pd.isna(v):
            return None
        return float(v)

    # 일별 cash 이벤트 합산
    deposits_by_date: dict[date, float] = defaultdict(float)
    for e in cash_events:
        try:
            ed = date.fromisoformat(e.get("date", ""))
        except (ValueError, TypeError):
            continue
        amt = float(e.get("amount", 0))
        if e.get("type") == "withdraw":
            amt = -amt
        deposits_by_date[ed] += amt

    # 일별 buy/sell 총액
    buys_by_date: dict[date, float] = defaultdict(float)
    sells_by_date: dict[date, float] = defaultdict(float)
    for t in transactions:
        d_str = _date_only(t.get("date", ""))
        if not d_str:
            continue
        try:
            td = date.fromisoformat(d_str)
        except ValueError:
            continue
        amt = float(t.get("total_amount", 0))
        if t.get("type") == "buy":
            buys_by_date[td] += amt
        elif t.get("type") == "sell":
            sells_by_date[td] += amt

    rows: list[dict] = []
    running_cash = 0.0
    for d in dates:
        running_cash += deposits_by_date[d] + sells_by_date[d] - buys_by_date[d]

        spot_eval = 0.0
        for name, qty in spot_by_date[d].items():
            ticker = name_to_ticker.get(name, "")
            p = _price(ticker, d) if ticker else None
            if p is None:
                # 가격을 못 구하면 평균매입가로 fallback — 적어도 음수는 안 됨
                p = 0.0
                # tx 매수단가 평균으로라도 채우려면 별도 로직 필요. draft 에선 0.
            spot_eval += qty * p

        fut_unrealized = 0.0
        for s in fut_by_date[d]:
            sym = (s.get("symbol") or "").strip()
            name = s.get("name", "")
            ticker = fut_ticker_by_pos.get((name, sym), "")
            p = _price(ticker, d) if ticker else None
            if p is None and sym:
                # KOSPI 실패하면 KOSDAQ 한 번 더 시도
                p = _price(sym + ".KQ", d)
            if p is None:
                continue
            key = (name, s.get("contract_month", ""), s.get("direction", ""))
            if key in seeded_keys and ticker:
                baseline = _price(ticker, dates[0]) or s["avg"]
            else:
                baseline = s["avg"]
            sign = 1 if s.get("direction") == "long" else -1
            fut_unrealized += (p - baseline) * s["contracts"] * s["multiplier"] * sign

        nav = spot_eval + running_cash + fut_unrealized
        rows.append({
            "date": d,
            "nav": nav,
            "cash": running_cash,
            "spot_eval": spot_eval,
            "fut_unrealized": fut_unrealized,
            "deposits_today": deposits_by_date[d],
        })
    return rows


# ---------------------------------------------------------------------------
# 그래프 렌더
# ---------------------------------------------------------------------------

def _format_krw_short(x: float, _pos) -> str:
    """3,500만 / 1.5억 처럼 천만/억 단위로 라벨링."""
    if x == 0:
        return "0"
    eok = x / 1e8
    if abs(eok) >= 1:
        return f"{eok:.1f}억"
    chunman = x / 1e7
    return f"{chunman:.0f}천만"


def render_asset_graph() -> io.BytesIO | None:
    """일별 NAV 그래프 PNG bytes 반환."""
    _setup_korean_font()
    rows = compute_daily_nav()
    if not rows:
        return None

    dates = [r["date"] for r in rows]
    navs = [r["nav"] for r in rows]
    first_nav = navs[0] if navs and navs[0] != 0 else 1.0
    pct = [(n / first_nav - 1) * 100 for n in navs]

    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=120)
    ax.plot(dates, navs, color="#fbbf24", linewidth=2.0, zorder=3)
    ax.fill_between(dates, [0] * len(navs), navs,
                    color="#fbbf24", alpha=0.08, zorder=1)

    # 천만 단위 ticks
    nav_max = max(navs)
    nav_min = min(navs + [0])
    pad = (nav_max - nav_min) * 0.10 or nav_max * 0.10
    ax.set_ylim(max(0, nav_min - pad), nav_max + pad)
    ax.yaxis.set_major_formatter(FuncFormatter(_format_krw_short))

    # 우측 축: 첫날 대비 %
    ax2 = ax.twinx()
    y_lo, y_hi = ax.get_ylim()
    ax2.set_ylim((y_lo / first_nav - 1) * 100, (y_hi / first_nav - 1) * 100)
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f}%"))
    ax2.tick_params(colors="#9ca3af", labelsize=9)
    ax2.set_ylabel("첫날 대비 (%)", color="#9ca3af", fontsize=9)

    # 입출금 이벤트 마커
    events = get_all_cash_events()
    # 같은 X 위치에 라벨이 겹치지 않도록 stagger
    label_offsets: dict[date, int] = {}
    for ev in events:
        try:
            ed = date.fromisoformat(ev.get("date", ""))
        except (ValueError, TypeError):
            continue
        if ed < dates[0] or ed > dates[-1]:
            continue
        amt = float(ev.get("amount", 0))
        ev_type = ev.get("type", "")
        is_seed = ev_type == "seed"
        is_withdraw = ev_type == "withdraw"
        is_auto = ev.get("source") == "auto"
        # seed: 옅은 회색 vline만, 라벨 없음
        if is_seed:
            ax.axvline(ed, color="#6b7280", linewidth=0.6, linestyle=":", alpha=0.4, zorder=2)
            continue
        color = "#ef4444" if is_withdraw else "#22c55e"
        alpha = 0.40 if is_auto else 0.75
        ax.axvline(ed, color=color, linewidth=0.7, linestyle="--", alpha=alpha, zorder=2)
        marker = "▼" if is_withdraw else "▲"
        amt_label = f"{abs(amt) / 1e7:.1f}천만" if abs(amt) < 1e8 else f"{abs(amt) / 1e8:.2f}억"
        label = f"{marker}{amt_label}{'*' if is_auto else ''}"
        offset = label_offsets.get(ed, 0)
        label_offsets[ed] = offset + 14
        ax.annotate(
            label, xy=(ed, ax.get_ylim()[1]),
            xytext=(0, -10 - offset), textcoords="offset points",
            color=color, fontsize=7 if is_auto else 8,
            ha="center", va="top", alpha=alpha + 0.2,
        )

    # 첫날·마지막 라벨
    ax.scatter(dates[0], navs[0], color="#fbbf24", s=44, zorder=5)
    ax.scatter(dates[-1], navs[-1], color="#fbbf24", s=44, zorder=5)
    ax.text(dates[-1], navs[-1], f"  {navs[-1] / 1e7:.0f}천만 ({pct[-1]:+.1f}%)",
            color="#fbbf24", fontsize=9, va="center")

    # 스타일
    ax.set_facecolor("#0f0f14")
    fig.patch.set_facecolor("#0f0f14")
    for spine in ("top",):
        ax.spines[spine].set_visible(False)
        ax2.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color("#444")
    ax2.spines["right"].set_color("#444")
    ax.tick_params(colors="#9ca3af", labelsize=9)
    ax.set_ylabel("총 자산 (KRW)", color="#9ca3af", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=10))
    ax.set_title(
        f"자산 추이 — {dates[0]:%Y-%m-%d} ~ {dates[-1]:%Y-%m-%d}",
        color="#fff", fontsize=12, loc="left", pad=12,
    )
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf
