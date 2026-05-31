"""매매 복기 ConversationHandler.

플로우:
  복기 → 보유 현물 목록(손익률) → 종목 선택 →
  일봉 캔들 + 내 ▲매수/▼매도 마커 차트 + 요약 캡션 →
  [◀ 이전 / 다음 ▶ / 📋 목록 / 종료] 로 한 종목씩 되짚기.

차트의 거래 마커는 yfinance 일봉과 동일한 스케일된 가격우주라 그대로 정렬된다.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

from bot.formatters import _resolve_tickers, fetch_current_quotes
from bot.keyboards import (
    REVIEW_PICK_PREFIX,
    RV_DONE,
    RV_LIST,
    RV_NEXT,
    RV_PREV,
    review_nav_keyboard,
    review_select_keyboard,
)
from bot.trade_review import build_trade_chart, summarize_review
from storage.json_store import load_holdings, load_ticker_map, load_transactions

logger = logging.getLogger(__name__)

SELECT, VIEWING = range(2)


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


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = await asyncio.to_thread(_build_order)
    if not order:
        await update.message.reply_text("복기할 보유 현물이 없습니다.")
        return ConversationHandler.END

    context.user_data["review_order"] = order
    context.user_data.pop("review_nav_msg_id", None)
    items = [{"name": o["name"], "qty": o["quantity"], "pct": o["pct"]} for o in order]
    await update.message.reply_text(
        f"📈 매매 복기 — 보유 현물 {len(order)}종목\n복기할 종목을 선택하세요:",
        reply_markup=review_select_keyboard(items),
    )
    return SELECT


async def _clear_prev_nav(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """직전 차트의 네비 버튼을 제거해 오작동(옛 버튼 클릭) 방지."""
    msg_id = context.user_data.pop("review_nav_msg_id", None)
    if not msg_id:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=msg_id, reply_markup=None
        )
    except TelegramError:
        pass


async def _send_chart(context: ContextTypes.DEFAULT_TYPE, chat_id: int, idx: int) -> None:
    """현재 인덱스 종목의 복기 차트 + 캡션 + 네비를 전송."""
    order: list[dict] = context.user_data["review_order"]
    idx = max(0, min(idx, len(order) - 1))
    context.user_data["review_idx"] = idx
    h = order[idx]

    await _clear_prev_nav(context, chat_id)

    # 거래 매칭: 종목명 + 같은 티커를 공유하는 모든 이름(과거명/별칭) → rename·alias 에도
    # 전체 매매 여정을 보존한다. transaction_ids 는 현재 보유분만 담겨 일부 누락되므로 안 씀.
    alias_names = {h["name"]}
    ticker = h.get("ticker")
    if ticker:
        alias_names |= {nm for nm, tk in load_ticker_map().items() if tk == ticker}
    txs = [
        t for t in load_transactions()
        if t.get("name") in alias_names and t.get("type") in ("buy", "sell")
    ]
    caption = summarize_review(
        h, txs, cur_price=h.get("cur"), change_pct=h.get("change_pct"),
        position=(idx + 1, len(order)),
    )
    try:
        chart = await asyncio.to_thread(
            build_trade_chart, h["name"], h["ticker"], txs, h["avg_price"], h.get("cur"),
        )
    except Exception:
        logger.warning("%s 복기 차트 생성 실패", h["name"], exc_info=True)
        chart = None

    if chart is not None:
        await context.bot.send_photo(
            chat_id=chat_id, photo=chart, caption=caption, parse_mode=ParseMode.HTML,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption + "\n\n⚠️ 일봉 시세를 불러오지 못해 차트는 생략했습니다.",
            parse_mode=ParseMode.HTML,
        )

    nav = await context.bot.send_message(
        chat_id=chat_id,
        text=f"[{idx + 1}/{len(order)}] {h['name']} 복기 중",
        reply_markup=review_nav_keyboard(idx, len(order)),
    )
    context.user_data["review_nav_msg_id"] = nav.message_id


async def _pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if "review_order" not in context.user_data:
        await query.edit_message_text("세션이 만료됐어요. '복기'를 다시 입력해주세요.")
        return ConversationHandler.END
    try:
        idx = int(query.data.removeprefix(REVIEW_PICK_PREFIX))
    except ValueError:
        return SELECT
    await _send_chart(context, update.effective_chat.id, idx)
    return VIEWING


async def _nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if "review_order" not in context.user_data:
        await query.edit_message_text("세션이 만료됐어요. '복기'를 다시 입력해주세요.")
        return ConversationHandler.END

    data = query.data
    chat_id = update.effective_chat.id
    order = context.user_data["review_order"]
    idx = context.user_data.get("review_idx", 0)

    if data == RV_DONE:
        await _clear_prev_nav(context, chat_id)
        await context.bot.send_message(chat_id=chat_id, text="복기를 마쳤습니다. 👍")
        _cleanup(context)
        return ConversationHandler.END

    if data == RV_LIST:
        await _clear_prev_nav(context, chat_id)
        items = [{"name": o["name"], "qty": o["quantity"], "pct": o["pct"]} for o in order]
        await context.bot.send_message(
            chat_id=chat_id,
            text="복기할 종목을 선택하세요:",
            reply_markup=review_select_keyboard(items),
        )
        return SELECT

    if data == RV_NEXT:
        idx += 1
    elif data == RV_PREV:
        idx -= 1
    await _send_chart(context, chat_id, idx)
    return VIEWING


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("복기가 취소되었습니다.")
    _cleanup(context)
    return ConversationHandler.END


def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("review_order", "review_idx", "review_nav_msg_id"):
        context.user_data.pop(k, None)


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|help|수정|회고|자산그래프|백테스트|10억|입금|출금|입출금목록|선물진입|선물청산|선물롤오버|선물회고|복기)$"
    ) | filters.COMMAND


def trade_review_conversation() -> ConversationHandler:
    other_cmd = _other_command_filter()
    return ConversationHandler(
        entry_points=[
            CommandHandler("review", _start),
            MessageHandler(filters.Regex(r"^복기$"), _start),
        ],
        states={
            SELECT: [
                CallbackQueryHandler(_pick, pattern=f"^{REVIEW_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
            VIEWING: [
                CallbackQueryHandler(_nav, pattern=r"^rv_nav:"),
                # 위로 스크롤해 옛 목록 버튼(rv_pick)을 눌러도 바로 그 종목으로 점프 가능하게.
                CallbackQueryHandler(_pick, pattern=f"^{REVIEW_PICK_PREFIX}"),
                MessageHandler(other_cmd, _cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _cancel),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _cancel),
            CommandHandler("cancel", _cancel),
        ],
        name="trade_review",
        allow_reentry=True,
    )
