"""매매 복기 차트/요약 단위 테스트.

- aggregate_trades: 같은 날·같은 방향 분할체결을 VWAP·총수량으로 합산
- summarize_review: 캡션 내용 + Telegram 1024자 제한 + 위치 태그
- build_trade_chart: 티커/거래/시세 누락 시 None, 정상 시 PNG BytesIO
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from bot.trade_review import aggregate_trades, build_trade_chart, summarize_review


def _tx(date, ty, qty, price):
    return {"date": date, "type": ty, "quantity": qty, "price": price}


# ── aggregate_trades ─────────────────────────────────────────────────────────
def test_aggregate_merges_same_day_same_side_vwap():
    txs = [
        _tx("2026-05-19T10:00:00", "buy", 200, 61930),
        _tx("2026-05-19T11:00:00", "buy", 100, 64500),
        _tx("2026-05-19T12:00:00", "buy", 100, 61900),
    ]
    agg = aggregate_trades(txs)
    assert len(agg) == 1
    m = agg[0]
    assert m["type"] == "buy"
    assert m["qty"] == 400
    vwap = (200 * 61930 + 100 * 64500 + 100 * 61900) / 400
    assert abs(m["price"] - vwap) < 1e-6


def test_aggregate_keeps_buy_and_sell_separate_same_day():
    txs = [
        _tx("2026-05-04", "sell", 50, 226500),
        _tx("2026-05-04", "buy", 20, 231500),
    ]
    agg = aggregate_trades(txs)
    assert len(agg) == 2
    # 같은 날은 매수가 먼저
    assert agg[0]["type"] == "buy"
    assert agg[1]["type"] == "sell"


def test_aggregate_orders_by_date():
    txs = [
        _tx("2026-05-19", "buy", 10, 100),
        _tx("2026-04-07", "buy", 10, 90),
        _tx("2026-05-04", "sell", 5, 110),
    ]
    agg = aggregate_trades(txs)
    assert [m["date"] for m in agg] == ["2026-04-07", "2026-05-04", "2026-05-19"]


def test_aggregate_drops_invalid_and_nontrade():
    txs = [
        _tx("2026-05-01", "buy", 0, 100),       # qty 0
        _tx("2026-05-01", "buy", 10, 0),        # price 0
        {"date": "2026-05-01", "type": "open", "quantity": 5, "price": 100},  # 선물류
        _tx("", "buy", 5, 100),                 # 날짜 없음
        _tx("2026-05-02", "buy", 5, 100),       # 유효
    ]
    agg = aggregate_trades(txs)
    assert len(agg) == 1
    assert agg[0]["date"] == "2026-05-02"


# ── summarize_review ─────────────────────────────────────────────────────────
def _holding():
    return {
        "name": "삼성전자", "ticker": "005930.KS", "sector": "반도체",
        "quantity": 110, "avg_price": 286000, "total_invested": 31460000,
    }


def test_summary_has_position_tag_and_pnl():
    txs = [
        _tx("2026-04-07", "buy", 106, 192000),
        _tx("2026-05-26", "sell", 280, 299500),
    ]
    cap = summarize_review(_holding(), txs, cur_price=317000, change_pct=2.9,
                           position=(3, 10))
    assert cap.startswith("[3/10] ")
    assert "삼성전자" in cap and "005930.KS" in cap
    assert "%" in cap  # 손익률 표기
    assert len(cap) <= 1024


def test_summary_without_price_omits_eval_line():
    cap = summarize_review(_holding(), [_tx("2026-04-07", "buy", 110, 286000)],
                           cur_price=None)
    assert "평가" not in cap  # 시세 없으면 평가/손익 줄 생략
    assert "보유 110주" in cap


def test_summary_caption_within_telegram_limit_many_trades():
    txs = [_tx(f"2026-05-{d:02d}", "buy" if d % 2 else "sell", 10, 60000)
           for d in range(1, 29)]
    cap = summarize_review(_holding(), txs, cur_price=60000, change_pct=0.0,
                           position=(1, 10))
    assert len(cap) <= 1024


# ── build_trade_chart ────────────────────────────────────────────────────────
def test_chart_none_without_ticker():
    assert build_trade_chart("삼성전자", "", [_tx("2026-05-01", "buy", 10, 100)], 100) is None


def test_chart_none_without_trades():
    assert build_trade_chart("삼성전자", "005930.KS", [], 100) is None


def _fake_hist(start_close=100.0, days=30):
    idx = pd.date_range("2026-04-01", periods=days, freq="D")
    closes = [start_close + i for i in range(days)]
    return pd.DataFrame({
        "Open": [c - 1 for c in closes],
        "High": [c + 2 for c in closes],
        "Low": [c - 2 for c in closes],
        "Close": closes,
    }, index=idx)


def test_chart_returns_png_with_mocked_history():
    txs = [
        _tx("2026-04-05", "buy", 100, 103),
        _tx("2026-04-20", "sell", 50, 118),
    ]
    fake = MagicMock()
    fake.history.return_value = _fake_hist()
    with patch("bot.trade_review.yf.Ticker", return_value=fake):
        buf = build_trade_chart("테스트", "005930.KS", txs, 105.0, cur_price=128.0)
    assert buf is not None
    data = buf.getvalue()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG 시그니처
    assert len(data) > 1000


def test_chart_none_when_history_empty():
    fake = MagicMock()
    fake.history.return_value = pd.DataFrame()
    with patch("bot.trade_review.yf.Ticker", return_value=fake):
        buf = build_trade_chart("테스트", "005930.KS",
                                [_tx("2026-04-05", "buy", 10, 100)], 100.0)
    assert buf is None


def test_summary_warns_when_no_matching_trades():
    cap = summarize_review(_holding(), [], cur_price=317000, change_pct=1.0)
    assert "거래 기록이 없" in cap  # rename/매칭 실패 시 침묵 대신 경고
    assert "누적 매수" not in cap


def test_chart_survives_nan_rows():
    """yfinance NaN 행이 섞여도 dropna 후 정상 렌더 (set_ylim ValueError 방지)."""
    hist = _fake_hist()
    hist.iloc[3] = float("nan")          # 한 행 통째 NaN
    hist.loc[hist.index[7], "High"] = float("nan")  # 부분 NaN
    fake = MagicMock()
    fake.history.return_value = hist
    with patch("bot.trade_review.yf.Ticker", return_value=fake):
        buf = build_trade_chart("테스트", "005930.KS",
                                [_tx("2026-04-10", "buy", 10, 110)], 110.0, cur_price=128.0)
    assert buf is not None
    assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"


def test_chart_none_when_all_rows_nan():
    hist = _fake_hist()
    hist[["Open", "High", "Low", "Close"]] = float("nan")
    fake = MagicMock()
    fake.history.return_value = hist
    with patch("bot.trade_review.yf.Ticker", return_value=fake):
        buf = build_trade_chart("테스트", "005930.KS",
                                [_tx("2026-04-10", "buy", 10, 110)], 110.0)
    assert buf is None


def test_chart_handles_future_dated_trade():
    """당일 봉 미게시 등으로 거래일이 마지막 캔들보다 늦어도 예외 없이 렌더."""
    fake = MagicMock()
    fake.history.return_value = _fake_hist(days=20)  # ~2026-04-20 까지
    with patch("bot.trade_review.yf.Ticker", return_value=fake):
        buf = build_trade_chart("테스트", "005930.KS",
                                [_tx("2026-12-31", "buy", 10, 120)], 120.0, cur_price=125.0)
    assert buf is not None  # 미래일 거래도 우측 여백에 분리 표시, 크래시 없음


def test_chart_handles_trade_on_nontrading_day():
    """거래일이 캔들 인덱스에 없을 때(주말 등) 예외 없이 직전 거래일로 스냅."""
    idx = pd.to_datetime(["2026-04-01", "2026-04-02", "2026-04-03",
                          "2026-04-06", "2026-04-07"])  # 4/4~4/5 주말 갭
    hist = pd.DataFrame({
        "Open": [100, 101, 102, 103, 104],
        "High": [102, 103, 104, 105, 106],
        "Low": [98, 99, 100, 101, 102],
        "Close": [101, 102, 103, 104, 105],
    }, index=idx)
    fake = MagicMock()
    fake.history.return_value = hist
    with patch("bot.trade_review.yf.Ticker", return_value=fake):
        buf = build_trade_chart("테스트", "005930.KS",
                                [_tx("2026-04-04", "buy", 10, 102)], 102.0)
    assert buf is not None  # 4/4(주말)도 예외 없이 처리
