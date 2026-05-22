from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from filelock import FileLock

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_reason(s: str) -> str:
    """연속된 공백류(NBSP, 탭, 줄바꿈 등)를 단일 공백으로 압축하고 양 끝을 자른다.
    겉보기에 같은 사유가 다른 공백 때문에 중복으로 보이는 것을 막기 위함.
    """
    return _WHITESPACE_RE.sub(" ", s or "").strip()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _path(filename: str) -> Path:
    return DATA_DIR / filename


def _lock_path(filename: str) -> str:
    return str(_path(filename)) + ".lock"


def load(filename: str) -> dict[str, Any]:
    """JSON 파일을 읽어 dict로 반환. 파일이 없으면 빈 dict."""
    _ensure_dir()
    fp = _path(filename)
    if not fp.exists():
        return {}
    with FileLock(_lock_path(filename)):
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)


def save(filename: str, data: dict[str, Any]) -> None:
    """dict를 JSON 파일에 저장."""
    _ensure_dir()
    fp = _path(filename)
    with FileLock(_lock_path(filename)):
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# --- 편의 함수 ---

PORTFOLIO_FILE = "portfolio.json"
TRANSACTIONS_FILE = "transactions.json"
RETROSPECTIVES_FILE = "retrospectives.json"
FUTURES_POSITIONS_FILE = "futures_positions.json"
FUTURES_TRANSACTIONS_FILE = "futures_transactions.json"


def load_holdings() -> list[dict]:
    return load(PORTFOLIO_FILE).get("holdings", [])


def save_holdings(holdings: list[dict]) -> None:
    save(PORTFOLIO_FILE, {"holdings": holdings})


def load_transactions() -> list[dict]:
    return load(TRANSACTIONS_FILE).get("transactions", [])


def save_transactions(transactions: list[dict]) -> None:
    save(TRANSACTIONS_FILE, {"transactions": transactions})


def get_recent_reasons(
    tx_type: str,
    limit: int = 5,
    pinned: list[str] | None = None,
) -> list[str]:
    """전체 거래 중 해당 타입의 최근 고유 사유를 최신순으로 반환.

    tx_type: "buy" → thesis 필드, "sell" → sell_reason 필드.
    pinned: 결과 맨 앞에 항상 포함될 사유(중복 제거됨). 예: ["자동손절"].
    """
    field = "thesis" if tx_type == "buy" else "sell_reason"

    matching = [t for t in load_transactions() if t.get("type") == tx_type]
    matching.sort(key=lambda t: t.get("date", ""), reverse=True)

    # 공백 정규화 후 비교/저장하여 겉보기에 같은 사유는 한 번만 노출.
    seen: set[str] = set()
    result: list[str] = []
    for p in pinned or []:
        norm = _normalize_reason(p)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(norm)
    pinned_count = len(result)
    for t in matching:
        norm = _normalize_reason(t.get(field) or "")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result.append(norm)
        if len(result) >= pinned_count + limit:
            break
    return result


def load_retrospectives() -> list[dict]:
    return load(RETROSPECTIVES_FILE).get("retrospectives", [])


def save_retrospectives(retrospectives: list[dict]) -> None:
    save(RETROSPECTIVES_FILE, {"retrospectives": retrospectives})


# --- 선물 ---

def load_futures_positions() -> list[dict]:
    return load(FUTURES_POSITIONS_FILE).get("positions", [])


def save_futures_positions(positions: list[dict]) -> None:
    save(FUTURES_POSITIONS_FILE, {"positions": positions})


def load_futures_transactions() -> list[dict]:
    return load(FUTURES_TRANSACTIONS_FILE).get("transactions", [])


def save_futures_transactions(transactions: list[dict]) -> None:
    save(FUTURES_TRANSACTIONS_FILE, {"transactions": transactions})


FUTURES_MARGIN_RATES_FILE = "futures_margin_rates.json"
FUTURES_MARGIN_RATE_POOL_FILE = "futures_margin_rate_pool.json"
MARGIN_RATE_POOL_SIZE = 7
DEFAULT_MARGIN_RATE_POOL: list[float] = [
    0.18, 0.30, 0.3285, 0.36, 0.369, 0.40, 0.50
]


def load_futures_margin_rates() -> dict[str, list[float]]:
    """[deprecated] 종목별 최근 사용한 위탁증거금률 — 글로벌 LRU 풀로 대체됨."""
    return load(FUTURES_MARGIN_RATES_FILE).get("rates", {})


def save_futures_margin_rate(name: str, rate: float, *, keep: int = 3) -> None:
    """[deprecated] 종목 진입 시 사용한 rate 저장. 글로벌 LRU 풀로 대체됨."""
    data = load(FUTURES_MARGIN_RATES_FILE)
    rates = data.get("rates", {})
    cur = rates.get(name, [])
    cur = [rate] + [r for r in cur if abs(r - rate) > 1e-6]
    rates[name] = cur[:keep]
    save(FUTURES_MARGIN_RATES_FILE, {"rates": rates})


def load_margin_rate_pool() -> list[float]:
    """글로벌 증거금률 카드 풀 (LRU 정렬, most recent first).

    파일 없으면 DEFAULT_MARGIN_RATE_POOL 그대로 반환 (seed).
    """
    data = load(FUTURES_MARGIN_RATE_POOL_FILE)
    cards = data.get("cards") if isinstance(data, dict) else None
    if not cards:
        return list(DEFAULT_MARGIN_RATE_POOL)
    cards = sorted(cards, key=lambda c: c.get("last_used") or "", reverse=True)
    return [float(c["rate"]) for c in cards if "rate" in c]


def touch_margin_rate_card(
    rate: float, *, max_size: int = MARGIN_RATE_POOL_SIZE,
) -> None:
    """rate 사용 시각 갱신. 새 rate면 추가 + LRU 초과분 evict.

    오래된 카드 (안 쓴 지 오래된) 가 풀에서 떨어져 나감.
    """
    from datetime import datetime
    now_iso = datetime.now().isoformat(timespec="seconds")
    data = load(FUTURES_MARGIN_RATE_POOL_FILE) or {}
    cards = data.get("cards") if isinstance(data, dict) else None
    if not cards:
        # seed — DEFAULT 는 last_used 빈 문자열로 깔아두고, 사용된 카드만 위로 올라옴
        cards = [{"rate": r, "last_used": ""} for r in DEFAULT_MARGIN_RATE_POOL]
    rate_q = round(float(rate), 4)
    found = False
    for c in cards:
        if abs(float(c.get("rate", 0)) - rate_q) < 1e-6:
            c["last_used"] = now_iso
            found = True
            break
    if not found:
        cards.append({"rate": rate_q, "last_used": now_iso})
    cards.sort(key=lambda c: c.get("last_used") or "", reverse=True)
    cards = cards[:max_size]
    save(FUTURES_MARGIN_RATE_POOL_FILE, {"cards": cards})


def get_recent_futures_reasons(
    tx_type: str,
    limit: int = 5,
    pinned: list[str] | None = None,
) -> list[str]:
    """선물 거래 중 해당 타입의 최근 사유를 최신순으로 반환.

    tx_type:
      "open"  → open + roll_open의 thesis 필드
      "close" → close + roll_close의 reason 필드
    """
    if tx_type == "open":
        target_types = {"open", "roll_open"}
        field = "thesis"
    else:
        target_types = {"close", "roll_close"}
        field = "reason"

    matching = [
        t for t in load_futures_transactions() if t.get("type") in target_types
    ]
    matching.sort(key=lambda t: t.get("date", ""), reverse=True)

    seen: set[str] = set()
    result: list[str] = []
    for p in pinned or []:
        norm = _normalize_reason(p)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(norm)
    pinned_count = len(result)
    for t in matching:
        norm = _normalize_reason(t.get(field) or "")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result.append(norm)
        if len(result) >= pinned_count + limit:
            break
    return result


ACCOUNT_FILE = "account.json"


def load_account() -> dict:
    """계좌 정보 로드. {initial_capital, cash, chat_id, last_expiry_alert}"""
    return load(ACCOUNT_FILE)


def save_account(data: dict) -> None:
    """계좌 정보 저장."""
    save(ACCOUNT_FILE, data)


def save_chat_id(chat_id: int) -> None:
    """알림 발송용 텔레그램 chat_id 저장. 이후 JobQueue가 사용."""
    account = load_account()
    if account.get("chat_id") == chat_id:
        return
    account["chat_id"] = chat_id
    save_account(account)


def load_chat_id() -> int | None:
    """저장된 chat_id 반환. 없으면 None."""
    cid = load_account().get("chat_id")
    return int(cid) if cid is not None else None


NICKNAME_MAP_FILE = "nickname_map.json"


def load_nickname_map() -> dict[str, str]:
    """닉네임 → 종목명 매핑 로드."""
    _ensure_dir()
    fp = _path(NICKNAME_MAP_FILE)
    if not fp.exists():
        return {}
    with FileLock(_lock_path(NICKNAME_MAP_FILE)):
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)


def save_nickname_map(nickname_map: dict[str, str]) -> None:
    """닉네임 → 종목명 매핑 저장."""
    _ensure_dir()
    fp = _path(NICKNAME_MAP_FILE)
    with FileLock(_lock_path(NICKNAME_MAP_FILE)):
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(nickname_map, f, ensure_ascii=False, indent=2)


TICKER_MAP_FILE = "ticker_map.json"


def load_ticker_map() -> dict[str, str]:
    """종목명 → 티커코드 매핑 로드."""
    _ensure_dir()
    fp = _path(TICKER_MAP_FILE)
    if not fp.exists():
        return {}
    with FileLock(_lock_path(TICKER_MAP_FILE)):
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)


def save_ticker_map(ticker_map: dict[str, str]) -> None:
    """종목명 → 티커코드 매핑 저장."""
    _ensure_dir()
    fp = _path(TICKER_MAP_FILE)
    with FileLock(_lock_path(TICKER_MAP_FILE)):
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(ticker_map, f, ensure_ascii=False, indent=2)
