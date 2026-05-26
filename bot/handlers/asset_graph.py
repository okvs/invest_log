"""자산그래프 명령 핸들러.

`자산그래프` → 기록 첫날부터 현재까지의 일별 NAV 그래프 PNG 발송.
입출금 이벤트 마커 포함. 자동 추정 이벤트는 옅게 표시 (라벨에 `*`).
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.asset_history import compute_daily_nav, render_asset_graph, ensure_seed_cash_event

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

    rows = compute_daily_nav()
    events = ensure_seed_cash_event()
    auto = [e for e in events if "자동" in (e.get("note") or "")]
    pct = (rows[-1]["nav"] / rows[0]["nav"] - 1) * 100 if rows else 0
    caption_lines = [
        f"<b>자산 추이</b>  ·  {rows[0]['date']} ~ {rows[-1]['date']}",
        f"첫날 {int(rows[0]['nav']):,}원 → 오늘 {int(rows[-1]['nav']):,}원 ({pct:+.1f}%)",
    ]
    if auto:
        caption_lines.append(
            f"\n⚠️ 자동 추정 입출금 {len(auto)}건(★ 표시) — 실제 시점은 추후 입출금 명령으로 등록 권장",
        )
    caption = "\n".join(caption_lines)

    await context.bot.send_photo(
        chat_id=chat_id, photo=buf, caption=caption, parse_mode="HTML",
    )
