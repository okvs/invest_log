"""자산그래프 최근 N일 윈도우 — 기간 파싱 + 슬라이스 + 렌더 스모크."""
from __future__ import annotations

from datetime import date, timedelta

from bot.asset_history import _render_window_graph, slice_recent_rows
from bot.handlers.asset_graph import parse_window_days


def _make_rows(n: int, start: date | None = None) -> list[dict]:
    start = start or date(2026, 4, 3)
    rows = []
    for i in range(n):
        rows.append({
            "date": start + timedelta(days=i),
            "realized": i * 1000.0,
            "unrealized": i * 500.0,
            "profit": i * 1500.0,
            "deposits": 0.0,
            "asset": 300_000_000 + i * 1_000_000,
        })
    return rows


# ---------------------------------------------------------------------------
# parse_window_days
# ---------------------------------------------------------------------------

def test_parse_no_arg_returns_none():
    assert parse_window_days("자산그래프") is None


def test_parse_one_month_variants():
    assert parse_window_days("자산그래프 1개월") == 30
    assert parse_window_days("자산그래프 한달") == 30
    assert parse_window_days("자산그래프 한 달") == 30
    assert parse_window_days("자산그래프 1달") == 30


def test_parse_n_months():
    assert parse_window_days("자산그래프 3개월") == 90


def test_parse_n_days():
    assert parse_window_days("자산그래프 30일") == 30
    assert parse_window_days("자산그래프 7일") == 7


def test_parse_full_and_garbage_fall_back_to_none():
    assert parse_window_days("자산그래프 전체") is None
    assert parse_window_days("자산그래프 뭐시기") is None
    assert parse_window_days("") is None


# ---------------------------------------------------------------------------
# slice_recent_rows
# ---------------------------------------------------------------------------

def test_slice_returns_last_n_days():
    rows = _make_rows(60)
    win = slice_recent_rows(rows, 30)
    assert len(win) == 30
    assert win[-1]["date"] == rows[-1]["date"]
    assert win[0]["date"] == rows[-1]["date"] - timedelta(days=29)


def test_slice_days_longer_than_data_returns_all():
    rows = _make_rows(10)
    assert slice_recent_rows(rows, 30) == rows


def test_slice_none_or_zero_returns_all():
    rows = _make_rows(10)
    assert slice_recent_rows(rows, None) == rows
    assert slice_recent_rows(rows, 0) == rows


def test_slice_empty_rows():
    assert slice_recent_rows([], 30) == []


# ---------------------------------------------------------------------------
# _render_window_graph 스모크
# ---------------------------------------------------------------------------

def test_render_window_graph_returns_png():
    rows = _make_rows(30)
    buf = _render_window_graph(rows, 30)
    assert buf is not None
    head = buf.read(8)
    assert head == b"\x89PNG\r\n\x1a\n"


def test_render_window_graph_single_row_returns_none():
    rows = _make_rows(1)
    assert _render_window_graph(rows, 30) is None


def test_render_window_graph_flat_asset_no_crash():
    rows = _make_rows(30)
    for r in rows:
        r["asset"] = 300_000_000  # 변동 0 — y패딩 0 가드
    buf = _render_window_graph(rows, 30)
    assert buf is not None
