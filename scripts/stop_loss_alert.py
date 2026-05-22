#!/usr/bin/env python3
"""평일 15:30 KST 자동손절(-10%) 임박 푸시.

보유 종목 중 현재 손익률이 -10% ~ -5% 구간 (자동손절 5% 이내) 인 종목을
chat_id 778372474 로 푸시한다. 메시지에는 종목명·현재 손익률·손절선까지
남은 % 가 들어가고, 가능하면 25봉 일봉 차트 1장이 동봉된다.
위태 종목이 0건이면 아무것도 보내지 않는다.

launchd LaunchAgent (`com.seung.invest_log.stop_loss_alert.plist`) 에서 호출.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from pathlib import Path

# 프로젝트 루트 sys.path 등록
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import yfinance as yf
from matplotlib import font_manager
from telegram import Bot
from telegram.constants import ParseMode

from bot.formatters import _resolve_tickers, fetch_current_quotes
from storage.json_store import load_holdings

logger = logging.getLogger(__name__)

CHAT_ID = 778372474
STOP_LOSS_PCT = -10.0
WARN_THRESHOLD_PCT = -5.0  # 손익률이 이 값 이하 + STOP_LOSS_PCT 이상 → 위태
BARS = 25

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
        if avg <= 0:
            continue
        pnl_pct = (cur - avg) / avg * 100
        if STOP_LOSS_PCT <= pnl_pct <= WARN_THRESHOLD_PCT:
            at_risk.append({
                "name": name,
                "ticker": ticker,
                "cur": cur,
                "avg": avg,
                "pnl_pct": pnl_pct,
                "remaining": pnl_pct - STOP_LOSS_PCT,  # 양수, 작을수록 위태
            })
    # 손절선 근접 순(remaining 오름차순)
    at_risk.sort(key=lambda r: r["remaining"])
    return at_risk


def _build_chart(
    ticker: str, name: str, avg: float, cur: float, bars: int = BARS
) -> io.BytesIO | None:
    """25봉 일봉 line + high/low 음영 차트. 매수단가·손절(-10%)선 표시."""
    try:
        hist = yf.Ticker(ticker).history(period=f"{bars * 2 + 10}d", interval="1d")
        if hist.empty:
            return None
        hist = hist.tail(bars)
        fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=120)
        dates = hist.index
        close = hist["Close"]
        high = hist["High"]
        low = hist["Low"]
        ax.fill_between(dates, low, high, color="#888", alpha=0.18, linewidth=0)
        ax.plot(dates, close, color="#e0e0e0", linewidth=1.6)
        ax.scatter(dates[-1], close.iloc[-1], color="#fbbf24", s=44, zorder=5)

        ax.axhline(y=avg, color="#22c55e", linewidth=1.2, linestyle="--", alpha=0.85)
        ax.text(dates[0], avg, f" 매수 {avg:,.0f}", color="#22c55e",
                fontsize=8, va="bottom")

        sl = avg * (1 + STOP_LOSS_PCT / 100)
        ax.axhline(y=sl, color="#ef4444", linewidth=1.2, linestyle="--", alpha=0.85)
        ax.text(dates[0], sl, f" 손절 {sl:,.0f} (-10%)", color="#ef4444",
                fontsize=8, va="top")

        ax.text(dates[-1], cur, f"  {cur:,.0f}", color="#fbbf24",
                fontsize=8, va="center")

        ax.set_facecolor("#0f0f14")
        fig.patch.set_facecolor("#0f0f14")
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("bottom", "left"):
            ax.spines[spine].set_color("#444")
        ax.tick_params(colors="#888", labelsize=8)
        ax.yaxis.label.set_color("#888")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
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


async def _send_alerts(bot: Bot, alerts: list[dict]) -> None:
    n = len(alerts)
    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"⚠️ 자동손절(-10%) 임박 종목 {n}건  ·  15:30 점검",
    )
    for a in alerts:
        text = (
            f"<b>{a['name']}</b>\n"
            f"현재 손익률 : <b>{a['pnl_pct']:+.2f}%</b>\n"
            f"손절(-10%)까지 : <b>{a['remaining']:+.2f}%</b> 남음\n"
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

    alerts = _find_at_risk_positions()
    if not alerts:
        logger.info("위태 종목 없음 — 무발송")
        return 0

    logger.info("위태 종목 %d건 발견, 발송 시작", len(alerts))
    bot = Bot(token=token)
    async with bot:
        await _send_alerts(bot, alerts)
    logger.info("발송 완료 (%d건)", len(alerts))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
