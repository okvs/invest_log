"""피라미딩 기회 감지 테스트.

규칙: 종가 전일대비 ≥4% AND (직전 20봉 종가 신고가 OR 갭상승), 보유·수익 중.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from bot.pyramiding import detect_opportunities, pyramiding_signal, scan_history


def test_signal_new_closing_high_strong():
    O = [100, 101, 102, 103]
    Hi = [101, 102, 103, 110]
    C = [100, 101, 102, 109]  # +6.9%, 종가 신고가
    sig = pyramiding_signal(O, Hi, C, 3)
    assert sig and sig["newhigh"] and sig["kind"] in ("신고가", "갭상 신고가")


def test_signal_gap_up_strong_not_newhigh():
    # 직전 고점 아래지만 갭상승 + 강세 → '갭상승 강세'
    O = [100, 130, 120, 118]   # i=3 시가 118 > 전일 종가 110 (갭업)
    Hi = [135, 135, 125, 125]
    C = [100, 128, 110, 117]   # i=3 +6.4%, 직전 종가 신고가는 아님(128>117)
    sig = pyramiding_signal(O, Hi, C, 3)
    assert sig and sig["gapup"] and not sig["newhigh"]
    assert sig["kind"] == "갭상승 강세"


def test_signal_weak_rise_filtered():
    # 신고가지만 +1%대 약한 상승 → 신호 아님(노이즈 제거)
    O = [100, 101, 102, 103]
    Hi = [101, 102, 103, 104]
    C = [100, 101, 102, 103]  # +0.98%
    assert pyramiding_signal(O, Hi, C, 3) is None


def test_signal_down_close_new_intraday_high_filtered():
    # 장중 신고가 찔렀지만 음봉 마감 → 종가 신고가 아님 + 약세 → 신호 아님
    O = [100, 101, 102, 115]
    Hi = [101, 102, 103, 130]  # 장중 130 신고가
    C = [100, 101, 102, 99]    # 종가 99 (하락 마감)
    assert pyramiding_signal(O, Hi, C, 3) is None


def _hist(o, h, l, c):
    idx = pd.date_range("2026-04-01", periods=len(c), freq="D")
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c,
                         "Volume": [1_000_000] * len(c)}, index=idx)


def test_detect_only_when_in_profit():
    o = [100, 101, 102, 103]; h = [101, 102, 103, 112]; l = [99, 100, 101, 102]
    c = [100, 101, 102, 110]  # 오늘 +7.8% 종가 신고가
    holdings = [{"name": "테스트", "ticker": "005930.KS", "quantity": 10, "avg_price": 105}]
    fake = MagicMock(); fake.history.return_value = _hist(o, h, l, c)
    with patch("bot.pyramiding.yf.Ticker", return_value=fake), \
         patch("bot.pyramiding._resolve_tickers", return_value=({"테스트": "005930.KS"}, [])):
        opps = detect_opportunities(holdings)
    assert len(opps) == 1
    o0 = opps[0]
    assert o0["name"] == "테스트"
    assert o0["suggested_shares"] == round(10_000_000 / 110)  # 1천만 / 현재가
    assert o0["pnl_pct"] > 0


def test_detect_skips_loss_position():
    o = [100, 101, 102, 103]; h = [101, 102, 103, 112]; l = [99, 100, 101, 102]
    c = [100, 101, 102, 110]
    holdings = [{"name": "테스트", "ticker": "005930.KS", "quantity": 10, "avg_price": 200}]  # 손실 중
    fake = MagicMock(); fake.history.return_value = _hist(o, h, l, c)
    with patch("bot.pyramiding.yf.Ticker", return_value=fake), \
         patch("bot.pyramiding._resolve_tickers", return_value=({"테스트": "005930.KS"}, [])):
        assert detect_opportunities(holdings) == []


def test_scan_history_collects_trigger_days():
    o = [100, 101, 102, 103, 104]
    h = [101, 102, 103, 104, 120]
    l = [99, 100, 101, 102, 103]
    c = [100, 101, 102, 103, 118]  # 마지막 봉 +14.6% 신고가
    fake = MagicMock(); fake.history.return_value = _hist(o, h, l, c)
    with patch("bot.pyramiding.yf.Ticker", return_value=fake):
        days = scan_history("005930.KS", avg_price=0, require_profit=False)
    assert any(d["chg"] > 10 for d in days)
