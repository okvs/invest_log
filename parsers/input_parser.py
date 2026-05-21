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

    1. 공백 제거
    2. nickname_map에서 대소문자 무시하고 검색
    3. 매칭되면 실제 종목명 반환, 아니면 원본 그대로 반환
    """
    name = name.replace(" ", "")
    if nickname_map:
        name_lower = name.lower()
        for nick, real in nickname_map.items():
            if nick.lower() == name_lower:
                return real.replace(" ", "")
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
    """네이버 금융 검색창 자동완성으로 종목 후보 목록 반환.

    별도 subprocess에서 Playwright를 실행하여 봇 이벤트 루프와 충돌 방지.
    6자리 숫자 코드를 가진 일반 주식만 필터링하여 최대 max_results개 반환.
    """
    import json
    import subprocess
    import sys

    script = _SEARCH_SCRIPT.replace("__QUERY__", query).replace(
        "__MAX__", str(max_results)
    )

    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
        if result.returncode != 0:
            logger.warning("종목 검색 subprocess 실패:\n%s", result.stderr)
            return []

        candidates = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) == 3:
                candidates.append(StockCandidate(parts[0], parts[1], parts[2]))
        return candidates
    except subprocess.TimeoutExpired:
        logger.warning("종목 검색 타임아웃: %s", query)
    except Exception:
        logger.warning("종목 검색 실패: %s", query, exc_info=True)
    return []


# subprocess에서 실행할 Playwright 스크립트
_SEARCH_SCRIPT = r'''
import sys
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

query = "__QUERY__"
max_results = __MAX__

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto("https://finance.naver.com/", timeout=10000)
        search = page.locator("#stock_items")
        search.click()
        search.type(query, delay=30)
        try:
            page.wait_for_selector("ul._resultBox li", timeout=3000)
        except Exception:
            sys.exit(0)
        items = page.query_selector_all("ul._resultBox li")
        count = 0
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
                if code.isdigit() and len(code) == 6:
                    market = "KOSDAQ" if "코스닥" in market_text else "KOSPI"
                    print(f"{name}|{code}|{market}")
                    count += 1
                    if count >= max_results:
                        break
            except Exception:
                continue
    finally:
        browser.close()
'''


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


@dataclass
class BrokerMessage:
    name: str
    quantity: int
    price: float  # 주당 가격
    trade_type: str  # "buy" or "sell"
    broker: str  # "KB" or "신한"


@dataclass
class FuturesBrokerMessage:
    """KB증권 등에서 받은 선물 체결 메시지.

    체결금액 해석 가설 — KB증권 개별주식선물 메시지의 `체결금액`은
    `계약수 × multiplier × 주당가`인 총 체결대금으로 가정한다.
    즉 `price_per_share()`로 단가를 산출한다. 사용자가 다른 의미로
    온다고 알리면 이 함수를 다른 가설로 교체한다.
    """
    name: str
    contract_month: str          # YYYYMM
    multiplier: int
    quantity: int                # 계약수
    raw_amount: float            # 메시지의 체결금액 (해석은 price_per_share)
    trade_type: str              # "buy" | "sell"
    broker: str                  # "KB"

    def price_per_share(self) -> float:
        denom = self.quantity * self.multiplier
        if denom <= 0:
            return 0.0
        return self.raw_amount / denom

    def total_amount(self) -> float:
        return self.raw_amount


def _parse_kb_message(text: str) -> BrokerMessage:
    """KB증권 체결 알림 메시지 파싱. 체결금액은 주당 가격."""
    name_match = re.search(r"■\s*종목명:\s*(.+)", text)
    qty_match = re.search(r"■\s*주문수량:\s*(.+)", text)
    amount_match = re.search(r"■\s*체결금액:\s*(.+)", text)
    type_match = re.search(r"■\s*내용:\s*(.+)", text)

    if not all([name_match, qty_match, amount_match, type_match]):
        raise ValueError("KB증권 메시지 형식을 인식할 수 없습니다.")

    content = type_match.group(1)
    if "매도" in content:
        trade_type = "sell"
    elif "매수" in content:
        trade_type = "buy"
    else:
        raise ValueError(f"체결 유형을 인식할 수 없습니다: {content}")

    name = name_match.group(1).strip()
    quantity = int(_parse_number(qty_match.group(1)))
    price = _parse_number(amount_match.group(1))

    return BrokerMessage(
        name=name, quantity=quantity, price=price,
        trade_type=trade_type, broker="KB",
    )


def _parse_shinhan_message(text: str) -> BrokerMessage:
    """신한증권 체결 알림 메시지 파싱. 체결단가는 주당 가격."""
    name_match = re.search(r"종목명\s*:\s*(.+)", text)
    type_match = re.search(r"체결구분\s*:\s*(.+)", text)
    qty_match = re.search(r"체결수량\s*:\s*(.+)", text)
    price_match = re.search(r"체결단가\s*:\s*(.+)", text)

    if not all([name_match, type_match, qty_match, price_match]):
        raise ValueError("신한증권 메시지 형식을 인식할 수 없습니다.")

    content = type_match.group(1).strip()
    if "매도" in content:
        trade_type = "sell"
    elif "매수" in content:
        trade_type = "buy"
    else:
        raise ValueError(f"체결 유형을 인식할 수 없습니다: {content}")

    name = name_match.group(1).strip()
    quantity = int(_parse_number(qty_match.group(1)))
    price = _parse_number(price_match.group(1))

    return BrokerMessage(
        name=name, quantity=quantity, price=price,
        trade_type=trade_type, broker="신한",
    )


def _is_kb_futures(text: str) -> bool:
    """KB증권 선물옵션 메시지인지 헤더와 종목명 패턴으로 판단."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if "선물옵션" in first_line:
        return True
    if re.search(r"■\s*종목명\s*:.+\sF\s+\d{6}", text):
        return True
    return False


_KB_FUTURES_NAME_RE = re.compile(
    r"■\s*종목명\s*:\s*(.+?)\s+F\s+(\d{6})\s*\(\s*(\d{1,3})\s*\)"
)


def _parse_kb_futures_message(text: str) -> FuturesBrokerMessage:
    """KB증권 선물옵션 체결 메시지 파싱.

    종목명 형식: `<기초자산명> F <YYYYMM> ( <multiplier> )`
    예: `현대모비스 F 202606 (  10)` → name=현대모비스, month=202606, mult=10
    """
    name_match = _KB_FUTURES_NAME_RE.search(text)
    qty_match = re.search(r"■\s*주문수량\s*:\s*([\d,]+)\s*계약", text)
    amount_match = re.search(r"■\s*체결금액\s*:\s*([\d,]+)\s*원", text)
    type_match = re.search(r"■\s*내용\s*:\s*(매수|매도)체결", text)

    if not all([name_match, qty_match, amount_match, type_match]):
        raise ValueError("KB증권 선물옵션 메시지 형식을 인식할 수 없습니다.")

    name = name_match.group(1).strip()
    contract_month = name_match.group(2)
    multiplier = int(name_match.group(3))
    quantity = int(qty_match.group(1).replace(",", ""))
    raw_amount = float(amount_match.group(1).replace(",", ""))
    trade_type = "buy" if type_match.group(1) == "매수" else "sell"

    if quantity <= 0:
        raise ValueError("계약수가 0 이하입니다.")
    if multiplier <= 0:
        raise ValueError("승수가 0 이하입니다.")

    return FuturesBrokerMessage(
        name=name,
        contract_month=contract_month,
        multiplier=multiplier,
        quantity=quantity,
        raw_amount=raw_amount,
        trade_type=trade_type,
        broker="KB",
    )


def parse_broker_message(text: str) -> BrokerMessage | FuturesBrokerMessage:
    """증권사 체결 메시지를 자동 감지하여 파싱.

    KB증권 선물옵션이면 FuturesBrokerMessage,
    KB/신한 현물이면 BrokerMessage 반환.
    """
    stripped = text.strip()
    if stripped.startswith("[KB증권]"):
        if _is_kb_futures(text):
            return _parse_kb_futures_message(text)
        return _parse_kb_message(text)
    if stripped.startswith("계좌명"):
        return _parse_shinhan_message(text)
    raise ValueError("지원하는 증권사 메시지 형식이 아닙니다.")


def _strip_spaces(name: str) -> str:
    """종목명에서 공백을 모두 제거."""
    return name.replace(" ", "")


def parse_buy_input(text: str) -> BuyInput:
    """여러 줄 매수 입력을 파싱.

    3줄: 종목명, 수량, 매수가
    섹터와 매수 근거는 별도 단계에서 처리됩니다.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    if len(lines) < 3:
        raise ValueError(
            "입력이 부족합니다. 다음 형식으로 입력해주세요:\n"
            "종목명\n수량(예: 10주)\n매수가(예: 72000원)"
        )

    name = _strip_spaces(lines[0])
    quantity = int(_parse_number(lines[1]))
    price = _parse_number(lines[2])

    if quantity <= 0:
        raise ValueError("수량은 1 이상이어야 합니다.")
    if price <= 0:
        raise ValueError("매수가는 0보다 커야 합니다.")

    return BuyInput(
        name=name,
        ticker="",  # 핸들러에서 자동 조회
        sector="",
        quantity=quantity,
        price=price,
        thesis="",
        research_notes="",
    )


def parse_sell_input(text: str, name: str = "") -> SellInput:
    """여러 줄 매도 입력을 파싱.

    name이 제공되면 최소 2줄: 수량, 매도가 (사유는 3줄 이후 선택사항)
    name이 없으면 최소 3줄: 종목명, 수량, 매도가 (사유는 4줄 이후 선택사항)
    사유가 비어 있으면 핸들러가 다음 단계로 넘어가 사유를 별도로 받는다.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    if name:
        # 종목이 이미 선택된 경우 — 수량/매도가 2줄만 필수
        if len(lines) < 2:
            raise ValueError(
                "입력이 부족합니다. 다음 형식으로 입력해주세요:\n"
                "수량(예: 5주)\n매도가(예: 85000원)\n(사유는 다음 단계에서 선택/입력)"
            )
        quantity = int(_parse_number(lines[0]))
        price = _parse_number(lines[1])
        sell_reason = "\n".join(lines[2:])
    else:
        # 종목 미선택 — 종목명/수량/매도가 3줄 필수
        if len(lines) < 3:
            raise ValueError(
                "입력이 부족합니다. 다음 형식으로 입력해주세요:\n"
                "종목명\n수량(예: 5주)\n매도가(예: 85000원)\n(사유는 다음 단계에서 선택/입력)"
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
