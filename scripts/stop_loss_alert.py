#!/usr/bin/env python3
"""평일 15:30 KST 자동손절(-10%) 임박 + 추세 이평선 깨짐 푸시.

두 가지 알람을 한 번에 발송:
1. 자동손절(-10%) 임박: 보유 종목 중 현재 손익률이 -10% ~ -5% 구간
2. 추세 이평선 깨짐: `data/timeframe_labels.json` 에 라벨된 종목 중 종가가
   해당 시간프레임 트리거 이평선(short=5일선 / long=50일선) 아래로 마감

chat_id 778372474 로 푸시. 25봉 일봉 차트 1장 동봉.
대상 0건인 카테고리는 무발송.

launchd LaunchAgent (`com.seung.invest_log.stop_loss_alert.plist`) 에서 호출.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
from pathlib import Path

# 프로젝트 루트 sys.path 등록
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yfinance as yf
from matplotlib import font_manager
from matplotlib.patches import Rectangle
from telegram import Bot
from telegram.constants import ParseMode

from bot.formatters import _resolve_tickers, fetch_current_quotes
from storage.json_store import load_holdings

logger = logging.getLogger(__name__)

CHAT_ID = 778372474
STOP_LOSS_PCT = -10.0
WARN_THRESHOLD_PCT = -5.0  # 손익률이 이 값 이하 + STOP_LOSS_PCT 이상 → 위태
BARS = 25
TIMEFRAME_LABELS_PATH = ROOT / "data" / "timeframe_labels.json"

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


def _load_env_token() -> str | None:
    """프로젝트 루트 .env 에서 BOT_TOKEN 한 줄 파싱."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == "BOT_TOKEN":
            return v.strip().strip('"').strip("'")
    return None


def _find_at_risk_positions() -> list[dict]:
    """현재 손익률 -10% ~ -5% 구간 보유 종목 추리기."""
    holdings = [h for h in load_holdings() if h.get("quantity", 0) > 0]
    name_to_ticker, _ = _resolve_tickers(holdings)
    tickers = list(set(name_to_ticker.values()))
    quotes = fetch_current_quotes(tickers) if tickers else {}

    at_risk: list[dict] = []
    for h in holdings:
        name = h["name"]
        ticker = name_to_ticker.get(name, "")
        if not ticker:
            continue
        q = quotes.get(ticker)
        if not q or q.get("price") is None:
            continue
        cur = float(q["price"])
        avg = float(h["avg_price"])
        qty = int(h.get("quantity", 0))
        if avg <= 0 or qty <= 0:
            continue
        pnl_pct = (cur - avg) / avg * 100
        if STOP_LOSS_PCT <= pnl_pct <= WARN_THRESHOLD_PCT:
            eval_amt = cur * qty
            invested = float(h.get("total_invested", avg * qty))
            pnl_krw = eval_amt - invested
            at_risk.append({
                "name": name,
                "ticker": ticker,
                "cur": cur,
                "avg": avg,
                "qty": qty,
                "eval": eval_amt,
                "pnl_krw": pnl_krw,
                "pnl_pct": pnl_pct,
                "remaining": pnl_pct - STOP_LOSS_PCT,  # 양수, 작을수록 위태
            })
    # 손절선 근접 순(remaining 오름차순)
    at_risk.sort(key=lambda r: r["remaining"])
    return at_risk


def _build_chart(
    ticker: str, name: str, avg: float, cur: float, bars: int = BARS
) -> io.BytesIO | None:
    """25봉 일봉 캔들차트. 매수단가·손절(-10%)선 표시.

    한국 관례: 상승=빨강(#ef4444), 하락=파랑(#3b82f6).
    """
    try:
        hist = yf.Ticker(ticker).history(period=f"{bars * 2 + 10}d", interval="1d")
        if hist.empty:
            return None
        hist = hist.tail(bars)
        fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=120)

        opens = hist["Open"].to_numpy()
        highs = hist["High"].to_numpy()
        lows = hist["Low"].to_numpy()
        closes = hist["Close"].to_numpy()
        n = len(hist)
        body_w = 0.6

        for i in range(n):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            up = c >= o
            color = "#ef4444" if up else "#3b82f6"
            ax.vlines(i, l, h, color=color, linewidth=1.0, zorder=2)
            body_lo = min(o, c)
            body_hi = max(o, c)
            body_h = body_hi - body_lo
            if body_h <= 0:
                ax.hlines(o, i - body_w / 2, i + body_w / 2,
                          color=color, linewidth=1.2, zorder=3)
            else:
                ax.add_patch(Rectangle(
                    (i - body_w / 2, body_lo), body_w, body_h,
                    facecolor=color, edgecolor=color, linewidth=0.6, zorder=3,
                ))

        last_x = n - 1
        ax.scatter(last_x, closes[-1], color="#fbbf24", s=44, zorder=5)

        ax.axhline(y=avg, color="#22c55e", linewidth=1.2, linestyle="--", alpha=0.85)
        ax.text(0, avg, f" 매수 {avg:,.0f}", color="#22c55e",
                fontsize=8, va="bottom")

        sl = avg * (1 + STOP_LOSS_PCT / 100)
        ax.axhline(y=sl, color="#ef4444", linewidth=1.2, linestyle="--", alpha=0.85)
        ax.text(0, sl, f" 손절 {sl:,.0f} (-10%)", color="#ef4444",
                fontsize=8, va="top")

        ax.text(last_x, cur, f"  {cur:,.0f}", color="#fbbf24",
                fontsize=8, va="center")

        ax.set_xlim(-0.8, n - 0.2)
        y_min = min(float(lows.min()), sl, avg)
        y_max = max(float(highs.max()), avg, cur)
        y_pad = (y_max - y_min) * 0.05 or 1.0
        ax.set_ylim(y_min - y_pad, y_max + y_pad)

        tick_count = min(6, n)
        tick_idx = [int(round(x)) for x in
                    [i * (n - 1) / (tick_count - 1) for i in range(tick_count)]] if n > 1 else [0]
        ax.set_xticks(tick_idx)
        ax.set_xticklabels([hist.index[i].strftime("%m-%d") for i in tick_idx])

        ax.set_facecolor("#0f0f14")
        fig.patch.set_facecolor("#0f0f14")
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("bottom", "left"):
            ax.spines[spine].set_color("#444")
        ax.tick_params(colors="#888", labelsize=8)
        ax.yaxis.label.set_color("#888")
        ax.set_title(f"{name}  ({ticker})  ·  {bars}봉 일봉",
                     color="#fff", fontsize=11, loc="left", pad=10)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.warning("%s 차트 생성 실패: %s", ticker, e)
        return None


def _load_timeframe_labels() -> dict[str, dict]:
    """data/timeframe_labels.json 에서 종목별 시간프레임 라벨 로드.

    스키마: {"labels": {"<name>": {"timeframe": "short|long", "ma": int, "note": str}}}
    파일/키 누락 시 빈 dict.
    """
    if not TIMEFRAME_LABELS_PATH.exists():
        return {}
    try:
        data = json.loads(TIMEFRAME_LABELS_PATH.read_text(encoding="utf-8"))
        return data.get("labels", {}) or {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("timeframe_labels.json 파싱 실패: %s", e)
        return {}


def _find_ma_breakdown_positions(bars: int = 80) -> list[dict]:
    """라벨된 보유 종목 중 *오늘 종가가 추세 트리거 이평선 아래로 마감* 한 종목.

    timeframe_labels.json 에 명시되지 않은 종목은 스킵 — 사용자가 분류한 종목만
    알람 대상 ([[user-sector-timeframe-classification]] 분류 적용 원칙).
    """
    labels = _load_timeframe_labels()
    if not labels:
        return []

    holdings = [h for h in load_holdings() if h.get("quantity", 0) > 0]
    name_to_ticker, _ = _resolve_tickers(holdings)

    out: list[dict] = []
    for h in holdings:
        name = h["name"]
        lab = labels.get(name)
        if not lab:
            continue
        ticker = name_to_ticker.get(name, "")
        if not ticker:
            continue
        ma_n = int(lab.get("ma") or 0)
        if ma_n <= 0:
            continue
        try:
            hist = yf.Ticker(ticker).history(period=f"{max(bars, ma_n * 3)}d", interval="1d")
            if hist.empty or len(hist) < ma_n + 1:
                continue
            close = hist["Close"]
            ma_series = close.rolling(window=ma_n).mean()
            last_close = float(close.iloc[-1])
            last_ma = float(ma_series.iloc[-1])
            if last_close < last_ma:
                avg = float(h["avg_price"])
                qty = int(h.get("quantity", 0))
                eval_amt = last_close * qty
                invested = float(h.get("total_invested", avg * qty))
                pnl_krw = eval_amt - invested
                pnl_pct = (last_close - avg) / avg * 100 if avg > 0 else 0.0
                deviation = (last_close - last_ma) / last_ma * 100
                out.append({
                    "name": name,
                    "ticker": ticker,
                    "cur": last_close,
                    "ma": last_ma,
                    "ma_n": ma_n,
                    "timeframe": lab.get("timeframe", "?"),
                    "note": lab.get("note", ""),
                    "avg": avg,
                    "qty": qty,
                    "eval": eval_amt,
                    "pnl_krw": pnl_krw,
                    "pnl_pct": pnl_pct,
                    "deviation": deviation,  # 음수, 작을수록 깊이 깨짐
                })
        except Exception as e:
            logger.warning("%s MA 체크 실패: %s", name, e)
            continue
    out.sort(key=lambda r: r["deviation"])  # 가장 깊이 깨진 순
    return out


async def _send_alerts(bot: Bot, alerts: list[dict]) -> None:
    n = len(alerts)
    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"⚠️ 자동손절(-10%) 임박 종목 {n}건  ·  15:30 점검",
    )
    for a in alerts:
        text = (
            f"<b>{a['name']}</b>\n"
            f"현재 손익률 : <b>{a['pnl_pct']:+.2f}%</b> ({a['pnl_krw']:+,.0f}원)\n"
            f"손절(-10%)까지 : <b>{a['remaining']:+.2f}%</b> 남음\n"
            f"평가금 {a['eval']:,.0f}원 ({a['qty']}주)\n"
            f"현재가 {a['cur']:,.0f}원 · 평단 {a['avg']:,.0f}원"
        )
        chart = _build_chart(a["ticker"], a["name"], a["avg"], a["cur"])
        if chart is not None:
            await bot.send_photo(
                chat_id=CHAT_ID, photo=chart, caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML,
            )


async def _send_ma_alerts(bot: Bot, alerts: list[dict]) -> None:
    n = len(alerts)
    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"📉 추세 이평선 깨짐 종목 {n}건  ·  15:30 점검",
    )
    for a in alerts:
        tf_label = {"short": "단기/테마", "long": "장기/구조"}.get(a["timeframe"], a["timeframe"])
        text = (
            f"<b>{a['name']}</b>  ·  {tf_label} ({a['ma_n']}일선)\n"
            f"종가 {a['cur']:,.0f} &lt; MA{a['ma_n']} {a['ma']:,.0f}  "
            f"(<b>{a['deviation']:+.2f}%</b> 이탈)\n"
            f"현재 손익률 : <b>{a['pnl_pct']:+.2f}%</b> ({a['pnl_krw']:+,.0f}원)\n"
            f"평가금 {a['eval']:,.0f}원 ({a['qty']}주) · 평단 {a['avg']:,.0f}원"
        )
        chart = _build_chart(a["ticker"], a["name"], a["avg"], a["cur"])
        if chart is not None:
            await bot.send_photo(
                chat_id=CHAT_ID, photo=chart, caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML,
            )


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    _setup_korean_font()

    token = _load_env_token() or os.environ.get("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN 미발견 — .env 또는 환경변수 확인")
        return 1

    stop_loss_alerts = _find_at_risk_positions()
    ma_alerts = _find_ma_breakdown_positions()

    if not stop_loss_alerts and not ma_alerts:
        logger.info("위태/이평선깨짐 모두 0건 — 무발송")
        return 0

    bot = Bot(token=token)
    async with bot:
        if stop_loss_alerts:
            logger.info("자동손절 임박 %d건 발송", len(stop_loss_alerts))
            await _send_alerts(bot, stop_loss_alerts)
        if ma_alerts:
            logger.info("이평선 깨짐 %d건 발송", len(ma_alerts))
            await _send_ma_alerts(bot, ma_alerts)
    logger.info("발송 완료 (자동손절 %d / MA깨짐 %d)", len(stop_loss_alerts), len(ma_alerts))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
