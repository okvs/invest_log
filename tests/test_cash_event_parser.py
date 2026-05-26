"""입출금 입력 파서 단위 테스트."""
from __future__ import annotations

from datetime import date

import pytest

from bot.handlers.cash_event import (
    _parse_amount,
    _parse_date,
    parse_cash_event_input,
)


# ── 금액 ──────────────────────────────────────────────────────────────


def test_parse_amount_raw_number():
    assert _parse_amount("50000000") == 50_000_000.0


def test_parse_amount_with_commas():
    assert _parse_amount("50,000,000") == 50_000_000.0


def test_parse_amount_with_unit_eok():
    assert _parse_amount("1억") == 1e8


def test_parse_amount_with_unit_chunman():
    assert _parse_amount("5천만") == 5e7


def test_parse_amount_with_unit_baekman():
    assert _parse_amount("3백만") == 3e6


def test_parse_amount_with_unit_man():
    assert _parse_amount("1000만") == 1e7


def test_parse_amount_with_won_suffix():
    assert _parse_amount("50,000,000원") == 50_000_000.0


def test_parse_amount_with_decimal():
    assert _parse_amount("1.5억") == 1.5e8


def test_parse_amount_empty_raises():
    with pytest.raises(ValueError):
        _parse_amount("")


# ── 날짜 ──────────────────────────────────────────────────────────────


def test_parse_date_full():
    assert _parse_date("2026-04-15") == date(2026, 4, 15)


def test_parse_date_short_uses_current_year():
    d = _parse_date("04-15")
    assert d.month == 4 and d.day == 15
    assert d.year == date.today().year


def test_parse_date_today_keyword():
    assert _parse_date("오늘") == date.today()


def test_parse_date_today_english():
    assert _parse_date("today") == date.today()


def test_parse_date_invalid_raises():
    with pytest.raises(ValueError):
        _parse_date("not-a-date")


# ── 결합 ──────────────────────────────────────────────────────────────


def test_parse_input_single_line():
    d, amt, note = parse_cash_event_input("2026-04-15 5천만 월급")
    assert d == date(2026, 4, 15)
    assert amt == 5e7
    assert note == "월급"


def test_parse_input_single_line_no_note():
    d, amt, note = parse_cash_event_input("2026-04-15 1000만")
    assert d == date(2026, 4, 15)
    assert amt == 1e7
    assert note == ""


def test_parse_input_multi_line():
    d, amt, note = parse_cash_event_input("오늘\n3억\n자본증가")
    assert d == date.today()
    assert amt == 3e8
    assert note == "자본증가"


def test_parse_input_missing_amount_raises():
    with pytest.raises(ValueError):
        parse_cash_event_input("2026-04-15")
