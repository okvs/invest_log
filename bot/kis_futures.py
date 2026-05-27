"""KIS Open Trading API 기반 개별주식선물 현재가 조회.

stocks_battle/broker/kis.py 의 KisClient 를 재사용하고,
KIS 마스터 파일(fo_stk_code_mts.mst)로 (기초자산 종목코드 + 결제월) →
KIS 선물 단축코드를 매핑한다.

기본은 실전 키(KIS_REAL_*). 모의 키로 테스트하려면 KIS_PAPER=1 export.
"""
from __future__ import annotations

import io
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_INVEST_LOG_ROOT = Path(__file__).resolve().parent.parent
_STOCKS_BATTLE_ROOT = Path(
    os.environ.get(
        "STOCKS_BATTLE_DIR",
        str(_INVEST_LOG_ROOT.parent / "stocks_battle"),
    )
)

# KIS 마스터 파일 (개별주식선물 종목코드)
MASTER_URL = "https://new.real.download.dws.co.kr/common/master/fo_stk_code_mts.mst.zip"
MASTER_CACHE = _INVEST_LOG_ROOT / "data" / "kis_fo_stk_code.mst"
MASTER_TTL = 24 * 60 * 60  # 하루

# 시세 캐시 (5분) — 같은 호출이 짧은 간격으로 반복될 때 KIS 한도 절약
_PRICE_CACHE: dict[str, tuple[dict, float]] = {}
PRICE_CACHE_TTL = 5 * 60

# KIS 초당 거래건수(EGW00201) 회피용 스로틀 — 연속 호출 사이 최소 간격.
# KIS_QUOTE_MIN_INTERVAL 환경변수로 조정 가능(초).
_MIN_CALL_INTERVAL = float(os.environ.get("KIS_QUOTE_MIN_INTERVAL", "0.35"))
_RATE_LIMIT_RETRIES = 3   # 레이트리밋 시 재시도 횟수
_RATE_LIMIT_BACKOFF = 0.6  # 첫 백오프(초), 시도마다 2배
_throttle_lock = threading.Lock()
_last_call_ts = 0.0


def _throttle() -> None:
    """직전 KIS 호출과 최소 간격을 보장 (스레드 안전)."""
    global _last_call_ts
    with _throttle_lock:
        wait = _MIN_CALL_INTERVAL - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc)
    return "EGW00201" in msg or "초당 거래건수" in msg


def _ensure_stocks_battle_on_path() -> None:
    p = str(_STOCKS_BATTLE_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)
    # stocks_battle/.env 자동 로드 (KIS 키)
    env_path = _STOCKS_BATTLE_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _download_master() -> None:
    """KIS 선물 종목 마스터를 받아 data/kis_fo_stk_code.mst 로 저장."""
    MASTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    ssl_ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(MASTER_URL, context=ssl_ctx, timeout=30) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # 보통 'fo_stk_code_mts.mst' 한 개 — 첫 항목 추출
        name = zf.namelist()[0]
        with zf.open(name) as src:
            MASTER_CACHE.write_bytes(src.read())


def _master_fresh() -> bool:
    if not MASTER_CACHE.exists():
        return False
    age = time.time() - MASTER_CACHE.stat().st_mtime
    return age < MASTER_TTL


def _read_master_rows() -> list[list[str]]:
    """마스터를 cp949 + '|' 로 파싱해 행 리스트 반환. 필요 시 다운로드."""
    if not _master_fresh():
        try:
            _download_master()
        except Exception:
            logger.warning("KIS 선물 마스터 다운로드 실패", exc_info=True)
            if not MASTER_CACHE.exists():
                return []
    try:
        text = MASTER_CACHE.read_bytes().decode("cp949", errors="replace")
    except Exception:
        return []
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 9:
            rows.append(parts)
    return rows


# 컬럼 인덱스 — fo_stk_code_mts.mst 헤더 기준
# 0: info_type(1코스피선물 2코스피SP 3코스닥선물 4코스닥SP 5콜 6풋)
# 1: 단축코드  2: 표준코드  3: 한글종목명  4: ATM구분  5: 행사가
# 6: 월물구분코드  7: 기초자산 단축코드  8: 기초자산 명


def lookup_futures_code(symbol: str, contract_month: str) -> Optional[str]:
    """기초자산 종목코드 + 결제월(YYYYMM) → KIS 선물 단축코드.

    예: ("005930", "202606") → "A11606"
    SP/옵션 행은 제외. 매칭 실패 시 None.
    """
    if not symbol or not contract_month or len(contract_month) != 6:
        return None
    rows = _read_master_rows()
    if not rows:
        return None

    sym = symbol.zfill(6)
    # 한글종목명에 "F YYYYMM" 패턴이 들어 있어야 함
    pat = re.compile(rf"F\s*{re.escape(contract_month)}\b")

    for r in rows:
        info_type = r[0].strip()
        # 1: 코스피 주식선물, 3: 코스닥 주식선물. SP/옵션은 제외.
        if info_type not in ("1", "3"):
            continue
        if r[7].strip() != sym:
            continue
        if pat.search(r[3]):
            return r[1].strip()
    return None


_CLIENT = None
_client_lock = threading.Lock()


def _build_client():
    """stocks_battle 의 KisClient 인스턴스(메모이즈). 환경변수에 따라 실전/모의 자동 선택.

    호출마다 새로 만들면 토큰 재발급 등 부가 요청이 늘어 KIS 한도를 더 빨리
    소진하므로 프로세스당 1개만 만들어 재사용한다.
    """
    global _CLIENT
    with _client_lock:
        if _CLIENT is None:
            _ensure_stocks_battle_on_path()
            from broker.kis import KisClient  # type: ignore  # noqa: E402
            _CLIENT = KisClient()
        return _CLIENT


def _parse_float(s) -> Optional[float]:
    try:
        v = float(str(s).strip())
        return v
    except (TypeError, ValueError):
        return None


def fetch_kis_futures_quote(
    symbol: str, contract_month: str, *, force_refresh: bool = False,
) -> Optional[dict]:
    """단일 포지션 KIS 선물 시세.

    반환 dict: {"price": float, "change_pct": float|None, "prev_close": float|None,
                "short_code": str}.
    매핑/조회 실패 시 None.
    """
    code = lookup_futures_code(symbol, contract_month)
    if not code:
        return None

    cache_key = code
    if not force_refresh:
        cached = _PRICE_CACHE.get(cache_key)
        if cached and time.time() - cached[1] < PRICE_CACHE_TTL:
            return cached[0]

    output = None
    backoff = _RATE_LIMIT_BACKOFF
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        try:
            client = _build_client()
            _throttle()
            # 개별주식선물 시장구분 = JF (지수선물은 F, 지수옵션은 O)
            output = client.futures_current_price(code, mrkt_div="JF")
            break
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < _RATE_LIMIT_RETRIES:
                logger.info("KIS 레이트리밋 — %.1fs 후 재시도 (%s %s, %d/%d)",
                            backoff, symbol, contract_month,
                            attempt + 1, _RATE_LIMIT_RETRIES)
                time.sleep(backoff)
                backoff *= 2
                continue
            logger.warning("KIS 선물 시세 조회 실패 (%s %s → %s): %s",
                           symbol, contract_month, code, e)
            return None

    if not output:
        return None

    price = _parse_float(output.get("futs_prpr"))
    if price is None or price <= 0:
        return None

    change_pct = _parse_float(output.get("futs_prdy_ctrt"))
    # KIS 응답의 부호는 별도 필드(prdy_vrss_sign: 1상한 2상승 3보합 4하한 5하락)
    sign = (output.get("prdy_vrss_sign") or "").strip()
    if change_pct is not None and sign in ("4", "5"):
        # 하한·하락이면 음수
        change_pct = -abs(change_pct)
    elif change_pct is not None:
        change_pct = abs(change_pct)

    prev_close = _parse_float(output.get("futs_prdy_clpr"))

    result = {
        "price": price,
        "change_pct": change_pct,
        "prev_close": prev_close,
        "short_code": code,
    }
    _PRICE_CACHE[cache_key] = (result, time.time())
    return result
