"""기록 첫날부터 현재까지 수익금·평가금 추이 재구성 + 그래프 렌더.

cash/신용 역산이 아니라 정확한 실현손익(매도 기록) + 시세 기반 미실현으로
구성해 매도된 종목의 과거 시세 공백 영향을 최소화한다.

데이터 소스:
  - data/transactions.json — 현물 매수/매도 (profit_loss 포함)
  - data/cash_events.json — 입출금 이벤트
  - data/account.json — initial_capital anchor
  - pykrx — 보유 종목 일별 종가 (미실현 평가)

산출물 (compute_profit_trend):
  - asset = 초기자본 + 누적 입출금 + 누적 실현손익 + 당일 미실현
  - PNG 그래프 3선: 평가금 / 실현+미실현 / 누적 실현손익
"""
from __future__ import annotations

import io
import logging
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

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
# 종목명 → ticker 매핑
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


# ---------------------------------------------------------------------------
# 수익금·평가금 추이 (실현손익 + pykrx 미실현)
# ---------------------------------------------------------------------------

def _reconstruct_holdings_with_avg(
    transactions: list[dict], dates: list[date],
) -> dict[date, dict[str, tuple[int, float]]]:
    """각 date 시점의 보유 {종목: (수량, 평균단가)}.

    매수: 평균단가 가중평균 갱신. 매도: 수량만 차감(평단 유지), 0주면 리셋.
    """
    ev: dict[str, list[dict]] = defaultdict(list)
    for t in sorted(transactions, key=lambda x: x.get("date", "")):
        if t.get("date"):
            ev[t.get("name", "")].append(t)
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


def _fetch_pykrx_closes(
    codes, start: date, end: date,
) -> dict[tuple[str, date], float]:
    """6자리 종목코드별 일별 종가. pykrx(get_market_ohlcv_by_date) 사용."""
    out: dict[tuple[str, date], float] = {}
    try:
        from pykrx import stock
    except ImportError:
        logger.warning("pykrx 미설치 — 미실현 시세 조회 불가")
        return out
    s8 = start.strftime("%Y%m%d")
    e8 = end.strftime("%Y%m%d")
    for code in {c for c in codes if c}:
        try:
            df = stock.get_market_ohlcv_by_date(s8, e8, code)
            for dt, row in df.iterrows():
                out[(code, dt.date())] = float(row["종가"])
        except Exception:
            logger.debug("pykrx 종가 조회 실패 code=%s", code, exc_info=True)
    return out


def _load_futures_tx() -> list[dict]:
    from storage.json_store import load as _load_json
    return _load_json("futures_transactions.json").get("transactions", [])


def _load_futures_pos() -> list[dict]:
    from storage.json_store import load as _load_json
    return _load_json("futures_positions.json").get("positions", [])


# 이름 정규화 — 백테스트와 동일한 alias
_NAME_ALIAS = {
    "G넥스원": "LIG넥스원",
    "반도체레버리지": "KODEX반도체레버리지",
    "LIG디펜스앤에어로스페이스": "LIG넥스원",
}


def _canon(name: str) -> str:
    return _NAME_ALIAS.get(name, name)


def _reconstruct_daily_futures(
    futures_tx: list[dict], seed_positions: list[dict], dates: list[date]
) -> dict[date, dict[str, dict]]:
    """각 date 의 선물 포지션 {pid: {name, dir, contracts, avg_entry, mult}}.

    seed positions(전부 백로딩 + entry_date 시점에 open 가정) + transactions 합성.
    """
    # seed → synthetic open
    tx_pids = {t.get("position_id") for t in futures_tx if t.get("position_id")}
    synthetic: list[dict] = []
    for p in seed_positions:
        pid = p.get("id")
        if pid in tx_pids:
            continue
        synthetic.append({
            "id": f"seed-{pid}",
            "type": "open",
            "name": p.get("name", ""),
            "direction": p.get("direction", "long"),
            "contracts": int(p.get("contracts", 0)),
            "price": float(p.get("avg_entry_price", 0)),
            "multiplier": int(p.get("multiplier", 10)),
            "date": (p.get("entry_date", "") or "") + "T00:00:00",
            "position_id": pid,
        })
    all_tx = sorted(list(futures_tx) + synthetic, key=lambda x: x.get("date", ""))
    out: dict[date, dict[str, dict]] = {d: {} for d in dates}
    state: dict[str, dict] = {}
    idx = 0
    for d in dates:
        while idx < len(all_tx) and date.fromisoformat(_date_only(all_tx[idx]["date"])) <= d:
            t = all_tx[idx]
            pid = t.get("position_id") or t.get("id")
            ctr = int(t.get("contracts", 0))
            pr = float(t.get("price", 0))
            mult = int(t.get("multiplier", 10))
            ttype = t.get("type", "")
            if ttype in {"open", "roll_open"}:
                cur = state.get(pid)
                if cur is None:
                    state[pid] = {
                        "name": t.get("name", ""),
                        "direction": t.get("direction", "long"),
                        "contracts": ctr,
                        "avg_entry": pr,
                        "multiplier": mult,
                    }
                else:
                    nc = cur["contracts"] + ctr
                    if nc > 0:
                        cur["avg_entry"] = (
                            cur["avg_entry"] * cur["contracts"] + pr * ctr
                        ) / nc
                    cur["contracts"] = nc
            elif ttype in {"close", "roll_close"}:
                cur = state.get(pid)
                if cur:
                    cur["contracts"] -= ctr
                    if cur["contracts"] <= 0:
                        state.pop(pid, None)
            idx += 1
        out[d] = {pid: dict(v) for pid, v in state.items()}
    return out


def compute_profit_trend(target_nav: float | None = None) -> list[dict]:
    """일별 NAV 추이 — 식: initial + 누적 입금 + 누적 실현(현물+선물) + 미실현.

    각 row: {date, realized, unrealized, profit, deposits, asset}
      - realized   = 누적 실현 (현물 매도 profit_loss + 선물 청산 pnl)
      - unrealized = 그날 보유 미실현 (현물 종가−평단 + 선물 종가−평균진입)
      - profit     = realized + unrealized
      - deposits   = 누적 입출금
      - asset      = initial + 누적입금 + profit  (자기자본/equity)

    target_nav 가 주어지면(실제 잔고 = compute_balance_nav), 오늘 asset 이 그
    값과 일치하도록 잔차(residual)를 전 구간 asset 에 더해 보정한다. 잔차는
    pykrx↔실시세 가격차 + 시드/백로딩 포지션의 기록 밖 자본(초기자본+입금+실현
    으로 설명 안 되는 분)을 흡수한다 — 그래프 끝점을 실제 계좌와 맞추기 위함.
    """
    # 연금계좌 거래는 모든 자산/손익 계산에서 제외 (기록 탭에만 표시).
    transactions = [t for t in load_transactions() if not t.get("is_pension")]
    if not transactions:
        return []
    account = load_account()
    initial = float(account.get("initial_capital") or 0)
    name_to_ticker = _name_to_ticker_map(load_holdings())

    realized_by_day: dict[date, float] = defaultdict(float)
    for t in transactions:
        if t.get("type") == "sell" and t.get("date"):
            realized_by_day[date.fromisoformat(_date_only(t["date"]))] += float(
                t.get("profit_loss", 0) or 0
            )
    futures_tx = _load_futures_tx()
    for t in futures_tx:
        if t.get("type") in {"close", "roll_close"} and t.get("date"):
            realized_by_day[date.fromisoformat(_date_only(t["date"]))] += float(
                t.get("pnl", 0) or 0
            )

    dep_by_day: dict[date, float] = defaultdict(float)
    for e in load_cash_events():
        if e.get("type") == "seed":
            continue
        try:
            ed = date.fromisoformat(e.get("date", ""))
        except (ValueError, TypeError):
            continue
        a = float(e.get("amount", 0))
        dep_by_day[ed] += (-a if e.get("type") == "withdraw" else a)

    all_dates = [_date_only(t.get("date", "")) for t in transactions if t.get("date")]
    if not all_dates:
        return []
    start = date.fromisoformat(min(all_dates))
    end = date.today()
    dates: list[date] = []
    d = start
    while d <= end:
        dates.append(d)
        d += timedelta(days=1)

    # 현물 보유 (alias 적용)
    canon_tx = [dict(t, name=_canon(t.get("name", ""))) for t in transactions]
    hold_by_day = _reconstruct_holdings_with_avg(canon_tx, dates)

    # 종목 코드 매핑 (alias 적용)
    name_to_ticker_canon: dict[str, str] = {}
    for n, t in name_to_ticker.items():
        name_to_ticker_canon[_canon(n)] = t
    code_by_name: dict[str, str] = {}
    for n in {_canon(t.get("name", "")) for t in transactions}:
        tk = name_to_ticker_canon.get(n, "")
        if tk:
            code_by_name[n] = tk.split(".")[0]
    # 선물 기초자산도 필요
    for fp in _load_futures_pos():
        n = fp.get("name", "")
        sym = fp.get("symbol", "")
        if n and sym:
            code_by_name.setdefault(_canon(n), sym)

    price = _fetch_pykrx_closes(code_by_name.values(), start, end)

    def close_on(code: str, dd: date) -> float | None:
        cur = dd
        for _ in range(10):
            if (code, cur) in price:
                return price[(code, cur)]
            cur -= timedelta(days=1)
        return None

    # 일별 선물 포지션
    fut_by_day = _reconstruct_daily_futures(futures_tx, _load_futures_pos(), dates)

    rows: list[dict] = []
    rp = 0.0
    dp = 0.0
    for d in dates:
        rp += realized_by_day.get(d, 0.0)
        dp += dep_by_day.get(d, 0.0)
        unreal = 0.0
        # 현물 미실현
        for name, (qty, avg) in hold_by_day[d].items():
            code = code_by_name.get(name)
            c = close_on(code, d) if code else None
            if c is None:
                continue
            unreal += (c - avg) * qty
        # 선물 미실현 (mark-to-market, margin 제외)
        for pid, fp in fut_by_day[d].items():
            ctr = int(fp.get("contracts", 0))
            if ctr <= 0:
                continue
            code = code_by_name.get(_canon(fp.get("name", "")))
            c = close_on(code, d) if code else None
            if c is None:
                continue
            avg = float(fp.get("avg_entry", 0))
            mult = int(fp.get("multiplier", 10))
            dir_sign = 1 if fp.get("direction") == "long" else -1
            unreal += (c - avg) * ctr * mult * dir_sign
        rows.append({
            "date": d,
            "realized": rp,
            "unrealized": unreal,
            "profit": rp + unreal,
            "deposits": dp,
            "asset": initial + dp + rp + unreal,  # 자기자본(초기+입금+손익)
        })

    # 실제 잔고에 끝점 보정 (residual 을 전 구간에 더해 오늘 = target_nav)
    if target_nav is not None and rows:
        residual = float(target_nav) - rows[-1]["asset"]
        for r in rows:
            r["asset"] += residual
            r["residual"] = residual
    return rows


# ---------------------------------------------------------------------------
# 최근 N일 윈도우
# ---------------------------------------------------------------------------

def slice_recent_rows(rows: list[dict], days: int | None) -> list[dict]:
    """rows(일별 연속)에서 마지막 날 기준 최근 days일만 반환.

    days 가 None/0 이거나 데이터가 더 짧으면 전체 반환.
    """
    if not rows or not days or days <= 0:
        return rows
    cutoff = rows[-1]["date"] - timedelta(days=days - 1)
    return [r for r in rows if r["date"] >= cutoff]


# ---------------------------------------------------------------------------
# 그래프 렌더
# ---------------------------------------------------------------------------

def _format_krw_short(x: float, _pos) -> str:
    """Y축 눈금: 정수 억은 'N억', 나머지는 'N.N억' 또는 'N천만'.

    '1.0억'의 소수점이 안 보여 '10억'으로 오독되는 문제 때문에 정수 억은 점 생략.
    """
    if x == 0:
        return "0"
    eok = x / 1e8
    if abs(eok) >= 1:
        return f"{round(eok):.0f}억" if abs(eok - round(eok)) < 0.05 else f"{eok:.1f}억"
    return f"{x / 1e7:.0f}천만"


def _fmt_eok_label(x: float) -> str:
    """라벨용: 1억(10천만) 이상은 억, 미만은 천만."""
    if abs(x) >= 1e8:
        return f"{x / 1e8:.1f}억"
    return f"{x / 1e7:.0f}천만"


def _render_window_graph(rows: list[dict], days: int) -> io.BytesIO | None:
    """최근 N일 총자산(전부 청산 시 예수금) 단일 라인 그래프.

    전체 그래프와 달리 y축을 윈도우 내 자산 범위로 줌해 한 달간의
    등락이 또렷이 보이게 한다. 기준선은 윈도우 첫날 자산.
    """
    if len(rows) < 2:
        return None
    dates = [r["date"] for r in rows]
    asset = [r["asset"] for r in rows]
    base = asset[0]

    fig, ax = plt.subplots(figsize=(10, 5.8), dpi=120)

    # 윈도우 첫날 기준선
    ax.axhline(base, color="#9ca3af", linewidth=0.8, linestyle=":", alpha=0.6, zorder=1)
    ax.annotate(
        f"{dates[0]:%m-%d} {_fmt_eok_label(base)}", xy=(dates[0], base),
        xytext=(0, 6), textcoords="offset points",
        color="#9ca3af", fontsize=8, va="bottom", ha="left",
    )

    ax.plot(dates, asset, color="#3b82f6", linewidth=2.4, zorder=5, label="총자산 (전부 청산 시 예수금)")
    ax.fill_between(dates, base, asset, where=[a >= base for a in asset],
                    color="#3b82f6", alpha=0.10, zorder=2, interpolate=True)
    ax.fill_between(dates, base, asset, where=[a < base for a in asset],
                    color="#ef4444", alpha=0.10, zorder=2, interpolate=True)

    # 기간 고점/저점 마커 (끝점과 겹치면 생략)
    hi_i = max(range(len(asset)), key=lambda i: asset[i])
    lo_i = min(range(len(asset)), key=lambda i: asset[i])
    last_i = len(asset) - 1
    if hi_i != last_i:
        ax.scatter(dates[hi_i], asset[hi_i], color="#ef4444", s=30, zorder=6)
        ax.annotate(
            f"고점 {_fmt_eok_label(asset[hi_i])}", xy=(dates[hi_i], asset[hi_i]),
            xytext=(0, 8), textcoords="offset points",
            color="#f87171", fontsize=8, va="bottom", ha="center",
        )
    if lo_i != last_i:
        ax.scatter(dates[lo_i], asset[lo_i], color="#22c55e", s=30, zorder=6)
        ax.annotate(
            f"저점 {_fmt_eok_label(asset[lo_i])}", xy=(dates[lo_i], asset[lo_i]),
            xytext=(0, -10), textcoords="offset points",
            color="#4ade80", fontsize=8, va="top", ha="center",
        )

    # 끝점 라벨: 현재값 + 윈도우 시작 대비
    delta = asset[-1] - base
    pct = (delta / base * 100) if base else 0.0
    ax.scatter(dates[-1], asset[-1], color="#3b82f6", s=44, zorder=6)
    ax.annotate(
        f"{_fmt_eok_label(asset[-1])}\n({delta / 1e4:+,.0f}만 · {pct:+.1f}%)",
        xy=(dates[-1], asset[-1]), xytext=(10, 0), textcoords="offset points",
        color="#60a5fa", fontsize=9, va="center", ha="left",
    )

    # y축 줌 (윈도우 범위 + 6% 패딩)
    lo, hi = min(asset), max(asset)
    pad = max((hi - lo) * 0.06, hi * 0.002, 1.0)
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(dates[0], dates[-1] + timedelta(days=4))

    ax.yaxis.set_major_formatter(FuncFormatter(_format_krw_short))
    ax.set_facecolor("#0f0f14")
    fig.patch.set_facecolor("#0f0f14")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color("#444")
    ax.tick_params(colors="#9ca3af", labelsize=10)
    ax.set_ylabel("총자산 (KRW)", color="#9ca3af", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=10))
    ax.legend(loc="upper left", facecolor="#16161e", edgecolor="#333",
              labelcolor="#ddd", fontsize=9)
    ax.set_title(
        f"총자산 추이 (전부 청산 시 예수금) — 최근 {days}일 "
        f"({dates[0]:%Y-%m-%d} ~ {dates[-1]:%Y-%m-%d})",
        color="#fff", fontsize=12, loc="left", pad=12,
    )
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


def render_asset_graph(
    target_nav: float | None = None, days: int | None = None,
) -> io.BytesIO | None:
    """수익금·평가금 추이 PNG bytes 반환.

    3선: 평가금(자기자본, 실제 잔고 보정) / 실현+미실현 손익 / 누적 실현손익.
    target_nav 가 주어지면 평가금 끝점을 실제 잔고와 맞춘다.
    days 가 주어지면 최근 N일 총자산 단일 라인(윈도우 줌) 그래프로 렌더.
    """
    _setup_korean_font()
    rows = compute_profit_trend(target_nav=target_nav)
    if not rows:
        return None

    if days:
        return _render_window_graph(slice_recent_rows(rows, days), days)

    account = load_account()
    initial = float(account.get("initial_capital") or 0)

    dates = [r["date"] for r in rows]
    asset = [r["asset"] for r in rows]
    profit = [r["profit"] for r in rows]
    realized = [r["realized"] for r in rows]

    fig, ax = plt.subplots(figsize=(10, 5.8), dpi=120)
    ax.axhline(0, color="#555", linewidth=0.7, zorder=1)
    if initial > 0:
        ax.axhline(initial, color="#3b82f6", linewidth=0.7, linestyle=":", alpha=0.5, zorder=1)
        ax.annotate(
            f"초기자본 {_fmt_eok_label(initial)}", xy=(dates[0], initial),
            xytext=(0, 16), textcoords="offset points",
            color="#3b82f6", fontsize=8, va="bottom", ha="left",
        )

    ax.plot(dates, asset, color="#3b82f6", linewidth=2.4, zorder=5,
            label="평가금 (초기자본+실현+미실현)")
    ax.fill_between(dates, initial, asset, color="#3b82f6", alpha=0.06, zorder=2)
    ax.plot(dates, profit, color="#f59e0b", linewidth=2.0, zorder=4,
            label="실현+미실현 손익")
    ax.plot(dates, realized, color="#22c55e", linewidth=2.0, zorder=4,
            label="누적 실현손익")
    ax.fill_between(dates, realized, profit, color="#f59e0b", alpha=0.10, zorder=3)

    # 우측 여백 + 끝점 라벨 (선과 안 겹치게 offset)
    ax.set_xlim(dates[0], dates[-1] + timedelta(days=7))
    asset_pct = (asset[-1] / initial - 1) * 100 if initial > 0 else 0.0
    ax.scatter(dates[-1], asset[-1], color="#3b82f6", s=44, zorder=6)
    ax.annotate(
        f"{_fmt_eok_label(asset[-1])} ({asset_pct:+.1f}%)",
        xy=(dates[-1], asset[-1]), xytext=(10, 0), textcoords="offset points",
        color="#60a5fa", fontsize=9, va="center", ha="left",
    )
    ax.scatter(dates[-1], profit[-1], color="#f59e0b", s=40, zorder=6)
    ax.annotate(
        f"실현+미실현 {_fmt_eok_label(profit[-1])}",
        xy=(dates[-1], profit[-1]), xytext=(10, 6), textcoords="offset points",
        color="#f59e0b", fontsize=8, va="center", ha="left",
    )
    ax.scatter(dates[-1], realized[-1], color="#22c55e", s=40, zorder=6)
    ax.annotate(
        f"실현 {_fmt_eok_label(realized[-1])}",
        xy=(dates[-1], realized[-1]), xytext=(10, -6), textcoords="offset points",
        color="#22c55e", fontsize=8, va="center", ha="left",
    )

    ax.yaxis.set_major_formatter(FuncFormatter(_format_krw_short))
    ax.set_facecolor("#0f0f14")
    fig.patch.set_facecolor("#0f0f14")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color("#444")
    ax.tick_params(colors="#9ca3af", labelsize=10)
    ax.set_ylabel("자산 / 손익 (KRW)", color="#9ca3af", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=10))
    ax.legend(loc="upper left", facecolor="#16161e", edgecolor="#333",
              labelcolor="#ddd", fontsize=9)
    ax.set_title(
        f"수익금·평가금 추이 — {dates[0]:%Y-%m-%d} ~ {dates[-1]:%Y-%m-%d}",
        color="#fff", fontsize=12, loc="left", pad=12,
    )
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf
