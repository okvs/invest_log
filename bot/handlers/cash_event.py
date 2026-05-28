"""입금/출금 이벤트 등록 + 목록 조회.

플로우:
  입금 / 출금 → 날짜 + 금액 + (옵션) 메모 입력 → cash_events.json 추가
  입출금목록 → 최근 이벤트 표시

입력 형식 (자유):
  `2026-04-15 5천만 월급`  → 1줄 입력
  `오늘 1000만`            → '오늘' 키워드 허용
  `04-15 50000000`         → 연도 생략 시 올해
또는 줄바꿈으로:
  `2026-04-15`
  `5000만`
  `(메모 옵션)`
"""
from __future__ import annotations

import logging
import re
from datetime import date

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from storage.json_store import (
    add_cash_event,
    load_cash_events,
    save_cash_events,
)

logger = logging.getLogger(__name__)

INPUT = 0


# ---------------------------------------------------------------------------
# 파서
# ---------------------------------------------------------------------------

def _parse_amount(token: str) -> float:
    """`5천만`/`1억`/`5,000,000`/`50000000` → float.

    한국어 단위: 천만(1e7), 백만(1e6), 만(1e4), 억(1e8)
    """
    t = token.replace(",", "").replace("원", "").strip()
    if not t:
        raise ValueError("금액이 비어 있습니다.")
    # 한국어 단위 처리
    m = re.match(r"^(\d+(?:\.\d+)?)(억|천만|백만|만)?$", t)
    if m:
        num = float(m.group(1))
        unit = m.group(2) or ""
        mult = {"억": 1e8, "천만": 1e7, "백만": 1e6, "만": 1e4, "": 1}[unit]
        return num * mult
    # 순수 숫자
    return float(t)


def _parse_date(token: str) -> date:
    """`2026-04-15`/`04-15`/`오늘` → date."""
    t = token.strip()
    if t in ("오늘", "today"):
        return date.today()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        return date.fromisoformat(t)
    if re.match(r"^\d{1,2}-\d{1,2}$", t):
        # 연도 생략 → 올해
        mo, day = t.split("-")
        return date(date.today().year, int(mo), int(day))
    raise ValueError(f"날짜 형식 인식 실패: {t!r}")


def parse_cash_event_input(text: str) -> tuple[date, float, str]:
    """자유 입력을 (date, amount, note) 로 파싱.

    한 줄: `날짜 금액 [메모...]`  공백 split
    여러 줄: 각 줄을 날짜/금액/메모 순서로
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    tokens: list[str]
    if len(lines) == 1:
        tokens = lines[0].split(maxsplit=2)
    else:
        tokens = lines[:3]
    if len(tokens) < 2:
        raise ValueError("날짜와 금액을 모두 입력해주세요.")
    d = _parse_date(tokens[0])
    amt = _parse_amount(tokens[1])
    note = tokens[2] if len(tokens) >= 3 else ""
    return d, amt, note


# ---------------------------------------------------------------------------
# 핸들러
# ---------------------------------------------------------------------------

def _ev_type_kr(ev_type: str) -> str:
    return {"seed": "시드", "deposit": "입금", "withdraw": "출금"}.get(ev_type, ev_type)


async def _start_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cash_ev_type"] = "deposit"
    await update.message.reply_text(
        "입금 정보를 입력해주세요.\n"
        "예) `2026-04-15 5천만 월급` 또는 줄바꿈으로 날짜/금액/메모 입력\n"
        "(`오늘`, `04-15` 도 허용)",
        parse_mode="Markdown",
    )
    return INPUT


async def _start_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cash_ev_type"] = "withdraw"
    await update.message.reply_text(
        "출금 정보를 입력해주세요.\n"
        "예) `2026-04-15 1천만 인출`",
        parse_mode="Markdown",
    )
    return INPUT


async def _receive_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ev_type = context.user_data.pop("cash_ev_type", "deposit")
    try:
        d, amt, note = parse_cash_event_input(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"입력 오류: {e}\n\n다시 입력해주세요.")
        context.user_data["cash_ev_type"] = ev_type
        return INPUT

    add_cash_event(d.isoformat(), amt, ev_type, note)
    label = _ev_type_kr(ev_type)
    msg = (
        f"✓ {label} 등록 완료\n"
        f"날짜: {d.isoformat()}\n"
        f"금액: {int(amt):,}원\n"
    )
    if note:
        msg += f"메모: {note}\n"
    msg += "\n`자산그래프` 명령으로 반영 확인."
    await update.message.reply_text(msg, parse_mode="Markdown")
    return ConversationHandler.END


async def list_cash_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """등록된 입출금 이벤트 목록 표시."""
    events = load_cash_events()
    if not events:
        await update.message.reply_text("등록된 입출금 이벤트가 없습니다.")
        return
    lines = ["<b>입출금 목록</b>\n"]
    events_sorted = sorted(events, key=lambda e: e.get("date", ""))
    for i, e in enumerate(events_sorted, 1):
        label = _ev_type_kr(e.get("type", ""))
        amt = int(e.get("amount", 0))
        note = e.get("note", "")
        line = f"{i}. {e.get('date','')} · {label} · {amt:,}원"
        if note:
            line += f"  <i>{note}</i>"
        lines.append(line)
    lines.append(
        "\n번호 삭제는 `입출금삭제 <번호>` (예: 입출금삭제 3)"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def delete_cash_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`입출금삭제 <번호>` — 1-based 번호로 삭제."""
    text = update.message.text.strip()
    m = re.match(r"^입출금삭제\s+(\d+)\s*$", text)
    if not m:
        await update.message.reply_text("사용법: `입출금삭제 3` (입출금목록의 번호)", parse_mode="Markdown")
        return
    idx = int(m.group(1)) - 1
    events = sorted(load_cash_events(), key=lambda e: e.get("date", ""))
    if idx < 0 or idx >= len(events):
        await update.message.reply_text(f"번호 {idx + 1} 이 범위를 벗어납니다.")
        return
    removed = events.pop(idx)
    save_cash_events(events)
    label = _ev_type_kr(removed.get("type", ""))
    await update.message.reply_text(
        f"✓ 삭제 완료: {removed.get('date','')} · {label} · {int(removed.get('amount', 0)):,}원"
    )


async def _abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("cash_ev_type", None)
    await update.message.reply_text("입출금 등록이 취소되었습니다.")
    return ConversationHandler.END


def _other_command_filter() -> filters.BaseFilter:
    return filters.Regex(
        r"^(매도|매수|현황|잔고|도움말|수정|회고|자산그래프|백테스트|입금|출금|입출금목록|선물진입|선물청산|선물롤오버|선물회고)$"
    ) | filters.COMMAND


def deposit_conversation() -> ConversationHandler:
    other_cmd = _other_command_filter()
    return ConversationHandler(
        entry_points=[
            CommandHandler("deposit", _start_deposit),
            CommandHandler("withdraw", _start_withdraw),
            MessageHandler(filters.Regex(r"^입금$"), _start_deposit),
            MessageHandler(filters.Regex(r"^출금$"), _start_withdraw),
        ],
        states={
            INPUT: [
                MessageHandler(other_cmd, _abort),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_input),
            ],
        },
        fallbacks=[
            MessageHandler(other_cmd, _abort),
            CommandHandler("cancel", _abort),
        ],
        name="cash_event",
        allow_reentry=True,
    )
