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

# 한국식 캔들: 상승=빨강 / 하락=파랑 (마커가 튀도록 살짝 투명하게 그린다)
UP_C = "#ef4444"
DOWN_C = "#3b82f6"
CANDLE_ALPHA = 0.55
# 거래 마커: 매수=빨강▲ / 매도=파랑▼ (한국식 매수/매도 직관). 봉을 가리지 않게
# 봉에서 충분히 떨어진 위치(매수 아래·매도 위)에 두고, 연결선은 쓰지 않는다
# (윗꼬리처럼 보임). 체결가는 봉 위 노란 가로줄로 따로 표시한다.
BUY_C = "#ff3b30"
SELL_C = "#0a84ff"
AVG_C = "#cbd5e1"   # 평단선 — 중립 회색(체결가 노란줄과 구분)
EXEC_C = "#ffd60a"  # 체결가 — 봉 위 노란 가로줄
CUR_C = "#ffffff"

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


def _vol_fmt(v: float, _pos=None) -> str:
    """거래량 축 라벨 (만/억 주 단위 축약)."""
    if v >= 1e8:
        return f"{v / 1e8:.1f}억"
    if v >= 1e4:
        return f"{v / 1e4:.0f}만"
    return f"{int(v)}"


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
    # 첫 매매 이전 맥락을 ~10거래일(약 14캘린더일) 보여준다.
    start_dt = datetime.strptime(first_date, "%Y-%m-%d") - timedelta(days=14)
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
    vols = hist["Volume"].fillna(0).to_numpy() if "Volume" in hist.columns else None

    # 위: 가격(캔들+마커), 아래: 거래량 (x축 공유)
    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(9.2, 5.4), dpi=120, sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1.0], "hspace": 0.06},
    )

    # ── 가격 캔들 (한국식 빨강/파랑, 마커가 튀도록 살짝 투명) ──
    body_w = 0.6
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        color = UP_C if c >= o else DOWN_C
        ax.vlines(i, l, h, color=color, linewidth=1.0, alpha=CANDLE_ALPHA, zorder=2)
        lo, hi = min(o, c), max(o, c)
        if hi - lo <= 0:
            ax.hlines(o, i - body_w / 2, i + body_w / 2, color=color,
                      linewidth=1.2, alpha=CANDLE_ALPHA, zorder=3)
        else:
            ax.add_patch(Rectangle(
                (i - body_w / 2, lo), body_w, hi - lo,
                facecolor=color, edgecolor=color, linewidth=0.6,
                alpha=CANDLE_ALPHA, zorder=3,
            ))

    p_hi = float(highs.max())
    p_lo = float(lows.min())
    span = (p_hi - p_lo) or 1.0

    # 평단선
    if avg_price and avg_price > 0:
        ax.axhline(avg_price, color=AVG_C, linewidth=1.2, linestyle="--", alpha=0.9, zorder=4)
        ax.text(0, avg_price, f" 평단 {avg_price:,.0f}", color=AVG_C,
                fontsize=8, va="bottom", fontweight="bold")

    # ── 체결가: 봉 위 노란 가로줄 / 매수(빨강▲)·매도(파랑▼) 마커는 봉에서 충분히 떨어뜨려 ──
    # (연결선은 윗꼬리처럼 보여 제거. 마커는 날짜·방향·수량만 전달, 가격은 노란줄이 전달.)
    gap = span * 0.12
    lo_extent, hi_extent = p_lo, p_hi
    for t in trades:
        x = to_x(t["date"])
        price = t["price"]
        is_buy = t["type"] == "buy"
        bx = int(round(x))
        # 체결가 노란 가로줄 (봉 폭에 맞춰, 얇은 점선)
        ax.hlines(price, x - 0.45, x + 0.45, color=EXEC_C,
                  linewidth=1.0, linestyle=(0, (2, 1.5)), zorder=6)
        color = BUY_C if is_buy else SELL_C
        bar_lo = lows[bx] if 0 <= bx < n else price
        bar_hi = highs[bx] if 0 <= bx < n else price
        if is_buy:
            anchor = min(price, bar_lo) - gap
            marker, va, dy, label = "^", "top", -11, f"매수 {t['qty']}"
            lo_extent = min(lo_extent, anchor)
        else:
            anchor = max(price, bar_hi) + gap
            marker, va, dy, label = "v", "bottom", 11, f"매도 {t['qty']}"
            hi_extent = max(hi_extent, anchor)
        ax.scatter(x, anchor, marker=marker, s=190, facecolor=color,
                   edgecolor="white", linewidth=1.3, zorder=7)
        ax.annotate(label, (x, anchor), color="white", fontsize=7, ha="center", va=va,
                    xytext=(0, dy), textcoords="offset points", zorder=8,
                    bbox=dict(boxstyle="round,pad=0.2", fc=color, ec="none", alpha=0.92))

    # 현재가(또는 시세 실패 시 최근 종가) 마커 — 라벨을 캡션과 일치시킨다.
    last_x = n - 1
    has_live = bool(cur_price and cur_price > 0)
    cur = cur_price if has_live else float(closes[-1])
    cur_label = "현재" if has_live else "최근종가"
    ax.scatter(last_x, cur, color=CUR_C, s=44, zorder=9, edgecolor="#0f0f14", linewidth=0.8)
    ax.text(last_x, cur, f"  {cur_label} {cur:,.0f}", color=CUR_C, fontsize=8, va="center")

    # ── 거래량 패널 (상승일=빨강 / 하락일=파랑) ──
    if vols is not None:
        vcolors = [UP_C if closes[i] >= opens[i] else DOWN_C for i in range(n)]
        axv.bar(range(n), vols, width=body_w, color=vcolors, alpha=0.55, zorder=2)
        axv.set_ylabel("거래량", color="#888", fontsize=8)
        axv.set_ylim(0, (float(vols.max()) or 1.0) * 1.15)
        axv.yaxis.set_major_formatter(plt.FuncFormatter(_vol_fmt))
    else:
        axv.set_visible(False)

    # 가격축 범위 (마커 봉 밖 위치까지 포함, NaN/inf 가드)
    ax.set_xlim(-0.8, n + 1.5)
    y_min = min(lo_extent, avg_price or lo_extent, cur)
    y_max = max(hi_extent, avg_price or hi_extent, cur)
    if not (math.isfinite(y_min) and math.isfinite(y_max)):
        plt.close(fig)
        return None
    pad = (y_max - y_min) * 0.06 or 1.0
    ax.set_ylim(y_min - pad, y_max + pad)

    # x 눈금 = 날짜 라벨 (주말 갭 없이 균등 6개) — 공유축이라 하단 패널에만 표기
    tick_count = min(6, n)
    if n > 1:
        tick_idx = [int(round(i * (n - 1) / (tick_count - 1))) for i in range(tick_count)]
    else:
        tick_idx = [0]
    bottom_ax = axv if vols is not None else ax
    bottom_ax.set_xticks(tick_idx)
    bottom_ax.set_xticklabels([idx_dates[i][5:] for i in tick_idx])
    if vols is None:
        ax.tick_params(labelbottom=True)

    # 다크 테마
    fig.patch.set_facecolor("#0f0f14")
    for a in (ax, axv):
        a.set_facecolor("#0f0f14")
        for spine in ("top", "right"):
            a.spines[spine].set_visible(False)
        for spine in ("bottom", "left"):
            a.spines[spine].set_color("#444")
        a.tick_params(colors="#888", labelsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    ax.set_title(
        f"{name}  ({ticker})  ·  ▲매수 ▼매도 복기  ·  {idx_dates[0]}~",
        color="#fff", fontsize=11, loc="left", pad=10,
    )
    fig.subplots_adjust(left=0.085, right=0.97, top=0.91, bottom=0.08)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    buf.name = f"review_{ticker}.png"
    return buf


# 급등봉 기준: 전일 종가 대비 일간 상승률(%)
SURGE_PCT = 10.0


def _build_review_figure(
    name: str,
    ticker: str,
    transactions: list[dict],
    avg_price: float,
    cur_price: float | None = None,
):
    """plotly 인터랙티브 figure 생성 (마우스 오버 시 상승률·OHLC·거래량).

    실패 시 None. plotly 미설치면 ImportError 를 그대로 올린다(호출부에서 PNG 폴백).
    """
    if not ticker:
        return None
    trades = aggregate_trades(transactions)
    if not trades:
        return None

    first_date = min(t["date"] for t in trades)
    start_dt = datetime.strptime(first_date, "%Y-%m-%d") - timedelta(days=14)
    try:
        hist = yf.Ticker(ticker).history(
            start=start_dt.strftime("%Y-%m-%d"), interval="1d"
        )
    except Exception as e:
        logger.warning("%s(%s) 일봉 조회 실패: %s", name, ticker, e)
        return None
    if hist is None or hist.empty:
        return None
    hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
    if hist.empty:
        return None

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    dates = [d.strftime("%Y-%m-%d") for d in hist.index]
    O = [float(v) for v in hist["Open"]]
    Hi = [float(v) for v in hist["High"]]
    Lo = [float(v) for v in hist["Low"]]
    Cl = [float(v) for v in hist["Close"]]
    V = ([float(v) for v in hist["Volume"].fillna(0)]
         if "Volume" in hist.columns else [0.0] * len(dates))
    n = len(dates)
    pos = {d: i for i, d in enumerate(dates)}

    def snap(ds: str) -> str:
        if ds in pos:
            return ds
        if ds > dates[-1]:
            return dates[-1]
        earlier = [d for d in dates if d <= ds]
        return earlier[-1] if earlier else dates[0]

    # 상승률 = 전일 종가 대비(%) (첫 봉은 시가 대비). 봉 몸통은 시가→종가라 둘은 다르다
    # (갭상승 시 전일대비는 크지만 몸통은 짧을 수 있음) → hover 에 둘 다 보여 혼동 방지.
    chg = []
    for i in range(n):
        base = O[i] if i == 0 else Cl[i - 1]
        chg.append(((Cl[i] - base) / base * 100) if base else 0.0)
    intraday = [((Cl[i] - O[i]) / O[i] * 100) if O[i] else 0.0 for i in range(n)]

    span = (max(Hi) - min(Lo)) or 1.0
    gap = span * 0.06

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.04, row_heights=[0.8, 0.2])

    # 체결가 노란 가로 틱 — 캔들보다 *먼저* 그려 봉 뒤로 보낸다(봉을 안 가림,
    # 노란줄이 봉보다 가로로 길어 양옆으로만 삐져나옴).
    fig.add_trace(go.Scatter(
        x=[snap(t["date"]) for t in trades], y=[t["price"] for t in trades],
        mode="markers",
        marker=dict(symbol="line-ew", size=28,
                    line=dict(color=EXEC_C, width=1.5), color=EXEC_C),
        hoverinfo="skip", name="체결가", showlegend=False), row=1, col=1)

    # 캔들 (한국식 빨강/파랑) — hover 는 아래 투명 scatter 가 담당
    fig.add_trace(go.Candlestick(
        x=dates, open=O, high=Hi, low=Lo, close=Cl,
        increasing=dict(line=dict(color=UP_C, width=1), fillcolor=UP_C),
        decreasing=dict(line=dict(color=DOWN_C, width=1), fillcolor=DOWN_C),
        whiskerwidth=0, hoverinfo="skip", showlegend=False, name=""), row=1, col=1)

    # 상승률·OHLC·거래량 통합 hover (x unified)
    customdata = list(zip(O, Hi, Lo, Cl, chg, V, intraday))
    fig.add_trace(go.Scatter(
        x=dates, y=Cl, mode="markers",
        marker=dict(size=4, color="rgba(0,0,0,0)"),
        customdata=customdata, name="",
        hovertemplate=(
            "시 %{customdata[0]:,.0f} · 고 %{customdata[1]:,.0f} · "
            "저 %{customdata[2]:,.0f} · 종 %{customdata[3]:,.0f}<br>"
            "<b>상승률(전일대비) %{customdata[4]:+.2f}%</b> · "
            "시가대비(몸통) %{customdata[6]:+.2f}%<br>"
            "거래량 %{customdata[5]:,.0f}"
            "<extra></extra>"),
        showlegend=False), row=1, col=1)

    # 상승률 ≥10% 급등봉 — 별 + 라벨
    surge = [(dates[i], Hi[i], chg[i]) for i in range(n) if chg[i] >= SURGE_PCT]
    if surge:
        fig.add_trace(go.Scatter(
            x=[s[0] for s in surge], y=[s[1] + gap * 0.7 for s in surge],
            mode="markers+text",
            marker=dict(symbol="star", size=13, color="#fbbf24",
                        line=dict(color="white", width=0.6)),
            text=[f"+{s[2]:.0f}%" for s in surge], textposition="top center",
            textfont=dict(color="#fbbf24", size=9),
            customdata=[s[2] for s in surge],
            hovertemplate="급등봉 +%{customdata:.2f}%<extra></extra>",
            name=f"급등(≥{SURGE_PCT:.0f}%)", showlegend=True), row=1, col=1)

    surge_dates = {s[0] for s in surge}

    # 매수▲(봉 아래) / 매도▼(봉 위). 급등봉(별) 위에 찍히는 매도는 별과 겹치지 않게 위로 확 올림.
    def _add_side(side: str, color: str, sym: str, label: str, below: bool) -> None:
        items = [t for t in trades if t["type"] == side]
        if not items:
            return
        xs = [snap(t["date"]) for t in items]
        ys = []
        for x in xs:
            if below:
                ys.append(Lo[pos[x]] - gap)
            else:
                # 급등봉(별+라벨)이 위에 있는 칸이면 그 위로 올려 안 겹치게.
                y = Hi[pos[x]] + (span * 0.13 if x in surge_dates else gap)
                ys.append(y)
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="markers+text",
            marker=dict(symbol=sym, size=14, color=color,
                        line=dict(color="white", width=1.2)),
            text=[f"{label} {t['qty']}" for t in items],
            textposition="bottom center" if below else "top center",
            textfont=dict(color="white", size=9),
            customdata=[[t["qty"], t["price"]] for t in items],
            hovertemplate=(label + " %{customdata[0]}주<br>단가 %{customdata[1]:,.0f}"
                           "<extra></extra>"),
            name=label, showlegend=True), row=1, col=1)

    _add_side("buy", BUY_C, "triangle-up", "매수", below=True)
    _add_side("sell", SELL_C, "triangle-down", "매도", below=False)

    # 평단선
    if avg_price and avg_price > 0:
        fig.add_hline(y=avg_price, line=dict(color=AVG_C, dash="dash", width=1),
                      annotation_text=f"평단 {avg_price:,.0f}",
                      annotation_position="top left",
                      annotation_font=dict(color=AVG_C, size=10), row=1, col=1)

    # 현재가(또는 최근종가) 마커
    has_live = bool(cur_price and cur_price > 0)
    cur = float(cur_price) if has_live else Cl[-1]
    fig.add_trace(go.Scatter(
        x=[dates[-1]], y=[cur], mode="markers+text",
        marker=dict(color=CUR_C, size=9),
        text=[f"  {'현재' if has_live else '최근종가'} {cur:,.0f}"],
        textposition="middle right", textfont=dict(color=CUR_C, size=10),
        hoverinfo="skip", showlegend=False), row=1, col=1)

    # 거래량 (상승일=빨강 / 하락일=파랑)
    vcolors = [UP_C if Cl[i] >= O[i] else DOWN_C for i in range(n)]
    fig.add_trace(go.Bar(x=dates, y=V, marker=dict(color=vcolors), opacity=0.55,
                         hoverinfo="skip", showlegend=False, name="거래량"),
                  row=2, col=1)

    fig.update_layout(
        title=dict(text=f"{name} ({ticker}) · 매매 복기 · {dates[0]}~",
                   font=dict(color="#fff", size=15), x=0.01),
        template="plotly_dark", paper_bgcolor="#0f0f14", plot_bgcolor="#0f0f14",
        hovermode="x unified", dragmode="pan",
        margin=dict(l=64, r=92, t=70, b=28),
        legend=dict(orientation="h", y=1.04, x=0.5, xanchor="center",
                    bgcolor="rgba(0,0,0,0)", font=dict(color="#ccc", size=10)),
        xaxis_rangeslider_visible=False, height=860,
    )
    # x를 category 로 고정: (1) 날짜문자열이 date축으로 자동인식돼 숫자 range 가
    # 1970년으로 튀던 버그 방지, (2) 주말/휴장 갭 제거. range 는 category 인덱스(0..n-1).
    # categoryarray 로 순서를 날짜순으로 고정 — 트레이스 추가 순서(체결틱이 첫 트레이스)
    # 때문에 category 순서가 뒤섞이는 것을 방지.
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=dates,
                     showgrid=False, color="#888", nticks=10, range=[-0.7, n + 4.5])
    fig.update_yaxes(showgrid=True, gridcolor="#1f2430", color="#888", row=1, col=1)
    fig.update_yaxes(showgrid=False, color="#888", title_text="거래량", row=2, col=1)
    return fig


def build_trade_review_html(
    name: str,
    ticker: str,
    transactions: list[dict],
    avg_price: float,
    cur_price: float | None = None,
) -> io.BytesIO | None:
    """매매 복기 인터랙티브 HTML (마우스 오버 시 상승률·OHLC·거래량, ≥10% 급등봉 별표시).

    plotly.js 는 CDN 으로 로드(파일 경량). 실패 시 None.
    """
    fig = _build_review_figure(name, ticker, transactions, avg_price, cur_price)
    if fig is None:
        return None
    import plotly.io as pio
    html = pio.to_html(fig, include_plotlyjs="cdn", full_html=True,
                       config={"displayModeBar": True, "scrollZoom": True,
                               "displaylogo": False})
    buf = io.BytesIO(html.encode("utf-8"))
    buf.name = f"review_{ticker}.html"
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
