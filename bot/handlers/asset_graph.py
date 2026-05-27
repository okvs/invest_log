"""자산그래프 명령 핸들러.

`자산그래프` → 기록 첫날부터 현재까지의 일별 NAV 그래프 PNG 발송.
입출금 이벤트 마커 포함. 자동 추정 이벤트는 옅게 표시 (라벨에 `*`).
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.asset_history import compute_profit_trend, render_asset_graph
from storage.json_store import load_account

logger = logging.getLogger(__name__)


async def asset_graph_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """자산그래프 명령 처리."""
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text="자산 그래프 만드는 중...")

    try:
        buf = render_asset_graph()
    except Exception:
        logger.exception("자산그래프 렌더 실패")
        await context.bot.send_message(chat_id=chat_id, text="자산그래프 생성 실패")
        return

    if buf is None:
        await context.bot.send_message(
            chat_id=chat_id, text="자산 기록이 없습니다. 거래내역을 먼저 입력해주세요.",
        )
        return

    rows = compute_profit_trend()
    last = rows[-1] if rows else None
    account = load_account()
    initial = float(account.get("initial_capital") or 0)

    caption = "자산 추이"
    if last:
        asset = last["asset"]
        pct = (asset / initial - 1) * 100 if initial > 0 else 0.0
        caption = "\n".join([
            f"<b>수익금·평가금 추이</b>  ·  {rows[0]['date']} ~ {last['date']}",
            f"평가금 {int(asset):,}원 (초기자본 대비 {pct:+.1f}%)",
            f"실현 {int(last['realized']):,}원 · 미실현 {int(last['unrealized']):,}원 "
            f"· 합계 {int(last['profit']):,}원",
        ])

    await context.bot.send_photo(
        chat_id=chat_id, photo=buf, caption=caption, parse_mode="HTML",
    )
