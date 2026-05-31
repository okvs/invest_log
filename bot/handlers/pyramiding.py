"""피라미딩 기회 점검 — `피라미딩` 명령.

보유 현물 중 *오늘* 강한 돌파(전일대비 ≥4% + 종가 신고가 또는 갭상승)가 나고
수익 중인 종목을 찾아, 국룰 1천만원 추가매수 검토 대상으로 알려준다.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.pyramiding import PYRAMID_AMOUNT, detect_opportunities
from storage.json_store import load_holdings

logger = logging.getLogger(__name__)


def format_opportunities(opps: list[dict]) -> str:
    """피라미딩 기회 리스트를 텔레그램 HTML 메시지로."""
    if not opps:
        return (
            "📈 <b>피라미딩각</b> — 오늘은 신호 없음\n"
            "(전일대비 ≥4% + 종가 신고가/갭상승 + 수익 중 조건)"
        )
    amt = f"{PYRAMID_AMOUNT // 10000:,}만원"
    lines = [f"📈 <b>피라미딩각 {len(opps)}종목</b> — 오늘 강한 돌파 · 보유·수익 중"]
    for o in opps:
        lines.append(
            f"\n<b>{o['name']}</b> · {o['kind']} <b>{o['chg']:+.1f}%</b>\n"
            f"  현재가 {o['cur']:,.0f} · 평단대비 <b>{o['pnl_pct']:+.1f}%</b>\n"
            f"  국룰 {amt} ≈ <b>{o['suggested_shares']}주</b> 추가 검토"
        )
    lines.append("\n💡 이기는 포지션에만. 한 번에 ~1천만원 고정.")
    return "\n".join(lines)


async def pyramiding_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`피라미딩` — 오늘 피라미딩 기회 즉시 점검."""
    await update.message.reply_text("📈 피라미딩 기회 점검 중…")
    try:
        opps = await asyncio.to_thread(detect_opportunities, load_holdings())
    except Exception:
        logger.warning("피라미딩 점검 실패", exc_info=True)
        await update.message.reply_text("점검 중 오류가 났어요. 잠시 후 다시 시도해주세요.")
        return
    await update.message.reply_text(format_opportunities(opps), parse_mode=ParseMode.HTML)
