"""선물 시세 조회.

전략 (우선순위):
  1. data/futures_quotes.json 에 저장된 최근 수동 시세 (TTL 안 지났을 때)
  2. KIS Open Trading API 개별주식선물 실시간가 (stocks_battle KisClient 재사용)
  3. 기초자산 ticker_map → yfinance 로 기초자산 현재가 (선물가 근사치)
  4. 모두 실패하면 해당 symbol 누락

수동 시세는 '선물시세' 명령으로 사용자가 직접 입력해 보정 가능.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from bot.formatters import fetch_current_quotes
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
    """뒤로호환: {symbol: price} 형태만 필요할 때 thin wrapper."""
    quotes = await fetch_futures_quotes(positions)
    return {sym: q["price"] for sym, q in quotes.items()}


async def fetch_futures_quotes(positions: list[dict]) -> dict[str, dict]:
    """선물 포지션 리스트 → {position_key: {price, change_pct, source}} 매핑.

    position_key: 같은 symbol 의 다른 결제월을 구분하기 위해 "symbol|contract_month" 사용.
    source: "manual" | "kis" | "underlying".
    """
    if not positions:
        return {}

    result: dict[str, dict] = {}

    # 1. 수동 시세 (symbol 단위)
    manual = _read_fresh_manual_quotes()

    # 2. KIS 선물 시세 (symbol+월 단위)
    # 3. 기초자산 폴백을 위한 ticker_map 준비
    ticker_map = load_ticker_map()

    for p in positions:
        sym = p.get("symbol", "")
        cm = p.get("contract_month", "")
        if not sym:
            continue
        key = f"{sym}|{cm}"

        if sym in manual:
            result[key] = {
                "price": manual[sym],
                "change_pct": None,
                "source": "manual",
            }
            continue

        # KIS 실시간 선물가 시도 (실패해도 폴백)
        try:
            kis_quote = await asyncio.to_thread(_kis_quote, sym, cm)
        except Exception:
            kis_quote = None
        if kis_quote:
            result[key] = {
                "price": kis_quote["price"],
                "change_pct": kis_quote.get("change_pct"),
                "source": "kis",
            }
            continue

        # 기초자산 yfinance 폴백
        name = p.get("name", "")
        ticker = ticker_map.get(name, "") or next(
            (v for k, v in ticker_map.items() if k.lower() == name.lower()),
            "",
        )
        if not ticker:
            ticker = f"{sym}.KS"
        try:
            quotes = await asyncio.to_thread(fetch_current_quotes, [ticker])
        except Exception:
            logger.warning("선물 기초자산 시세 조회 실패", exc_info=True)
            quotes = {}
        q = quotes.get(ticker)
        if q:
            result[key] = {
                "price": q["price"],
                "change_pct": q.get("change_pct"),
                "source": "underlying",
            }

    return result


def _kis_quote(symbol: str, contract_month: str):
    """KIS 선물 시세 호출 wrapper (sync) — asyncio.to_thread 용."""
    try:
        from bot.kis_futures import fetch_kis_futures_quote
    except Exception:
        logger.warning("KIS 선물 모듈 로드 실패", exc_info=True)
        return None
    return fetch_kis_futures_quote(symbol, contract_month)
