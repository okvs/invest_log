from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from filelock import FileLock

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


def load_holdings() -> list[dict]:
    return load(PORTFOLIO_FILE).get("holdings", [])


def save_holdings(holdings: list[dict]) -> None:
    save(PORTFOLIO_FILE, {"holdings": holdings})


def load_transactions() -> list[dict]:
    return load(TRANSACTIONS_FILE).get("transactions", [])


def save_transactions(transactions: list[dict]) -> None:
    save(TRANSACTIONS_FILE, {"transactions": transactions})


def load_retrospectives() -> list[dict]:
    return load(RETROSPECTIVES_FILE).get("retrospectives", [])


def save_retrospectives(retrospectives: list[dict]) -> None:
    save(RETROSPECTIVES_FILE, {"retrospectives": retrospectives})


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
