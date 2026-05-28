"""백테스트 명령 핸들러.

`백테스트` → 각 거래일의 포트폴리오(현물+선물)를 그때 그대로 동결하고 오늘 종가로
평가했을 때 NAV 가 현재 NAV 보다 높았던 날을 찾아 PNG + 표로 발송.

선물 만기는 다음 만기로 자동 롤오버 가정 (기초자산 가격 그대로 사용).
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from bot.backtest_html import build_backtest_html
from scripts.backtest_frozen_portfolio import run_backtest

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"

logger = logging.getLogger(__name__)


def _fmt_krw_short(x: float) -> str:
    if abs(x) >= 1e8:
        return f"{x/1e8:.2f}억"
    if abs(x) >= 1e7:
        return f"{x/1e7:.1f}천만"
    if abs(x) >= 1e4:
        return f"{x/1e4:.0f}만"
    return f"{x:,.0f}원"


async def backtest_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """백테스트 명령 처리."""
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text="백테스트 계산 중... (과거 종가 다운로드 포함, 10초 정도 걸립니다)",
    )

    try:
        res = await asyncio.to_thread(run_backtest)
    except Exception:
        logger.exception("백테스트 실행 실패")
        await context.bot.send_message(chat_id=chat_id, text="백테스트 실행 실패")
        return

    if res is None:
        await context.bot.send_message(
            chat_id=chat_id, text="거래 내역이 없습니다.",
        )
        return

    rows = res["rows"]
    higher = res["higher"]
    nav_now = res["nav_actual_today"]

    # 캡션: 요약 + 초과한 날 (있으면) top 5
    gross = res.get("nav_actual_today_gross", nav_now)
    cur_credit = res.get("cur_credit", 0)
    caption_lines = [
        "<b>포트폴리오 동결 백테스트 (현물+선물, 신용 제외)</b>",
        f"{rows[0]['date']} ~ {rows[-1]['date']} · 거래일 {len(rows)}일",
        f"현재 순자산 {_fmt_krw_short(nav_now)} "
        f"= 총자산 {_fmt_krw_short(gross)} − 신용 {_fmt_krw_short(cur_credit)}",
        f"  (현물 {_fmt_krw_short(res['cur_holdings_value'])} + "
        f"선물 {_fmt_krw_short(res['cur_futures_value'])} + "
        f"현금 {_fmt_krw_short(res['today_total_cash'])})",
        f"초기자본 {_fmt_krw_short(res['initial'])} "
        f"({(nav_now/res['initial']-1)*100:+.1f}% 순)",
    ]
    if higher:
        caption_lines.append(
            f"\n<b>★ 현재 순자산 초과 거래일: {len(higher)}/{len(rows)}건</b>"
        )
        for r in higher[:5]:
            diff = r["nav_frozen_today"] - nav_now
            caption_lines.append(
                f"  {r['date']:%m-%d} {_fmt_krw_short(r['nav_frozen_today'])} "
                f"(+{_fmt_krw_short(diff)})"
            )
        if len(higher) > 5:
            caption_lines.append(f"  ... +{len(higher)-5}건")
    else:
        caption_lines.append(
            f"\n→ <b>현재 순자산이 ALL-TIME HIGH</b> "
            f"(모든 거래일 동결 시나리오보다 높음)"
        )

    caption = "\n".join(caption_lines)

    # PNG 발송 (PNG 버퍼는 HTML 임베드용으로 재사용해야 하므로 복사)
    png_bytes = res["png_buf"].getvalue()
    photo_buf = io.BytesIO(png_bytes)
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=photo_buf,
        caption=caption,
        parse_mode="HTML",
    )

    # HTML 리포트 (PNG 임베드 + 전체 표) 파일로 발송
    try:
        # build_backtest_html 가 png_buf.getvalue() 를 호출하므로 새 버퍼 제공
        res["png_buf"] = io.BytesIO(png_bytes)
        html_buf = await asyncio.to_thread(build_backtest_html, res)
    except Exception:
        logger.exception("백테스트 HTML 렌더 실패")
        html_buf = None
    if html_buf is not None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = REPORTS_DIR / f"backtest_{ts}.html"
        data = html_buf.getvalue()
        fp.write_bytes(data)
        doc_buf = io.BytesIO(data)
        doc_buf.name = fp.name
        await context.bot.send_document(
            chat_id=chat_id,
            document=doc_buf,
            caption="백테스트 상세 리포트 (HTML)",
        )
