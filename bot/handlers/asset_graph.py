"""자산그래프 명령 핸들러.

`자산그래프` → 기록 첫날부터 현재까지의 일별 NAV 그래프 PNG 발송.
입출금 이벤트 마커 포함. 자동 추정 이벤트는 옅게 표시 (라벨에 `*`).
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.asset_history import compute_profit_trend, render_asset_graph
from bot.formatters import _resolve_tickers, fetch_current_quotes
from bot.goal_tracker import compute_balance_nav
from storage.json_store import load_account, load_futures_positions, load_holdings


async def _balance_nav() -> float | None:
    """실제 잔고(전부 청산) — 시세 조회 후 계산. 실패하면 None(미보정)."""
    try:
        holdings = load_holdings()
        futures_positions = load_futures_positions()
        active = [h for h in holdings if h.get("quantity", 0) > 0]
        name_to_ticker, _ = _resolve_tickers(active) if active else ({}, [])
        tickers = list(set(name_to_ticker.values()))
        spot_quotes = await asyncio.to_thread(fetch_current_quotes, tickers) if tickers else {}
        futures_prices = {}
        fut_active = [p for p in futures_positions if p.get("contracts", 0) > 0]
        if fut_active:
            from bot.futures_quote import fetch_futures_quotes
            futures_prices = await fetch_futures_quotes(fut_active)
        bal = compute_balance_nav(
            spot_quotes, futures_prices,
            holdings=holdings, futures_positions=futures_positions,
        )
        return bal["nav"]
    except Exception:
        logger.warning("실제 잔고 계산 실패 (그래프 미보정)", exc_info=True)
        return None

logger = logging.getLogger(__name__)


async def asset_graph_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """자산그래프 명령 처리."""
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text="자산 그래프 만드는 중...")

    # 실제 잔고(전부 청산) 로 끝점 보정 → 그래프가 잔고 대시보드와 일치
    target_nav = await _balance_nav()

    try:
        buf = await asyncio.to_thread(render_asset_graph, target_nav)
    except Exception:
        logger.exception("자산그래프 렌더 실패")
        await context.bot.send_message(chat_id=chat_id, text="자산그래프 생성 실패")
        return

    if buf is None:
        await context.bot.send_message(
            chat_id=chat_id, text="자산 기록이 없습니다. 거래내역을 먼저 입력해주세요.",
        )
        return

    rows = compute_profit_trend(target_nav=target_nav)
    last = rows[-1] if rows else None
    account = load_account()
    initial = float(account.get("initial_capital") or 0)

    caption = "자산 추이"
    if last:
        asset = last["asset"]
        pct = (asset / initial - 1) * 100 if initial > 0 else 0.0
        note = " · 실제 잔고 기준" if target_nav is not None else ""
        caption = "\n".join([
            f"<b>순자산 추이</b>  ·  {rows[0]['date']} ~ {last['date']}{note}",
            f"순자산 {int(asset):,}원 (초기자본 대비 {pct:+.1f}%)",
            f"실현 {int(last['realized']):,}원 · 미실현 {int(last['unrealized']):,}원 "
            f"· 합계 {int(last['profit']):,}원",
        ])

    await context.bot.send_photo(
        chat_id=chat_id, photo=buf, caption=caption, parse_mode="HTML",
    )
