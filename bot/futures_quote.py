"""선물 시세 조회.

전략:
  1. data/futures_quotes.json 에 저장된 최근 수동 시세 우선 (만료 안 됨)
  2. 기초자산 ticker_map → yfinance 로 기초자산 현재가 조회 (선물가 근사치)
  3. 위 둘 다 실패하면 해당 symbol 누락

개별주식선물 시세는 pykrx도 KRX 로그인을 요구하기 때문에,
정확한 선물가는 사용자가 '선물시세' 명령으로 직접 입력해 보정한다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from bot.formatters import fetch_current_prices
from storage.json_store import (
    DATA_DIR,
    load_ticker_map,
)
from storage.json_store import load as _load_json
from storage.json_store import save as _save_json

logger = logging.getLogger(__name__)

QUOTES_FILE = "futures_quotes.json"
QUOTE_TTL_SECONDS = 6 * 60 * 60  # 6시간 동안만 수동 시세를 유효 시세로 사용


def _load_quotes() -> dict[str, dict]:
    """{symbol: {"price": float, "ts": epoch}}"""
    data = _load_json(QUOTES_FILE)
    return data.get("quotes", {})


def _save_quotes(quotes: dict[str, dict]) -> None:
    _save_json(QUOTES_FILE, {"quotes": quotes})


def set_manual_quote(symbol: str, price: float) -> None:
    """수동 시세 입력 — Phase 4 '선물시세' 명령에서 호출."""
    if not symbol:
        return
    quotes = _load_quotes()
    quotes[symbol] = {"price": float(price), "ts": time.time()}
    _save_quotes(quotes)


def _read_fresh_manual_quotes() -> dict[str, float]:
    """만료되지 않은 수동 시세만 반환."""
    quotes = _load_quotes()
    now = time.time()
    out: dict[str, float] = {}
    changed = False
    for symbol, item in list(quotes.items()):
        ts = item.get("ts", 0)
        if now - ts > QUOTE_TTL_SECONDS:
            # 만료 항목 정리
            quotes.pop(symbol, None)
            changed = True
            continue
        out[symbol] = float(item.get("price", 0))
    if changed:
        _save_quotes(quotes)
    return out


async def fetch_futures_prices(positions: list[dict]) -> dict[str, float]:
    """선물 포지션 리스트에 대해 {symbol: price} 매핑 반환.

    수동 시세 우선, 없으면 yfinance 기초자산 현재가로 근사.
    """
    if not positions:
        return {}

    symbols = {p.get("symbol", "") for p in positions if p.get("symbol")}
    if not symbols:
        return {}

    manual = _read_fresh_manual_quotes()
    result = {s: manual[s] for s in symbols if s in manual}

    missing = [s for s in symbols if s not in result]
    if not missing:
        return result

    # 기초자산 ticker_map (예: "005930" 자체 또는 "005930.KS")
    ticker_map = load_ticker_map()
    name_to_ticker = {}
    # 포지션의 symbol → ticker (suffix 포함) 매핑 추정
    symbol_to_ticker: dict[str, str] = {}
    for p in positions:
        sym = p.get("symbol", "")
        if not sym or sym in result:
            continue
        name = p.get("name", "")
        # 우선 ticker_map에서 name으로 찾기
        ticker = ticker_map.get(name, "") or next(
            (v for k, v in ticker_map.items() if k.lower() == name.lower()),
            "",
        )
        if not ticker:
            # 폴백: 모르겠으면 .KS suffix 가정 (코스피)
            ticker = f"{sym}.KS"
        symbol_to_ticker[sym] = ticker

    if not symbol_to_ticker:
        return result

    tickers = list(set(symbol_to_ticker.values()))
    try:
        prices = await asyncio.to_thread(fetch_current_prices, tickers)
    except Exception:
        logger.warning("선물 기초자산 시세 조회 실패", exc_info=True)
        prices = {}

    for symbol, ticker in symbol_to_ticker.items():
        if ticker in prices:
            result[symbol] = prices[ticker]

    return result
