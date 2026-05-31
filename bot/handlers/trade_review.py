"""매매 복기 — `복기` 명령.

보유 현물 전체를 *한 HTML 파일*의 종목별 탭으로 묶어 보낸다. 각 탭은 일봉 캔들 +
내 ▲매수/▼매도 마커 + 체결가 노란선 + 거래량 + ⭐급등(≥10%)봉, 마우스 오버 시
상승률(전일대비)을 보여주는 인터랙티브 plotly 차트.

차트의 거래 마커는 yfinance 일봉과 동일한 스케일된 가격우주라 그대로 정렬된다.
거래 매칭은 같은 티커를 공유하는 모든 종목명(과거명/별칭 포함)으로 한다.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from bot.formatters import _resolve_tickers, fetch_current_quotes
from bot.trade_review import build_review_tabs_html
from storage.json_store import load_holdings, load_ticker_map, load_transactions

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"


def _build_order() -> list[dict]:
    """복기 대상 보유 현물을 평가금 내림차순으로 정렬해 반환.

    각 항목에 현재가/등락률/손익률을 채운다(시세 실패 시 None).
    """
    active = [h for h in load_holdings() if h.get("quantity", 0) > 0]
    if not active:
        return []

    name_to_ticker, _ = _resolve_tickers(active)
    tickers = list({t for t in name_to_ticker.values() if t})
    quotes = fetch_current_quotes(tickers) if tickers else {}

    order: list[dict] = []
    for h in active:
        name = h["name"]
        ticker = h.get("ticker") or name_to_ticker.get(name, "")
        qty = int(h.get("quantity", 0) or 0)
        invested = float(h.get("total_invested", 0) or 0)
        q = quotes.get(ticker) or {}
        cur = q.get("price")
        chg = q.get("change_pct")
        if cur and cur > 0:
            eval_amt = cur * qty
            pct = ((eval_amt - invested) / invested * 100) if invested > 0 else None
        else:
            eval_amt = invested
            pct = None
        order.append({
            "name": name,
            "ticker": ticker,
            "sector": h.get("sector", ""),
            "quantity": qty,
            "avg_price": float(h.get("avg_price", 0) or 0),
            "total_invested": invested,
            "cur": cur,
            "change_pct": chg,
            "pct": pct,
            "eval": eval_amt,
        })
    order.sort(key=lambda x: x["eval"], reverse=True)
    return order


def _build_tabs_file() -> io.BytesIO | None:
    """보유 현물 전체 → 한 파일 탭 HTML (블로킹: 시세조회 + plotly)."""
    order = _build_order()
    if not order:
        return None
    alltx = load_transactions()
    tmap = load_ticker_map()
    items: list[dict] = []
    for o in order:
        ticker = o.get("ticker")
        alias = {o["name"]}
        if ticker:
            alias |= {nm for nm, tk in tmap.items() if tk == ticker}
        txs = [
            t for t in alltx
            if t.get("name") in alias and t.get("type") in ("buy", "sell")
        ]
        items.append({"holding": o, "transactions": txs})
    return build_review_tabs_html(items)


def _save_locally(buf: io.BytesIO) -> io.BytesIO:
    """reports/ 에 저장하고 전송용 새 BytesIO 반환."""
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = REPORTS_DIR / f"trade_review_{ts}.html"
        fp.write_bytes(buf.getvalue())
        logger.info("복기 리포트 저장: %s", fp)
        out = io.BytesIO(buf.getvalue())
        out.name = fp.name
        return out
    except Exception:
        logger.warning("복기 리포트 로컬 저장 실패", exc_info=True)
        buf.seek(0)
        return buf


async def review_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`복기` — 보유 현물 전체를 종목별 탭 하나의 HTML 로 전송."""
    active = [h for h in load_holdings() if h.get("quantity", 0) > 0]
    if not active:
        await update.message.reply_text("복기할 보유 현물이 없습니다.")
        return

    await update.message.reply_text(
        f"📈 매매 복기 — 보유 현물 {len(active)}종목 차트를 만드는 중… (잠시만요)"
    )
    try:
        buf = await asyncio.to_thread(_build_tabs_file)
    except Exception:
        logger.warning("복기 HTML 생성 실패", exc_info=True)
        buf = None

    if buf is None:
        await update.message.reply_text(
            "차트를 만들지 못했어요(시세 조회 실패 등). 잠시 후 다시 시도해주세요."
        )
        return

    buf = _save_locally(buf)
    await update.message.reply_document(
        document=buf,
        caption="매매 복기 — 상단 탭으로 종목 전환 · 봉에 마우스 올리면 상승률 · ⭐는 +10% 급등봉",
    )
