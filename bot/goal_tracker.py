"""순자산 10억 목표 트래커 — 진척률·필요수익률·궤적·생존선 계산 + 그래프.

자산그래프(compute_profit_trend)의 NAV 시계열을 그대로 재사용해
"10억까지 얼마 남았고, 어떤 속도가 필요하고, 마진콜/드로다운까지 얼마나
버티는지"를 한 화면에 보여준다.

NAV 정의는 자산그래프와 100% 동일: asset = 초기자본 + 누적 실현 + 미실현.
(신용대출은 평단이 '전체 매입가' 기준이라 이 식의 미실현에 이미 반영돼 있어
 별도로 다시 차감하지 않는다 — progress.md 2026-05-28 통일 식과 일치.)

순수 계산 함수(required_cagr/realized_cagr/years_to_goal/trajectory_value/
drawdown_from_peak/margin_call_move/compute_margin_call)는 네트워크 없이 동작해
단위테스트 대상이고, compute_goal_status/render_goal_graph 만 시세에 의존한다.
"""
from __future__ import annotations

import io
import logging
import math
from datetime import date, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from bot.asset_history import (
    _format_krw_short,
    _setup_korean_font,
    compute_profit_trend,
)
from bot.futures_report import MAINTENANCE_RATIO
from storage.json_store import (
    load_account,
    load_futures_positions,
)

logger = logging.getLogger(__name__)

GOAL_KRW = 1_000_000_000          # 10억
HORIZONS_YEARS = (2, 5)           # 사용자 선택: 2년·5년 동시 표시
RUIN_DRAWDOWN = 0.25              # 파산방지선: 고점 대비 -25%
_DAYS_PER_YEAR = 365.25
_SHORT_SAMPLE_DAYS = 180          # 이보다 짧으면 연환산·도달예상은 '참고용'(표본 부족)


# ---------------------------------------------------------------------------
# 순수 계산 (네트워크 불필요 — 단위테스트 대상)
# ---------------------------------------------------------------------------

def required_cagr(current: float, goal: float, years: float) -> float:
    """current → goal 달성에 필요한 연복리 수익률(CAGR)."""
    if current <= 0 or years <= 0:
        return math.inf
    return (goal / current) ** (1.0 / years) - 1.0


def annual_to_monthly(annual: float) -> float:
    """연복리 수익률 → 월복리 수익률."""
    if not math.isfinite(annual):
        return annual
    return (1.0 + annual) ** (1.0 / 12.0) - 1.0


def realized_cagr(start: float, current: float, days: float) -> float | None:
    """기록 시작 이후 실현 연복리 수익률. 표본이 1일 미만이거나 비양수 NAV면 None."""
    if start <= 0 or current <= 0 or days < 1:
        return None
    return (current / start) ** (_DAYS_PER_YEAR / days) - 1.0


def years_to_goal(current: float, goal: float, annual_rate: float | None) -> float | None:
    """주어진 연복리 수익률로 목표 도달까지 걸리는 연수. 이미 달성이면 0, 불가면 None."""
    if current >= goal:
        return 0.0
    if annual_rate is None or annual_rate <= 0 or current <= 0:
        return None
    return math.log(goal / current) / math.log(1.0 + annual_rate)


def trajectory_value(
    start_nav: float, goal: float, total_years: float, elapsed_years: float,
) -> float:
    """start_nav 에서 total_years 후 goal 에 닿는 정속(일정 CAGR) 궤적의 elapsed 값."""
    if total_years <= 0 or start_nav <= 0:
        return start_nav
    frac = max(0.0, elapsed_years / total_years)
    return start_nav * (goal / start_nav) ** frac


def drawdown_from_peak(series: list[float]) -> tuple[float, float]:
    """(고점, 현재 낙폭). 낙폭은 0 이하(음수)."""
    if not series:
        return 0.0, 0.0
    peak = max(series)
    cur = series[-1]
    dd = (cur / peak - 1.0) if peak > 0 else 0.0
    return peak, dd


def margin_call_move(
    equity_now: float, maint_total: float, signed_notional: float,
) -> float | None:
    """보유 선물 현재가가 일괄 x 변동하면 순자산이 유지증거금에 닿는 x.

    순롱(signed_notional>0)이면 양수 x = 하락률, 순숏이면 양수 x = 상승률.
    notional 이 0이면 트리거 없음(None).
    """
    if signed_notional == 0:
        return None
    return (equity_now - maint_total) / signed_notional


def compute_margin_call(
    positions: list[dict],
    futures_prices: dict | None,
    futures_cash: float | None,
    maintenance_ratio: float | None = None,
) -> dict | None:
    """보유 선물 일괄 변동 마진콜 거리 계산 (futures_report 배너와 동일 식).

    futures_prices 포맷은 fetch_futures_quotes 와 동일:
      {"<symbol>|<YYYYMM>": {"price": ..., ...}} 또는 {"<symbol>": <price>}.
    시세를 못 구한 포지션은 notional/미실현에서 제외(priced 로 노출).
    """
    active = [p for p in positions if p.get("contracts", 0) > 0]
    if not active:
        return None
    prices = futures_prices or {}
    total_margin = 0.0
    unreal = 0.0
    signed_notional = 0.0
    priced = 0
    for p in active:
        sym = p.get("symbol", "")
        cm = p.get("contract_month", "")
        entry = prices.get(f"{sym}|{cm}")
        if entry is None:
            entry = prices.get(sym)
        cur = entry.get("price") if isinstance(entry, dict) else entry
        avg = float(p.get("avg_entry_price", 0))
        ctr = int(p.get("contracts", 0))
        mult = int(p.get("multiplier", 10))
        sign = 1 if p.get("direction") == "long" else -1
        total_margin += float(p.get("initial_margin", 0))
        if cur is not None:
            cur = float(cur)
            unreal += (cur - avg) * ctr * mult * sign
            signed_notional += cur * ctr * mult * sign
            priced += 1

    if maintenance_ratio and maintenance_ratio > 0:
        maint_total = total_margin * float(maintenance_ratio)
        maint_note = f"위탁 × {float(maintenance_ratio) * 100:.1f}%"
    else:
        maint_total = total_margin * MAINTENANCE_RATIO
        maint_note = "위탁 × 2/3 가정"

    fc = float(futures_cash or 0)
    equity_now = fc + total_margin + unreal
    x_call = margin_call_move(equity_now, maint_total, signed_notional)
    return {
        "x_call": x_call,
        "equity_now": equity_now,
        "maint_total": maint_total,
        "maint_note": maint_note,
        "signed_notional": signed_notional,
        "total_margin": total_margin,
        "unrealized": unreal,
        "free_cash": fc,
        "priced": priced,
        "positions": len(active),
        "net_long": signed_notional > 0,
    }


# ---------------------------------------------------------------------------
# 실제 잔고 기반 순자산 (잔고 대시보드와 동일 식 — single source of truth)
# ---------------------------------------------------------------------------

def compute_balance_nav(
    spot_quotes: dict | None,
    futures_prices: dict | None,
    *,
    holdings: list[dict] | None = None,
    futures_positions: list[dict] | None = None,
    account: dict | None = None,
) -> dict:
    """실제 잔고 기준 순자산(전부 청산 시 예수금). 잔고 대시보드 assets_both 와 동일 식.

    nav = 현물평가 + 현물예수금 + 선물가용예수금 + 위탁증거금 + 선물미실현 − 신용대출.
    (선물 청산 시 증거금을 되돌려받으므로 margin 을 자산으로 더한다.)
    가격을 못 구한 종목/선물은 원가·진입가로 폴백.

    spot_quotes: {ticker: {"price": ...}} (fetch_current_quotes 포맷)
    futures_prices: {"<sym>|<YYYYMM>": {"price": ...}} (fetch_futures_quotes 포맷)
    """
    from storage.json_store import (
        load_account, load_futures_positions, load_holdings,
    )
    if holdings is None:
        holdings = load_holdings()
    if futures_positions is None:
        futures_positions = load_futures_positions()
    if account is None:
        account = load_account()
    spot_quotes = spot_quotes or {}
    futures_prices = futures_prices or {}

    active = [h for h in holdings if h.get("quantity", 0) > 0]
    spot_eval = 0.0
    for h in active:
        q = spot_quotes.get(h.get("ticker", "")) or {}
        p = q.get("price") if isinstance(q, dict) else q
        spot_eval += (float(p) * h["quantity"]) if p else float(h.get("total_invested", 0))
    credit = sum(float(h.get("credit_loan", 0) or 0) for h in active)

    margin = 0.0
    fut_unreal = 0.0
    for fp in futures_positions:
        if fp.get("contracts", 0) <= 0:
            continue
        sym = fp.get("symbol", "")
        cm = fp.get("contract_month", "")
        e = futures_prices.get(f"{sym}|{cm}") or futures_prices.get(sym)
        cur = e.get("price") if isinstance(e, dict) else e
        margin += float(fp.get("initial_margin", 0) or 0)
        if cur is not None:
            s = 1 if fp.get("direction") == "long" else -1
            fut_unreal += (
                (float(cur) - float(fp.get("avg_entry_price", 0)))
                * int(fp.get("contracts", 0)) * int(fp.get("multiplier", 10)) * s
            )

    cash = float(account.get("cash") or 0)
    fcash = float(account.get("futures_cash") or 0)
    nav = spot_eval + cash + fcash + margin + fut_unreal - credit
    return {
        "nav": nav, "spot_eval": spot_eval, "cash": cash, "futures_cash": fcash,
        "margin": margin, "fut_unreal": fut_unreal, "credit": credit,
    }


# ---------------------------------------------------------------------------
# 상태 집계
# ---------------------------------------------------------------------------

def _plus_years(d: date, years: float) -> date:
    return d + timedelta(days=round(_DAYS_PER_YEAR * years))


def compute_goal_status(
    rows: list[dict] | None = None,
    futures_prices: dict | None = None,
) -> dict | None:
    """10억 트래커 상태 dict. rows 미지정 시 compute_profit_trend() 사용."""
    if rows is None:
        rows = compute_profit_trend()
    if not rows:
        return None

    account = load_account()
    initial = float(account.get("initial_capital") or 0)
    # goal = 현재 활성 목표(예: 1차 5억), final_goal = 최종 목표(10억).
    # 진척률·필요수익률·궤적·그래프는 모두 활성 goal 기준으로 계산한다.
    goal = float(account.get("goal_target") or GOAL_KRW)
    final_goal = float(account.get("goal_final") or GOAL_KRW)

    series = [r["asset"] for r in rows]
    dates = [r["date"] for r in rows]
    # '초기자본 대비' 는 기록된 초기자본 기준 (잔고 대시보드의 'vs 초기자본'과 일치).
    # 시계열 보정(residual)으로 series[0] 가 초기자본보다 커지므로 series[0] 대신 initial 사용.
    start_nav = initial if initial > 0 else series[0]
    start_date = dates[0]
    current = series[-1]
    today = dates[-1]
    elapsed_days = max(0, (today - start_date).days)

    progress_pct = current / goal * 100 if goal > 0 else 0.0
    remaining = goal - current
    period_return = (current / start_nav - 1.0) if start_nav > 0 else 0.0
    rcagr = realized_cagr(start_nav, current, elapsed_days)

    horizons = []
    for y in HORIZONS_YEARS:
        req = required_cagr(current, goal, y)
        horizons.append({
            "years": y,
            "target_date": _plus_years(today, y),
            "required_cagr": req,
            "required_monthly": annual_to_monthly(req),
        })

    proj_years = years_to_goal(current, goal, rcagr)
    proj_date = _plus_years(today, proj_years) if proj_years is not None else None

    peak, dd = drawdown_from_peak(series)
    ruin_line = peak * (1.0 - RUIN_DRAWDOWN)
    # 현재 NAV 에서 파산방지선까지 추가로 더 빠져야 하는 낙폭(0~-1, 음수)
    drop_to_ruin = (ruin_line / current - 1.0) if current > 0 else 0.0

    margin = compute_margin_call(
        load_futures_positions(),
        futures_prices,
        account.get("futures_cash"),
        account.get("futures_maintenance_ratio"),
    )

    return {
        "goal": goal,
        "final_goal": final_goal,
        "current": current,
        "start_nav": start_nav,
        "initial": initial,
        "start_date": start_date,
        "today": today,
        "elapsed_days": elapsed_days,
        "progress_pct": progress_pct,
        "remaining": remaining,
        "period_return": period_return,
        "realized_cagr": rcagr,
        "short_sample": elapsed_days < _SHORT_SAMPLE_DAYS,
        "horizons": horizons,
        "proj_years": proj_years,
        "proj_date": proj_date,
        "peak": peak,
        "drawdown": dd,
        "ruin_line": ruin_line,
        "drop_to_ruin": drop_to_ruin,
        "margin_call": margin,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 그래프 렌더
# ---------------------------------------------------------------------------

def _fmt_pct(x: float | None) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"{x * 100:+.1f}%"


def render_goal_graph(status: dict) -> io.BytesIO | None:
    """자산 추이 + 10억 목표선 + 2·5년 궤적 + 파산방지선 PNG."""
    _setup_korean_font()
    rows = status.get("rows")
    if not rows:
        return None

    goal = status["goal"]
    final_goal = status.get("final_goal", goal)
    current = status["current"]
    today = status["today"]
    dates = [r["date"] for r in rows]
    asset = [r["asset"] for r in rows]

    fig, ax = plt.subplots(figsize=(10, 5.8), dpi=120)

    # 실제 자산 추이
    ax.plot(dates, asset, color="#3b82f6", linewidth=2.4, zorder=6, label="순자산 (실제)")
    ax.fill_between(dates, 0, asset, color="#3b82f6", alpha=0.06, zorder=2)

    # 목표 10억 가로선
    far = _plus_years(today, max(HORIZONS_YEARS))
    ax.axhline(goal, color="#fbbf24", linewidth=1.3, linestyle="--", alpha=0.9, zorder=3)
    ax.annotate(
        f"목표 {_format_krw_short(goal, None)}", xy=(dates[0], goal),
        xytext=(2, 5), textcoords="offset points",
        color="#fbbf24", fontsize=9, va="bottom", ha="left",
    )

    # 2·5년 궤적 (오늘 현재 NAV → 목표)
    traj_styles = {2: ("#ef4444", "-."), 5: ("#22c55e", "-.")}
    for y in HORIZONS_YEARS:
        color, ls = traj_styles.get(y, ("#888", ":"))
        n = max(2, int(y * 12))
        xs = [_plus_years(today, y * i / n) for i in range(n + 1)]
        ys = [trajectory_value(current, goal, y, y * i / n) for i in range(n + 1)]
        ax.plot(xs, ys, color=color, linewidth=1.6, linestyle=ls, alpha=0.85,
                zorder=4, label=f"{y}년 궤적")

    # 파산방지선 (고점 -25%)
    ruin = status["ruin_line"]
    ax.axhline(ruin, color="#f87171", linewidth=1.1, linestyle=":", alpha=0.7, zorder=3)
    ax.annotate(
        f"파산방지선 {_format_krw_short(ruin, None)} (고점−25%)",
        xy=(dates[0], ruin), xytext=(2, -12), textcoords="offset points",
        color="#f87171", fontsize=8, va="top", ha="left",
    )

    # 현재 점 + 라벨
    ax.scatter([today], [current], color="#60a5fa", s=48, zorder=7)
    ax.annotate(
        f"{_format_krw_short(current, None)} ({status['progress_pct']:.0f}%)",
        xy=(today, current), xytext=(8, 0), textcoords="offset points",
        color="#60a5fa", fontsize=9, va="center", ha="left",
    )

    ax.set_xlim(dates[0], far + timedelta(days=20))
    ax.set_ylim(0, goal * 1.12)
    ax.yaxis.set_major_formatter(FuncFormatter(_format_krw_short))
    ax.set_facecolor("#0f0f14")
    fig.patch.set_facecolor("#0f0f14")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color("#444")
    ax.tick_params(colors="#9ca3af", labelsize=10)
    ax.set_ylabel("순자산 (KRW)", color="#9ca3af", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=9))
    ax.legend(loc="upper left", facecolor="#16161e", edgecolor="#333",
              labelcolor="#ddd", fontsize=9)
    # 활성 목표(goal)와 최종 목표(final_goal)가 다르면 "1차 목표 N억 (최종 M억)" 표기.
    if final_goal > goal:
        goal_title = (f"1차 목표 {_format_krw_short(goal, None)} "
                      f"(최종 {_format_krw_short(final_goal, None)})")
    else:
        goal_title = f"목표 {_format_krw_short(goal, None)}"
    ax.set_title(
        f"순자산 {_format_krw_short(final_goal, None)} 프로젝트 — "
        f"현재 {_format_krw_short(current, None)} / {goal_title} "
        f"({status['progress_pct']:.0f}%)",
        color="#fff", fontsize=12, loc="left", pad=12,
    )
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf
