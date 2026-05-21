"""선물 거래 입력 파싱.

선물진입 (방향/결제월은 별도 단계로 선택, 종목 선택 후 본문):
  계약수
  진입가
  위탁증거금
  (사유)

선물청산 (포지션 선택 후 본문):
  계약수
  청산가
  (사유)

선물롤오버 (포지션 선택 후 본문):
  당월물 청산가
  차월물 진입가
  추가 위탁증거금  (음수면 환급)
  (사유)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


def _parse_number(text: str, *, allow_negative: bool = False) -> float:
    """'72,000원', '72000', '10계약' 등에서 숫자 추출."""
    cleaned = text.replace(",", "").strip()
    if allow_negative:
        m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    else:
        m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        raise ValueError(f"숫자를 찾을 수 없습니다: {text}")
    return float(m.group(0))


@dataclass
class FuturesEntryInput:
    contracts: int
    price: float
    margin: float
    reason: str = ""


def parse_futures_entry(text: str) -> FuturesEntryInput:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ValueError(
            "입력이 부족합니다:\n"
            "계약수\n진입가\n위탁증거금\n(사유는 다음 단계에서 선택/입력)"
        )
    contracts = int(_parse_number(lines[0]))
    price = _parse_number(lines[1])
    margin = _parse_number(lines[2])
    reason = "\n".join(lines[3:])
    if contracts <= 0:
        raise ValueError("계약수는 1 이상이어야 합니다.")
    if price <= 0:
        raise ValueError("진입가는 0보다 커야 합니다.")
    if margin <= 0:
        raise ValueError("위탁증거금은 0보다 커야 합니다.")
    return FuturesEntryInput(contracts=contracts, price=price, margin=margin, reason=reason)


@dataclass
class FuturesCloseInput:
    contracts: int
    price: float
    reason: str = ""


def parse_futures_close(text: str) -> FuturesCloseInput:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(
            "입력이 부족합니다:\n계약수\n청산가\n(사유는 다음 단계에서 선택/입력)"
        )
    contracts = int(_parse_number(lines[0]))
    price = _parse_number(lines[1])
    reason = "\n".join(lines[2:])
    if contracts <= 0:
        raise ValueError("계약수는 1 이상이어야 합니다.")
    if price <= 0:
        raise ValueError("청산가는 0보다 커야 합니다.")
    return FuturesCloseInput(contracts=contracts, price=price, reason=reason)


@dataclass
class FuturesRollInput:
    close_price: float
    open_price: float
    margin_delta: float   # 차월물에 추가로 들어가는 위탁증거금 (음수면 환급)
    reason: str = ""


def parse_futures_roll(text: str) -> FuturesRollInput:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ValueError(
            "입력이 부족합니다:\n"
            "당월물 청산가\n차월물 진입가\n추가 증거금(환급이면 음수)\n(사유 선택)"
        )
    close_price = _parse_number(lines[0])
    open_price = _parse_number(lines[1])
    margin_delta = _parse_number(lines[2], allow_negative=True)
    reason = "\n".join(lines[3:])
    if close_price <= 0 or open_price <= 0:
        raise ValueError("가격은 0보다 커야 합니다.")
    return FuturesRollInput(
        close_price=close_price,
        open_price=open_price,
        margin_delta=margin_delta,
        reason=reason,
    )
