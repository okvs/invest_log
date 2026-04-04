"""한국어 매수/매도 입력 파싱.

매수 입력 (여러 줄):
  삼성전자
  반도체
  10주
  72000원
  AI 수요 증가 전망

매도 입력 (여러 줄):
  삼성전자
  5주
  85000원
  목표가 도달
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class BuyInput:
    name: str
    ticker: str
    sector: str
    quantity: int
    price: float
    thesis: str
    research_notes: str = ""


@dataclass
class SellInput:
    name: str
    quantity: int
    price: float
    sell_reason: str


def _parse_number(text: str) -> float:
    """'72,000원', '72000', '10주' 등에서 숫자 추출."""
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not cleaned:
        raise ValueError(f"숫자를 찾을 수 없습니다: {text}")
    return float(cleaned)


def parse_buy_input(text: str) -> BuyInput:
    """여러 줄 매수 입력을 파싱.

    최소 6줄: 종목명, 종목코드, 섹터, 수량, 매수가, 매수근거
    7줄 이상이면 마지막 줄은 참고자료.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    if len(lines) < 6:
        raise ValueError(
            "입력이 부족합니다. 다음 형식으로 입력해주세요:\n"
            "종목명\n종목코드(예: 005930)\n섹터\n수량(예: 10주)\n매수가(예: 72000원)\n매수 근거"
        )

    name = lines[0]
    ticker = lines[1].strip()
    sector = lines[2]
    quantity = int(_parse_number(lines[3]))
    price = _parse_number(lines[4])
    thesis = lines[5]
    research_notes = "\n".join(lines[6:]) if len(lines) > 6 else ""

    if quantity <= 0:
        raise ValueError("수량은 1 이상이어야 합니다.")
    if price <= 0:
        raise ValueError("매수가는 0보다 커야 합니다.")

    # 종목코드에 .KS/.KQ 접미사 없으면 .KS 추가 (KOSPI 기본)
    if not ticker.endswith((".KS", ".KQ")):
        ticker = ticker + ".KS"

    return BuyInput(
        name=name,
        ticker=ticker,
        sector=sector,
        quantity=quantity,
        price=price,
        thesis=thesis,
        research_notes=research_notes,
    )


def parse_sell_input(text: str) -> SellInput:
    """여러 줄 매도 입력을 파싱.

    최소 4줄: 종목명, 수량, 매도가, 매도사유
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    if len(lines) < 4:
        raise ValueError(
            "입력이 부족합니다. 다음 형식으로 입력해주세요:\n"
            "종목명\n수량(예: 5주)\n매도가(예: 85000원)\n매도 사유"
        )

    name = lines[0]
    quantity = int(_parse_number(lines[1]))
    price = _parse_number(lines[2])
    sell_reason = "\n".join(lines[3:])

    if quantity <= 0:
        raise ValueError("수량은 1 이상이어야 합니다.")
    if price <= 0:
        raise ValueError("매도가는 0보다 커야 합니다.")

    return SellInput(
        name=name,
        quantity=quantity,
        price=price,
        sell_reason=sell_reason,
    )
