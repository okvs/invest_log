"""`융자` 명령 — 종목별 신용융자 잔액 동기화 ConversationHandler.

증권사 앱 잔고의 종목별 융자잔액을 줄 단위로 붙여넣으면 보유 종목의
credit_loan 을 실측값으로 맞춘다. 매수 시점 자금구성 입력(전액신용/현금NN)이
어긋나거나 신용이자 가산·상환으로 드리프트가 생겨도 주기 동기화로 잡는 안전망.

입력 형식 (줄마다): `종목명 금액` — 금액은 콤마/한국단위(억·천만·백만·만) 허용.
  SK하이닉스 61,391,161
  삼성전자 3914만
  삼성전기 0        ← 0 = 융자 없음(상환 완료)
종목명 매칭은 공백 제거 + 대소문자 무시. 입력에 없는 종목은 기존 값 유지.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.handlers.cash_event import _parse_amount
from parsers.input_parser import norm_stock_name
from storage.json_store import load_holdings, save_holdings

logger = logging.getLogger(__name__)

INPUT, CONFIRM = range(2)

APPLY_CB = "credit_sync:apply"
CANCEL_CB = "credit_sync:cancel"


# ---------------------------------------------------------------------------
# 순수 로직
# ---------------------------------------------------------------------------

def parse_credit_sync_input(text: str) -> dict[str, float]:
    """줄 단위 `종목명 금액` 파싱 → {입력종목명: 금액}.

    종목명에 공백 허용(마지막 토큰이 금액). 금액 미해석/음수 줄은 ValueError.
    """
    out: dict[str, float] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            raise ValueError(f"형식 인식 실패(종목명 금액): {line!r}")
        name = " ".join(parts[:-1])
        try:
            amount = _parse_amount(parts[-1])
        except ValueError as e:
            raise ValueError(f"금액 인식 실패: {line!r}") from e
        if amount < 0:
            raise ValueError(f"음수 금액은 허용되지 않습니다: {line!r}")
        out[name] = amount
    if not out:
        raise ValueError("인식된 줄이 없습니다.")
    return out


def apply_credit_sync(
    holdings: list[dict], parsed: dict[str, float],
) -> tuple[list[tuple[str, float, float]], list[str]]:
    """보유 종목 credit_loan 을 parsed 값으로 갱신 (in-place).

    반환: (changes [(이름, old, new)], unmatched [미매칭 입력명]).
    매칭은 norm_stock_name(공백제거+casefold) 기준, 보유수량>0 종목만.
    """
    active = {
        norm_stock_name(h.get("name", "")): h
        for h in holdings if h.get("quantity", 0) > 0
    }
    changes: list[tuple[str, float, float]] = []
    unmatched: list[str] = []
    for in_name, amount in parsed.items():
        h = active.get(norm_stock_name(in_name))
        if h is None:
            unmatched.append(in_name)
            continue
        old = float(h.get("credit_loan") or 0)
        if round(old) != round(amount):
            changes.append((h.get("name", ""), old, amount))
        h["credit_loan"] = amount
    return changes, unmatched


def _render_current(holdings: list[dict]) -> str:
    lines = []
    total = 0.0
    for h in holdings:
        if h.get("quantity", 0) <= 0:
            continue
        cl = float(h.get("credit_loan") or 0)
        total += cl
        lines.append(f"  {h.get('name')}: {int(cl):,}원")
    lines.append(f"  합계: {int(total):,}원")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 핸들러
# ---------------------------------------------------------------------------

async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    holdings = load_holdings()
    msg = (
        "📋 현재 봇에 기록된 종목별 신용융자:\n"
        f"{_render_current(holdings)}\n\n"
        "증권사 앱의 <b>종목별 융자잔액</b>을 줄마다 <code>종목명 금액</code> 형식으로 "
        "붙여넣어 주세요. (콤마·억/천만/만 단위 허용, 0 = 융자 없음)\n"
        "예)\nSK하이닉스 61,391,161\n삼성전자 3914만\n\n취소하려면 `취소` 입력."
    )
    await update.message.reply_text(msg, parse_mode="HTML")
    return INPUT


async def _receive_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text or ""
    if text.strip() in ("취소", "cancel"):
        return await _abort(update, context)
    try:
        parsed = parse_credit_sync_input(text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\n다시 입력해주세요. (취소: `취소`)")
        return INPUT

    holdings = load_holdings()
    preview = [dict(h) for h in holdings]  # 미리보기용 사본
    changes, unmatched = apply_credit_sync(preview, parsed)

    lines = ["<b>융자잔액 동기화 미리보기</b>"]
    if changes:
        for name, old, new in changes:
            lines.append(f"  {name}: {int(old):,} → <b>{int(new):,}</b>원")
    else:
        lines.append("  (변경 없음 — 모두 현재 기록과 동일)")
    if unmatched:
        lines.append("⚠️ 보유 종목에서 못 찾음(무시됨): " + ", ".join(unmatched))
    new_total = sum(
        float(h.get("credit_loan") or 0)
        for h in preview if h.get("quantity", 0) > 0
    )
    lines.append(f"적용 후 총 융자: {int(new_total):,}원")

    context.user_data["credit_sync_parsed"] = parsed
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 적용", callback_data=APPLY_CB),
        InlineKeyboardButton("취소", callback_data=CANCEL_CB),
    ]])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    return CONFIRM


async def _confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == CANCEL_CB:
        context.user_data.pop("credit_sync_parsed", None)
        await query.edit_message_text("융자잔액 동기화가 취소되었습니다.")
        return ConversationHandler.END

    parsed = context.user_data.pop("credit_sync_parsed", None)
    if not parsed:
        await query.edit_message_text("세션이 만료되었습니다. `융자` 부터 다시 시작해주세요.")
        return ConversationHandler.END

    holdings = load_holdings()
    changes, unmatched = apply_credit_sync(holdings, parsed)
    save_holdings(holdings)

    total = sum(
        float(h.get("credit_loan") or 0)
        for h in holdings if h.get("quantity", 0) > 0
    )
    lines = ["✓ 융자잔액 동기화 완료"]
    for name, old, new in changes:
        lines.append(f"  {name}: {int(old):,} → {int(new):,}원")
    if not changes:
        lines.append("  (변경 없음)")
    lines.append(f"총 융자: {int(total):,}원 — 잔고/총자산에 즉시 반영됩니다.")
    await query.edit_message_text("\n".join(lines))
    return ConversationHandler.END


async def _abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("credit_sync_parsed", None)
    await update.message.reply_text("융자잔액 동기화가 취소되었습니다.")
    return ConversationHandler.END


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|help|수정|회고|자산그래프|백테스트|10억|입금|출금|입출금목록|선물진입|선물청산|선물롤오버|선물회고|복기)$"
    ) | filters.COMMAND


def credit_sync_conversation() -> ConversationHandler:
    other_cmd = _other_command_filter()
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^융자$"), _start),
        ],
        states={
            INPUT: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_input),
            ],
            CONFIRM: [
                CallbackQueryHandler(_confirm, pattern=r"^credit_sync:"),
                MessageHandler(other_cmd, _abort),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _abort),
            CommandHandler("cancel", _abort),
        ],
        name="credit_sync",
        allow_reentry=True,
    )
