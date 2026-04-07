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

import logging
import re
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


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


def resolve_name(name: str, nickname_map: dict[str, str] | None = None) -> str:
    """닉네임/대소문자 변환을 거쳐 실제 종목명을 반환.

    1. nickname_map에서 대소문자 무시하고 검색
    2. 매칭되면 실제 종목명 반환, 아니면 원본 그대로 반환
    """
    if nickname_map:
        name_lower = name.lower()
        for nick, real in nickname_map.items():
            if nick.lower() == name_lower:
                return real
    return name


def _find_key_casefold(d: dict[str, str], key: str) -> str | None:
    """dict에서 대소문자 무시하고 key를 찾아 value 반환."""
    key_lower = key.lower()
    for k, v in d.items():
        if k.lower() == key_lower:
            return v
    return None


def _lookup_naver(name: str) -> str:
    """네이버 금융 자동완성 API로 종목코드 조회."""
    import json
    import urllib.parse
    import urllib.request

    query = urllib.parse.quote(name)
    url = (
        f"https://ac.finance.naver.com/ac"
        f"?q={query}&q_enc=utf-8&t_koreng=1&st=111&r_lt=111"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urllib.request.urlopen(req, timeout=5)
    data = json.loads(resp.read().decode("utf-8"))
    logger.debug("네이버 AC 응답: %s", data)

    # items 구조: [[그룹1], [그룹2], ...] — 첫 그룹이 주식
    items = data.get("items", [])
    if not items or not items[0]:
        return ""

    for entry in items[0]:
        # entry 내부 구조가 다를 수 있으므로 방어적으로 파싱
        try:
            row = entry if isinstance(entry[0], str) else entry[0]
            stock_name = row[0]
            stock_code = row[1]
            if stock_name == name and stock_code.isdigit():
                market_info = row[2] if len(row) > 2 else ""
                if "코스닥" in market_info:
                    return stock_code + ".KQ"
                return stock_code + ".KS"
        except (IndexError, TypeError):
            continue
    return ""


def _lookup_krx(name: str) -> str:
    """KRX 종목 검색 API로 종목코드 조회."""
    import json
    import urllib.request

    url = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
    params = (
        f"bld=dbms/comm/finder/finder_stkisu&mktsel=ALL&searchText={name}"
    )
    req = urllib.request.Request(
        url,
        data=params.encode("utf-8"),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = urllib.request.urlopen(req, timeout=5)
    data = json.loads(resp.read().decode("utf-8"))
    logger.debug("KRX 응답: %s", data)

    for item in data.get("block1", []):
        if item.get("codeName", "").strip() == name:
            full_code = item.get("full_code", "")
            short_code = item.get("short_code", "")
            code = short_code or full_code
            if not code:
                continue
            mkt = item.get("marketName", "")
            if "코스닥" in mkt:
                return code + ".KQ"
            return code + ".KS"
    return ""


def lookup_ticker(name: str, ticker_map: dict[str, str] | None = None) -> str:
    """종목명으로 종목코드(Yahoo Finance 형식)를 조회.

    1. ticker_map 캐시에서 먼저 확인
    2. 네이버 금융 자동완성 API
    3. KRX 종목 검색 API
    조회 실패 시 빈 문자열 반환.
    """
    if ticker_map:
        found = _find_key_casefold(ticker_map, name)
        if found:
            return found

    for method in [_lookup_naver, _lookup_krx]:
        try:
            result = method(name)
            if result:
                return result
        except Exception:
            logger.warning(
                "%s 종목코드 조회 실패: %s",
                method.__name__,
                name,
                exc_info=True,
            )

    return ""


def parse_buy_input(text: str) -> BuyInput:
    """여러 줄 매수 입력을 파싱.

    최소 5줄: 종목명, 섹터, 수량, 매수가, 매수근거
    6줄 이상이면 마지막 줄은 참고자료.
    종목코드는 자동으로 조회됩니다.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    if len(lines) < 5:
        raise ValueError(
            "입력이 부족합니다. 다음 형식으로 입력해주세요:\n"
            "종목명\n섹터\n수량(예: 10주)\n매수가(예: 72000원)\n매수 근거"
        )

    name = lines[0]
    sector = lines[1]
    quantity = int(_parse_number(lines[2]))
    price = _parse_number(lines[3])
    thesis = lines[4]
    research_notes = "\n".join(lines[5:]) if len(lines) > 5 else ""

    if quantity <= 0:
        raise ValueError("수량은 1 이상이어야 합니다.")
    if price <= 0:
        raise ValueError("매수가는 0보다 커야 합니다.")

    return BuyInput(
        name=name,
        ticker="",  # 핸들러에서 자동 조회
        sector=sector,
        quantity=quantity,
        price=price,
        thesis=thesis,
        research_notes=research_notes,
    )


def parse_sell_input(text: str, name: str = "") -> SellInput:
    """여러 줄 매도 입력을 파싱.

    name이 제공되면 3줄: 수량, 매도가, 매도사유
    name이 없으면 4줄: 종목명, 수량, 매도가, 매도사유
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    if name:
        # 종목이 이미 선택된 경우 — 3줄만 필요
        if len(lines) < 3:
            raise ValueError(
                "입력이 부족합니다. 다음 형식으로 입력해주세요:\n"
                "수량(예: 5주)\n매도가(예: 85000원)\n매도 사유"
            )
        quantity = int(_parse_number(lines[0]))
        price = _parse_number(lines[1])
        sell_reason = "\n".join(lines[2:])
    else:
        # 종목 미선택 — 4줄 필요
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
