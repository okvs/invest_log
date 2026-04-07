"""보유 종목 수정 ConversationHandler.

플로우:
  수정 → 종목 선택 → 현재 정보 표시 → 새 정보 입력 → 저장
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.keyboards import EDIT_SELECT_PREFIX, edit_select_keyboard
from parsers.input_parser import search_stocks
from storage.json_store import (
    load_holdings,
    load_ticker_map,
    save_holdings,
    save_ticker_map,
)

logger = logging.getLogger(__name__)

SELECT, INPUT = range(2)


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """수정 시작 → 보유 종목 카드 표시."""
    holdings = load_holdings()
    active = [h for h in holdings if h.get("quantity", 0) > 0]

    if not active:
        await update.message.reply_text("보유 중인 종목이 없습니다.")
        return ConversationHandler.END

    await update.message.reply_text(
        "수정할 종목을 선택해주세요:",
        reply_markup=edit_select_keyboard(active),
    )
    return SELECT


async def _select_holding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """종목 선택 → 현재 정보 표시 + 새 정보 입력 안내."""
    query = update.callback_query
    await query.answer()

    name = query.data.removeprefix(EDIT_SELECT_PREFIX)
    context.user_data["edit_name"] = name

    holdings = load_holdings()
    target = None
    for h in holdings:
        if h["name"] == name:
            target = h
            break

    if not target:
        await query.edit_message_text("종목을 찾을 수 없습니다.")
        return ConversationHandler.END

    ticker = target.get("ticker", "") or "없음"
    notes = target.get("research_notes", "")

    current_info = (
        f"현재 정보:\n"
        f"  종목명: {target['name']}\n"
        f"  종목코드: {ticker}\n"
        f"  섹터: {target.get('sector', '')}\n"
        f"  수량: {target['quantity']}주\n"
        f"  평균단가: {target['avg_price']:,.0f}원\n"
        f"  매수근거: {target.get('buy_thesis', '')}\n"
        f"  참고자료: {notes}\n"
    )

    # 복사해서 바로 붙여넣기 가능한 형식
    copy_block = (
        f"{target['name']}\n"
        f"{target.get('sector', '')}\n"
        f"{target['quantity']}주\n"
        f"{target['avg_price']:,.0f}원\n"
        f"{target.get('buy_thesis', '')}"
    )
    if notes:
        copy_block += f"\n{notes}"

    await query.edit_message_text(
        f"{current_info}\n"
        "아래를 복사해서 수정 후 보내주세요:\n\n"
        f"{copy_block}"
    )
    return INPUT


async def _receive_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """수정 입력 파싱 → 저장."""
    import re

    text = update.message.text
    edit_name = context.user_data.pop("edit_name", "")

    if not edit_name:
        await update.message.reply_text("세션이 만료되었습니다. 다시 수정을 시작해주세요.")
        return ConversationHandler.END

    # 라벨 제거 + 불필요한 줄 스킵
    _SKIP_LABELS = {"종목코드"}
    lines = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # "종목명: 값", "섹터: 값" 등 라벨 제거
        if ":" in line:
            label, _, value = line.partition(":")
            label = label.strip()
            value = value.strip()
            # 종목코드 등 입력 불필요한 필드 스킵
            if label in _SKIP_LABELS:
                continue
            # 라벨 뒤 값이 비어있으면 스킵 (예: "참고자료:")
            if not value:
                continue
            line = value
        lines.append(line)
    if len(lines) < 5:
        await update.message.reply_text(
            "입력이 부족합니다. 다음 형식으로 입력해주세요:\n"
            "종목명\n섹터\n수량(예: 10주)\n평균단가(예: 72000원)\n매수 근거"
        )
        return INPUT

    try:
        new_name = lines[0]
        new_sector = lines[1]
        new_qty = int(float(re.sub(r"[^\d.]", "", lines[2].replace(",", ""))))
        new_price = float(re.sub(r"[^\d.]", "", lines[3].replace(",", "")))
        new_thesis = lines[4]
        new_notes = "\n".join(lines[5:]) if len(lines) > 5 else ""
    except (ValueError, IndexError) as e:
        await update.message.reply_text(f"입력 오류: {e}\n\n다시 입력해주세요.")
        return INPUT

    if new_qty <= 0:
        await update.message.reply_text("수량은 1 이상이어야 합니다.")
        return INPUT
    if new_price <= 0:
        await update.message.reply_text("단가는 0보다 커야 합니다.")
        return INPUT

    holdings = load_holdings()
    target_idx = None
    for idx, h in enumerate(holdings):
        if h["name"] == edit_name:
            target_idx = idx
            break

    if target_idx is None:
        await update.message.reply_text("종목을 찾을 수 없습니다.")
        return ConversationHandler.END

    h = holdings[target_idx]

    # 종목명 변경 시 종목코드 재검색
    new_ticker = h.get("ticker", "")
    if new_name != edit_name:
        tmap = load_ticker_map()
        cached = tmap.get(new_name, "")
        if cached:
            new_ticker = cached
        else:
            try:
                candidates = await asyncio.to_thread(search_stocks, new_name)
                exact = [c for c in candidates if c.name == new_name]
                if exact:
                    suffix = ".KQ" if exact[0].market == "KOSDAQ" else ".KS"
                    new_ticker = exact[0].code + suffix
                    tmap[new_name] = new_ticker
                    save_ticker_map(tmap)
            except Exception:
                logger.warning("종목코드 검색 실패: %s", new_name)

    h["name"] = new_name
    h["sector"] = new_sector
    h["quantity"] = new_qty
    h["avg_price"] = new_price
    h["total_invested"] = new_price * new_qty
    h["buy_thesis"] = new_thesis
    h["research_notes"] = new_notes
    if new_ticker:
        h["ticker"] = new_ticker

    save_holdings(holdings)

    ticker_display = f" [{new_ticker}]" if new_ticker else ""
    await update.message.reply_text(
        f"수정 완료!\n"
        f"{new_name}{ticker_display} ({new_sector}) "
        f"{new_qty}주 x {new_price:,.0f}원 = {new_price * new_qty:,.0f}원\n"
        f'근거: "{new_thesis}"'
    )
    return ConversationHandler.END


def edit_conversation() -> ConversationHandler:
    """수정 ConversationHandler를 생성하여 반환."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("edit", _start),
            MessageHandler(filters.Regex(r"^수정$"), _start),
        ],
        states={
            SELECT: [
                CallbackQueryHandler(
                    _select_holding, pattern=f"^{EDIT_SELECT_PREFIX}"
                ),
            ],
            INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_edit),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="edit",
        persistent=True,
        conversation_timeout=300,
    )


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("edit_name", None)
    await update.message.reply_text("수정이 취소되었습니다.")
    return ConversationHandler.END
