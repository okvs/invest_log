"""만기 임박 알림 단위 테스트."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.futures_alerts import (
    EXPIRY_ALERT_THRESHOLD_DAYS,
    build_alert_message,
    collect_expiry_alerts,
    run_daily_expiry_check,
)
from models.futures_position import FuturesPosition
from storage.json_store import (
    load_account,
    load_chat_id,
    save_account,
    save_chat_id,
    save_futures_positions,
)


def _make_pos(name="삼성전자", contracts=2, expiry="2026-05-24", direction="long") -> dict:
    return FuturesPosition(
        name=name,
        symbol="005930",
        contract_code="",
        contract_month=expiry.replace("-", "")[:6],
        expiry_date=expiry,
        direction=direction,
        contracts=contracts,
        avg_entry_price=70000.0,
        initial_margin=2520000.0,
    ).to_dict()


# ── collect_expiry_alerts ──────────────────────────────────────────────


def test_filters_only_near_expiry():
    today = date(2026, 5, 21)
    far = _make_pos(expiry="2026-09-10")
    near = _make_pos(expiry="2026-05-23")  # D-2
    after = _make_pos(expiry="2026-05-20", direction="short")  # 만기 경과
    alerts = collect_expiry_alerts([far, near, after], today=today)
    days = [a.days_to_expiry for a in alerts]
    # 만기 경과(-1) 가장 임박, 그 다음 D-2, 멀리 9월물은 제외
    assert days == [-1, 2]


def test_skips_zero_contract_positions():
    today = date(2026, 5, 21)
    closed = _make_pos(expiry="2026-05-22", contracts=0)
    alerts = collect_expiry_alerts([closed], today=today)
    assert alerts == []


def test_skips_invalid_expiry():
    today = date(2026, 5, 21)
    bad = _make_pos(expiry="")
    bad["expiry_date"] = ""
    alerts = collect_expiry_alerts([bad], today=today)
    assert alerts == []


def test_alert_render_text():
    today = date(2026, 5, 21)
    pos = _make_pos(expiry="2026-05-24")  # D-3
    [alert] = collect_expiry_alerts([pos], today=today)
    text = alert.render()
    assert "삼성전자" in text
    assert "롱" in text
    assert "2계약" in text
    assert "D-3" in text


def test_build_alert_message_empty():
    assert build_alert_message([]) == ""


def test_build_alert_message_combines_lines():
    today = date(2026, 5, 21)
    pos1 = _make_pos(name="삼성전자", expiry="2026-05-22")
    pos2 = _make_pos(name="SK하이닉스", expiry="2026-05-24")
    alerts = collect_expiry_alerts([pos1, pos2], today=today)
    msg = build_alert_message(alerts)
    assert "삼성전자" in msg
    assert "SK하이닉스" in msg
    assert "선물롤오버" in msg
    assert "선물청산" in msg


def test_threshold_default_three_days():
    assert EXPIRY_ALERT_THRESHOLD_DAYS == 3


# ── chat_id 저장/로드 ──────────────────────────────────────────────────


def test_chat_id_persists():
    assert load_chat_id() is None
    save_chat_id(123456)
    assert load_chat_id() == 123456
    # idempotent
    save_chat_id(123456)
    assert load_chat_id() == 123456


# ── JobQueue 콜백 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_skips_when_no_chat_id():
    """chat_id 미설정 시 발송 시도 자체를 안 한다."""
    save_futures_positions([_make_pos(expiry="2026-05-22")])
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    await run_daily_expiry_check(context)
    context.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_job_sends_when_alerts_pending(monkeypatch):
    """alerts가 있으면 send_message 호출되고 last_expiry_alert가 갱신된다."""
    from bot import futures_alerts
    save_chat_id(987654)
    save_futures_positions([_make_pos(expiry=date.today().isoformat())])  # D-0

    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    await run_daily_expiry_check(context)
    context.bot.send_message.assert_called_once()
    kwargs = context.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 987654
    assert "오늘이 만기일" in kwargs["text"]

    # 같은 날 두 번 호출 시 발송하지 않음
    context.bot.send_message.reset_mock()
    await run_daily_expiry_check(context)
    context.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_job_no_alerts_no_send():
    """알림 대상 없으면 발송하지 않음."""
    save_chat_id(987654)
    save_futures_positions([_make_pos(expiry="2026-12-10")])  # 한참 멀음
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    await run_daily_expiry_check(context)
    context.bot.send_message.assert_not_called()
