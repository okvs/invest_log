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


@dataclass
class StockCandidate:
    name: str
    code: str
    market: str


def search_stocks(query: str, max_results: int = 3) -> list[StockCandidate]:
    """네이버 금융 검색창 자동완성으로 종목 후보 목록 반환 (Playwright).

    6자리 숫자 코드를 가진 일반 주식만 필터링하여 최대 max_results개 반환.
    """
    try:
        return _search_naver_playwright(query, max_results)
    except Exception:
        logger.warning("네이버 종목 검색 실패: %s", query, exc_info=True)
    return []


def _search_naver_playwright(query: str, max_results: int = 3) -> list[StockCandidate]:
    """Playwright로 네이버 금융 검색창 자동완성 결과를 파싱."""
    from playwright.sync_api import sync_playwright

    candidates: list[StockCandidate] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto("https://finance.naver.com/", timeout=10000)

            search = page.locator("#stock_items")
            search.click()
            search.type(query, delay=30)

            # 자동완성 결과 대기
            try:
                page.wait_for_selector("ul._resultBox li", timeout=3000)
            except Exception:
                return candidates

            items = page.query_selector_all("ul._resultBox li")
            for item in items:
                try:
                    code_el = item.query_selector("._au_code")
                    name_el = item.query_selector("._au_name")
                    market_el = item.query_selector("._au_market")
                    if not code_el or not name_el:
                        continue

                    code = code_el.inner_text().strip()
                    name = name_el.inner_text().strip()
                    market_text = market_el.inner_text().strip() if market_el else ""

                    # 6자리 숫자 코드 = 일반 주식 (SPAC/ETF 등 제외)
                    if code.isdigit() and len(code) == 6:
                        market = "KOSDAQ" if "코스닥" in market_text else "KOSPI"
                        candidates.append(StockCandidate(name, code, market))
                        if len(candidates) >= max_results:
                            break
                except Exception:
                    continue
        finally:
            browser.close()

    return candidates


def lookup_ticker(name: str, ticker_map: dict[str, str] | None = None) -> str:
    """종목명으로 종목코드(Yahoo Finance 형식)를 조회.

    1. ticker_map 캐시에서 먼저 확인
    2. search_stocks로 검색 후 정확히 일치하는 종목 반환
    조회 실패 시 빈 문자열 반환.
    """
    if ticker_map:
        found = _find_key_casefold(ticker_map, name)
        if found:
            return found

    try:
        candidates = search_stocks(name)
        for c in candidates:
            if c.name == name:
                suffix = ".KQ" if c.market == "KOSDAQ" else ".KS"
                return c.code + suffix
    except Exception:
        logger.warning("종목코드 조회 실패: %s", name, exc_info=True)

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
