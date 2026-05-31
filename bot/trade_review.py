"""매매 복기(復棋) — 보유 종목별 일봉 차트 위에 내 매수/매도 기록을 찍어
"이 구간에서 왜 비중을 더/덜 안 실었지?"를 눈으로 되짚게 해주는 모듈.

핵심 사실(검증됨): 이 봇이 쓰는 가격은 yfinance 와 동일한 *스케일된 가격우주*라
기록된 거래 단가와 yfinance 일봉이 같은 축 위에 정확히 정렬된다. 따라서 일봉
캔들에 거래 마커를 그대로 얹으면 된다 (÷10 같은 보정 금지).

같은 날·같은 방향의 분할체결은 한 마커로 합산(VWAP·총수량)한다.
"""
from __future__ import annotations

import io
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import yfinance as yf  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

logger = logging.getLogger(__name__)

# 한국식 캔들: 상승=빨강 / 하락=파랑
UP_C = "#ef4444"
DOWN_C = "#3b82f6"
# 거래 마커: 캔들색과 헷갈리지 않게 매수=초록▲ / 매도=주황▼
BUY_C = "#22c55e"
SELL_C = "#f59e0b"
AVG_C = "#a78bfa"
CUR_C = "#fbbf24"

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
    logger.warning("한글 폰트 미발견 — 차트 한글이 깨질 수 있음")


def aggregate_trades(transactions: list[dict]) -> list[dict]:
    """같은 (날짜, 매수/매도)끼리 합산해 VWAP·총수량 마커로 변환.

    반환: [{"date": "YYYY-MM-DD", "type": "buy"|"sell", "qty": int, "price": float}]
    날짜 오름차순, 같은 날은 매수 먼저.
    """
    bucket: dict[tuple[str, str], dict] = defaultdict(lambda: {"amt": 0.0, "qty": 0})
    for t in transactions:
        ty = t.get("type")
        if ty not in ("buy", "sell"):
            continue
        d = (t.get("date") or "")[:10]
        if not d:
            continue
        qty = int(t.get("quantity", 0) or 0)
        price = float(t.get("price", 0) or 0)
        if qty <= 0 or price <= 0:
            continue
        b = bucket[(d, ty)]
        b["amt"] += price * qty
        b["qty"] += qty
    out: list[dict] = []
    for (d, ty), v in bucket.items():
        if v["qty"] <= 0:
            continue
        out.append({"date": d, "type": ty, "qty": v["qty"], "price": v["amt"] / v["qty"]})
    out.sort(key=lambda x: (x["date"], 0 if x["type"] == "buy" else 1))
    return out


def _marker_size(qty: int, max_qty: int) -> float:
    """수량 비례 마커 크기 (제곱근 스케일, 60~320 clamp)."""
    if max_qty <= 0:
        return 120.0
    frac = (qty / max_qty) ** 0.5
    return 70.0 + frac * 250.0


def build_trade_chart(
    name: str,
    ticker: str,
    transactions: list[dict],
    avg_price: float,
    cur_price: float | None = None,
) -> io.BytesIO | None:
    """보유 종목의 전체 매매 여정을 일봉 캔들 + 매수▲/매도▼ 마커로 렌더.

    실패(티커 없음·시세 없음) 시 None.
    """
    if not ticker:
        return None
    trades = aggregate_trades(transactions)
    if not trades:
        return None

    _setup_korean_font()

    first_date = min(t["date"] for t in trades)
    start_dt = datetime.strptime(first_date, "%Y-%m-%d") - timedelta(days=7)
    try:
        hist = yf.Ticker(ticker).history(
            start=start_dt.strftime("%Y-%m-%d"), interval="1d"
        )
    except Exception as e:
        logger.warning("%s(%s) 일봉 조회 실패: %s", name, ticker, e)
        return None
    if hist is None or hist.empty:
        return None
    # yfinance 는 신규상장·거래정지·당일 미완성봉에서 NaN 행을 흔히 내려준다.
    # NaN 이 highs.max()/lows.min()→set_ylim 까지 전파되면 ValueError 로 죽으므로 선제거.
    hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
    if hist.empty:
        return None

    idx_dates = [d.strftime("%Y-%m-%d") for d in hist.index]
    pos = {d: i for i, d in enumerate(idx_dates)}
    n = len(idx_dates)

    def to_x(dstr: str) -> float:
        """거래일 문자열을 캔들 x좌표로.

        - 정확히 일치: 그 인덱스
        - 마지막 캔들보다 늦음(당일 봉 미게시 등): 마지막 캔들 우측(n) — 현재가 마커와 안 겹치게
        - 비거래일: 직전 거래일, 그마저 없으면(범위 이전) 0
        """
        if dstr in pos:
            return pos[dstr]
        if dstr > idx_dates[-1]:
            return float(n)
        earlier = [i for i, d in enumerate(idx_dates) if d <= dstr]
        return earlier[-1] if earlier else 0

    opens = hist["Open"].to_numpy()
    highs = hist["High"].to_numpy()
    lows = hist["Low"].to_numpy()
    closes = hist["Close"].to_numpy()

    fig, ax = plt.subplots(figsize=(9.2, 4.6), dpi=120)
    body_w = 0.6
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        color = UP_C if c >= o else DOWN_C
        ax.vlines(i, l, h, color=color, linewidth=1.0, zorder=2)
        lo, hi = min(o, c), max(o, c)
        if hi - lo <= 0:
            ax.hlines(o, i - body_w / 2, i + body_w / 2, color=color, linewidth=1.2, zorder=3)
        else:
            ax.add_patch(Rectangle(
                (i - body_w / 2, lo), body_w, hi - lo,
                facecolor=color, edgecolor=color, linewidth=0.6, zorder=3,
            ))

    # 기간 고점/저점 참고선 (비중 축소/추가 기회 가늠용)
    p_hi = float(highs.max())
    p_lo = float(lows.min())
    ax.axhline(p_hi, color="#64748b", linewidth=0.8, linestyle=":", alpha=0.6, zorder=1)
    ax.axhline(p_lo, color="#64748b", linewidth=0.8, linestyle=":", alpha=0.6, zorder=1)
    ax.text(n - 0.4, p_hi, f" 기간고점 {p_hi:,.0f}", color="#94a3b8",
            fontsize=7, va="bottom", ha="right")
    ax.text(n - 0.4, p_lo, f" 기간저점 {p_lo:,.0f}", color="#94a3b8",
            fontsize=7, va="top", ha="right")

    # 평단선
    if avg_price and avg_price > 0:
        ax.axhline(avg_price, color=AVG_C, linewidth=1.2, linestyle="--", alpha=0.9, zorder=4)
        ax.text(0, avg_price, f" 평단 {avg_price:,.0f}", color=AVG_C,
                fontsize=8, va="bottom", fontweight="bold")

    # 매수▲ / 매도▼ 마커
    max_qty = max((t["qty"] for t in trades), default=1)
    span = float(highs.max() - lows.min()) or 1.0
    for t in trades:
        x = to_x(t["date"])
        price = t["price"]
        is_buy = t["type"] == "buy"
        ax.scatter(
            x, price,
            marker="^" if is_buy else "v",
            s=_marker_size(t["qty"], max_qty),
            facecolor=BUY_C if is_buy else SELL_C,
            edgecolor="#0f0f14", linewidth=0.8,
            zorder=6,
        )
        off = span * 0.025
        ax.text(
            x, price + (off if is_buy else -off),
            f"{t['qty']}",
            color=BUY_C if is_buy else SELL_C,
            fontsize=6.5, ha="center",
            va="bottom" if is_buy else "top",
            zorder=7,
        )

    # 현재가(또는 시세 실패 시 최근 종가) 마커 — 라벨을 캡션과 일치시킨다.
    last_x = n - 1
    has_live = bool(cur_price and cur_price > 0)
    cur = cur_price if has_live else float(closes[-1])
    cur_label = "현재" if has_live else "최근종가"
    ax.scatter(last_x, cur, color=CUR_C, s=46, zorder=8, edgecolor="#0f0f14", linewidth=0.8)
    ax.text(last_x, cur, f"  {cur_label} {cur:,.0f}", color=CUR_C, fontsize=8, va="center")

    # 축 범위 (NaN/inf 가 새어들면 set_ylim 이 죽으므로 유한성 가드)
    ax.set_xlim(-0.8, n + 1.5)
    y_min = min(p_lo, avg_price or p_lo, cur)
    y_max = max(p_hi, avg_price or p_hi, cur)
    if not (math.isfinite(y_min) and math.isfinite(y_max)):
        plt.close(fig)
        return None
    pad = (y_max - y_min) * 0.06 or 1.0
    ax.set_ylim(y_min - pad, y_max + pad)

    # x 눈금 = 날짜 라벨 (주말 갭 없이 균등 6개)
    tick_count = min(6, n)
    if n > 1:
        tick_idx = [int(round(i * (n - 1) / (tick_count - 1))) for i in range(tick_count)]
    else:
        tick_idx = [0]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([idx_dates[i][5:] for i in tick_idx])

    # 다크 테마
    ax.set_facecolor("#0f0f14")
    fig.patch.set_facecolor("#0f0f14")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color("#444")
    ax.tick_params(colors="#888", labelsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    ax.set_title(
        f"{name}  ({ticker})  ·  ▲매수 ▼매도 복기  ·  {idx_dates[0]}~",
        color="#fff", fontsize=11, loc="left", pad=10,
    )
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    buf.name = f"review_{ticker}.png"
    return buf


def _eok(v: float) -> str:
    """KRW 금액을 억/천만/만 단위로 축약."""
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e8:
        return f"{sign}{a / 1e8:.2f}억"
    if a >= 1e7:
        return f"{sign}{a / 1e7:.1f}천만"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.0f}만"
    return f"{sign}{a:,.0f}"


def summarize_review(
    holding: dict,
    transactions: list[dict],
    cur_price: float | None = None,
    change_pct: float | None = None,
    position: tuple[int, int] | None = None,
) -> str:
    """차트 캡션(HTML). Telegram 캡션 1024자 제한 내로 요약 + 복기 질문.

    position: (현재 인덱스 1-based, 전체 개수) — "[3/10]" 표기용.
    """
    import html

    name = html.escape(holding.get("name", ""))
    ticker = html.escape(holding.get("ticker", ""))
    sector = html.escape(holding.get("sector", ""))
    qty = int(holding.get("quantity", 0) or 0)
    avg = float(holding.get("avg_price", 0) or 0)
    invested = float(holding.get("total_invested", avg * qty) or 0)

    buys = [t for t in transactions if t.get("type") == "buy"]
    sells = [t for t in transactions if t.get("type") == "sell"]
    tot_buy_qty = sum(int(t.get("quantity", 0) or 0) for t in buys)
    tot_sell_qty = sum(int(t.get("quantity", 0) or 0) for t in sells)
    dates = [(t.get("date") or "")[:10] for t in transactions if t.get("date")]
    first = min(dates) if dates else "?"

    head = ""
    if position:
        head = f"[{position[0]}/{position[1]}] "
    lines = [f"{head}<b>{name}</b> ({ticker}) · {sector}"]
    lines.append(f"📅 {first} ~ 오늘  ·  {len(buys)}매수 / {len(sells)}매도")

    pos_line = f"보유 {qty}주 · 평단 {avg:,.0f}"
    if cur_price and cur_price > 0:
        chg = f" ({change_pct:+.1f}%)" if change_pct is not None else ""
        pos_line += f" · 현재가 {cur_price:,.0f}{chg}"
    lines.append(pos_line)

    if cur_price and cur_price > 0 and invested > 0:
        eval_amt = cur_price * qty
        pnl = eval_amt - invested
        pct = pnl / invested * 100
        s = "+" if pnl >= 0 else ""
        lines.append(
            f"평가 {_eok(eval_amt)} · 손익 <b>{s}{_eok(pnl)} ({s}{pct:.1f}%)</b>"
        )

    if not transactions and qty > 0:
        lines.append("⚠ 이 종목명으로 매칭된 거래 기록이 없어요(종목명 변경 가능성).")
    else:
        lines.append(f"🔁 누적 매수 +{tot_buy_qty}주 / 매도 −{tot_sell_qty}주")
    lines.append(
        "💭 ▲매수·▼매도 위치를 보며: 더 실었어야 할 자리는? 줄였어야 할 자리는?"
    )
    return "\n".join(lines)
