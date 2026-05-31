"""피라미딩(이기는 포지션에 추가매수) 기회 감지.

사용자 국룰:
  - 피라미딩 = *수익 중인* 보유 종목이 강하게 치고 올라갈 때 ~1천만원 추가.
  - 트리거(둘 중 하나):
      (a) 신고가  : 종가/고가가 직전 N(기본 20)거래일 최고가를 경신
      (b) 갭상승 강세: 시가가 전일 종가 위(갭업) + 종가 전일대비 ≥ +X%(기본 5%)
    둘 다면 '갭상 신고가'(최강 신호).
  - 조건: 보유 중 + 현재가 ≥ 평단(이익 중).

실데이터 검증: 이 규칙은 사용자가 복기에서 짚은 피라미딩 적기
  삼성전기 5/21·5/22, SK하이닉스 4/8·4/14·4/21·4/27·5/4·5/11·5/21 을 모두 포착한다
  (5/20 삼성전기는 당시 직전 20봉 최고가 아래라 신호 아님 — 데이터 기준).
"""
from __future__ import annotations

import logging

import yfinance as yf

from bot.formatters import _resolve_tickers

logger = logging.getLogger(__name__)

PYRAMID_AMOUNT = 10_000_000      # 국룰: 1회 피라미딩 ≈ 1천만원(스케일 가격우주 기준)
NEW_HIGH_WINDOW = 20             # 신고가 판정 직전 거래일 수(종가 기준)
STRONG_PCT = 4.0                 # 강한 상승봉 최소 전일대비 상승률(%) — 노이즈 제거


def pyramiding_signal(
    opens, highs, closes, i: int,
    window: int = NEW_HIGH_WINDOW, strong_pct: float = STRONG_PCT,
) -> dict | None:
    """i번째 봉이 피라미딩 트리거인지 판정. 아니면 None.

    조건: 종가 전일대비 ≥ strong_pct(%)  AND  (직전 window봉 신고가  OR  갭상승).
    강한 상승봉만 잡아 잔잔한 신고가/음봉 신고가 같은 노이즈를 거른다.

    반환: {"kind", "newhigh", "gapup", "chg", "win_high"}
    kind ∈ {"갭상 신고가", "신고가", "갭상승 강세"}
    """
    if i <= 0:
        return None
    chg = (closes[i] - closes[i - 1]) / closes[i - 1] * 100 if closes[i - 1] else 0.0
    if chg < strong_pct:                      # 강한 상승봉만
        return None
    win_high = max(closes[max(0, i - window):i])   # 종가 신고가(장중 찔러본 건 제외)
    newhigh = closes[i] >= win_high
    gapup = opens[i] > closes[i - 1]
    if not (newhigh or gapup):
        return None
    kind = "갭상 신고가" if (newhigh and gapup) else ("신고가" if newhigh else "갭상승 강세")
    return {"kind": kind, "newhigh": newhigh, "gapup": gapup,
            "chg": chg, "win_high": win_high}


def scan_history(
    ticker: str, avg_price: float, *, start: str = "2026-03-15",
    require_profit: bool = True,
) -> list[dict]:
    """과거 전 구간에서 피라미딩 트리거가 났던 날들을 반환(복기/검증용)."""
    try:
        h = yf.Ticker(ticker).history(start=start, interval="1d")
    except Exception as e:
        logger.warning("%s 일봉 조회 실패: %s", ticker, e)
        return []
    h = h.dropna(subset=["Open", "High", "Low", "Close"])
    if h.empty:
        return []
    idx = [d.strftime("%Y-%m-%d") for d in h.index]
    O, Hi, C = h["Open"].tolist(), h["High"].tolist(), h["Close"].tolist()
    out = []
    for i in range(len(idx)):
        sig = pyramiding_signal(O, Hi, C, i)
        if not sig:
            continue
        if require_profit and avg_price and C[i] < avg_price:
            continue
        out.append({"date": idx[i], "close": C[i], **sig})
    return out


def detect_opportunities(holdings: list[dict]) -> list[dict]:
    """오늘(최신 봉) 기준 피라미딩 기회인 보유 종목 리스트.

    각 항목: name, ticker, kind, chg, cur, avg, qty, pnl_pct,
             suggested_shares(≈1천만/현재가), win_high
    """
    active = [h for h in holdings if h.get("quantity", 0) > 0]
    if not active:
        return []
    name_to_ticker, _ = _resolve_tickers(active)

    out: list[dict] = []
    for h in active:
        name = h["name"]
        ticker = h.get("ticker") or name_to_ticker.get(name, "")
        avg = float(h.get("avg_price", 0) or 0)
        qty = int(h.get("quantity", 0) or 0)
        if not ticker:
            continue
        try:
            hist = yf.Ticker(ticker).history(period="90d", interval="1d")
        except Exception as e:
            logger.warning("%s(%s) 일봉 조회 실패: %s", name, ticker, e)
            continue
        hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
        if len(hist) < 2:
            continue
        O, Hi, C = hist["Open"].tolist(), hist["High"].tolist(), hist["Close"].tolist()
        i = len(C) - 1
        sig = pyramiding_signal(O, Hi, C, i)
        if not sig:
            continue
        cur = C[i]
        if avg > 0 and cur < avg:   # 이익 중인 포지션에만(피라미딩 원칙)
            continue
        pnl_pct = ((cur - avg) / avg * 100) if avg > 0 else 0.0
        out.append({
            "name": name, "ticker": ticker, "kind": sig["kind"],
            "chg": sig["chg"], "cur": cur, "avg": avg, "qty": qty,
            "pnl_pct": pnl_pct, "win_high": sig["win_high"],
            "suggested_shares": max(1, round(PYRAMID_AMOUNT / cur)) if cur > 0 else 0,
        })
    # 강한 신호(갭상 신고가) → 큰 상승률 순
    rank = {"갭상 신고가": 0, "신고가": 1, "갭상승 강세": 2}
    out.sort(key=lambda r: (rank.get(r["kind"], 9), -r["chg"]))
    return out
