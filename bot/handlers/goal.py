"""순자산 10억 목표 트래커 명령 핸들러.

`10억` → 현재 순자산 / 10억 진척률 + 2·5년 필요수익률 + 현재 페이스 +
파산방지선(고점-25%) + 마진콜 거리 한 화면 + 궤적 그래프 PNG 발송.
"""
from __future__ import annotations

import asyncio
import logging
import math

from telegram import Update
from telegram.ext import ContextTypes

from bot.asset_history import _fmt_eok_label, compute_profit_trend
from bot.goal_tracker import compute_goal_status, render_goal_graph
from storage.json_store import load_futures_positions

logger = logging.getLogger(__name__)


def _won(x: float) -> str:
    return f"{int(round(x)):,}원"


def _pct(x: float | None) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"{x * 100:+.1f}%"


async def _fetch_futures_prices(positions: list[dict]) -> dict:
    active = [p for p in positions if p.get("contracts", 0) > 0]
    if not active:
        return {}
    try:
        from bot.futures_quote import fetch_futures_quotes
        return await fetch_futures_quotes(active)
    except Exception:
        logger.warning("선물 시세 조회 실패 (마진콜 거리 생략)", exc_info=True)
        return {}


def _build_caption(s: dict) -> str:
    goal = s["goal"]
    lines = [
        "<b>🎯 순자산 10억 프로젝트</b>",
        f"현재 순자산 <b>{_fmt_eok_label(s['current'])}</b> "
        f"· 진척률 <b>{s['progress_pct']:.1f}%</b> · 남은 {_fmt_eok_label(s['remaining'])}",
        f"초기자본 {_fmt_eok_label(s['initial'])} 대비 {_pct(s['period_return'])} "
        f"(기록 {s['elapsed_days']}일)",
        "",
        "<b>필요 페이스 (지금부터)</b>",
    ]
    for h in s["horizons"]:
        lines.append(
            f"  {h['years']}년 (~{h['target_date']:%Y-%m}): "
            f"연 <b>{_pct(h['required_cagr'])}</b> · 월 {_pct(h['required_monthly'])}"
        )

    lines.append("")
    lines.append("<b>현재 페이스</b>")
    if s["short_sample"]:
        lines.append(
            f"  기록 {s['elapsed_days']}일간 {_pct(s['period_return'])} "
            f"(연환산 {_pct(s['realized_cagr'])})"
        )
        lines.append(
            "  <i>※ 표본이 짧아 연환산·도달예상은 과대평가될 수 있음 "
            "(최소 6개월 누적 후 신뢰).</i>"
        )
    else:
        lines.append(f"  실현 연복리 {_pct(s['realized_cagr'])}")
    if s["proj_date"] is not None:
        lines.append(f"  이 속도면 10억 도달 예상 ~{s['proj_date']:%Y-%m}")
    else:
        lines.append("  현재 페이스로는 도달 예상 산출 불가 (수익률 ≤ 0)")

    lines.append("")
    lines.append("<b>생존선</b>")
    lines.append(
        f"  고점 {_fmt_eok_label(s['peak'])} · 현재 낙폭 {_pct(s['drawdown'])}"
    )
    lines.append(
        f"  파산방지선(고점−25%) {_fmt_eok_label(s['ruin_line'])} "
        f"— 여기서 {_pct(s['drop_to_ruin'])} 더 빠지면 중단 규칙"
    )
    mc = s.get("margin_call")
    if mc and mc.get("x_call") is not None and mc.get("priced", 0) > 0:
        x = mc["x_call"]
        move = "하락" if mc["net_long"] else "상승"
        if x <= 0:
            lines.append("  마진콜: 이미 추가증거금 필요 수준 ⚠️")
        elif x >= 1:
            lines.append(f"  마진콜: 일괄 100% {move}해도 여유 (증거금 충분)")
        else:
            lines.append(
                f"  마진콜: 보유 선물 일괄 <b>{x * 100:.1f}% {move}</b> 시 추가증거금 "
                f"(유지 {mc['maint_note']})"
            )
    elif mc:
        lines.append("  마진콜: 선물 시세 미조회로 거리 산출 불가")

    return "\n".join(lines)


async def goal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """10억 트래커 명령 처리."""
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id, text="10억 트래커 계산 중... (시세 조회 포함)",
    )

    try:
        rows = await asyncio.to_thread(compute_profit_trend)
    except Exception:
        logger.exception("NAV 추이 계산 실패")
        await context.bot.send_message(chat_id=chat_id, text="10억 트래커 계산 실패")
        return

    if not rows:
        await context.bot.send_message(
            chat_id=chat_id, text="거래 내역이 없습니다. 거래를 먼저 입력해주세요.",
        )
        return

    futures_prices = await _fetch_futures_prices(load_futures_positions())

    try:
        status = compute_goal_status(rows, futures_prices)
        buf = await asyncio.to_thread(render_goal_graph, status)
    except Exception:
        logger.exception("10억 트래커 렌더 실패")
        await context.bot.send_message(chat_id=chat_id, text="10억 트래커 렌더 실패")
        return

    caption = _build_caption(status)
    if buf is not None:
        await context.bot.send_photo(
            chat_id=chat_id, photo=buf, caption=caption, parse_mode="HTML",
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=caption, parse_mode="HTML",
        )
