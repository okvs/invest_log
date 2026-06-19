"""미국주식 시세 + USD/KRW 환율 — 24시간(정규장 밖 포함) 조회.

yfinance(Yahoo quoteSummary)로 종목별 시세를 가져오되, `marketState` 를 보고
프리장/애프터장/직전 종가 중 **가장 최신 체결가**를 골라 정규장 밖에도 항상
가격을 반환한다(요건: 정규장만이 아니라 24h 조회). 환율은 `KRW=X`.

성능: get_info(quoteSummary)는 종목당 ~0.5s 라 reports/ 파일캐시로 단기 재사용
(시세 TTL 180s · 환율 TTL 600s). 캐시·네트워크 모두 실패하면 직전 캐시로 폴백.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent / "reports"
_QUOTE_CACHE = _CACHE_DIR / ".us_quotes.json"
_FX_CACHE = _CACHE_DIR / ".usdkrw.json"
_QUOTE_TTL = 180   # 초
_FX_TTL = 600      # 초


def _now() -> float:
    return time.time()


def _read_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(path: Path, data: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# USD/KRW 환율
# ---------------------------------------------------------------------------
def fetch_usdkrw() -> float | None:
    """USD/KRW 환율. 캐시(600s)→yfinance(KRW=X)→직전 캐시 폴백."""
    cache = _read_cache(_FX_CACHE)
    if cache.get("rate") and (_now() - cache.get("ts", 0)) < _FX_TTL:
        return float(cache["rate"])
    try:
        import yfinance as yf
        fi = yf.Ticker("KRW=X").fast_info
        rate = fi.get("lastPrice") if hasattr(fi, "get") else getattr(fi, "last_price", None)
        if rate and rate > 0:
            _write_cache(_FX_CACHE, {"rate": float(rate), "ts": _now()})
            return float(rate)
    except Exception as e:  # noqa: BLE001
        logger.warning("USD/KRW 환율 조회 실패: %s", e)
    return float(cache["rate"]) if cache.get("rate") else None


# ---------------------------------------------------------------------------
# 미국주식 시세
# ---------------------------------------------------------------------------
def _pick_price(info: dict) -> tuple[float | None, str]:
    """marketState 로 프리/포스트/정규 중 최신 체결가 선택. (price, source)."""
    state = (info.get("marketState") or "").upper()
    reg = info.get("regularMarketPrice")
    pre = info.get("preMarketPrice")
    post = info.get("postMarketPrice")
    if state in ("PRE", "PREPRE") and pre:
        return float(pre), "pre"
    if state in ("POST", "POSTPOST", "POSTCLOSE", "CLOSED") and post:
        return float(post), "post"
    if reg:
        return float(reg), "reg"
    # 정규가도 없으면 그나마 있는 값
    for v in (post, pre):
        if v:
            return float(v), "fallback"
    return None, "none"


def fetch_us_quotes(tickers: list[str]) -> dict[str, dict]:
    """미국 티커별 시세. {ticker: {price(USD), change_pct, source}} (USD 단위).

    캐시(180s) 히트분은 재사용, 미스분만 yfinance 조회 후 캐시 갱신.
    개별 종목 실패는 건너뛴다(부분 성공 허용).
    """
    tickers = [t for t in dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip())]
    if not tickers:
        return {}

    cache = _read_cache(_QUOTE_CACHE)
    entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
    out: dict[str, dict] = {}
    stale: list[str] = []
    for t in tickers:
        e = entries.get(t)
        if e and (_now() - e.get("ts", 0)) < _QUOTE_TTL and e.get("price"):
            out[t] = {k: e[k] for k in ("price", "change_pct", "source") if k in e}
        else:
            stale.append(t)

    if stale:
        try:
            import yfinance as yf
            for t in stale:
                try:
                    info = yf.Ticker(t).get_info()
                    price, src = _pick_price(info)
                    if price is None:
                        continue
                    prev = info.get("regularMarketPreviousClose")
                    chg = round((price / prev - 1) * 100, 2) if (prev and prev > 0) else None
                    rec = {"price": price, "change_pct": chg, "source": src}
                    out[t] = rec
                    entries[t] = {**rec, "ts": _now()}
                except Exception as e:  # noqa: BLE001
                    logger.warning("미국주식 시세 실패 %s: %s", t, e)
                    # 직전 캐시라도 있으면 사용
                    if entries.get(t, {}).get("price"):
                        old = entries[t]
                        out[t] = {k: old[k] for k in ("price", "change_pct", "source") if k in old}
        except Exception as e:  # noqa: BLE001
            logger.warning("yfinance 로드 실패: %s", e)
        _write_cache(_QUOTE_CACHE, {"entries": entries})

    return out
