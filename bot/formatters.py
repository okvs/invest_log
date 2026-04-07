from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from typing import Dict, List

import yfinance as yf

from storage.json_store import load_ticker_map

logger = logging.getLogger(__name__)


def format_number(n: float) -> str:
    """숫자를 천 단위 콤마 포맷."""
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.1f}"


def fetch_current_prices(tickers: List[str]) -> Dict[str, float]:
    """yfinance로 현재가 일괄 조회."""
    prices = {}
    if not tickers:
        return prices
    try:
        data = yf.download(tickers, period="1d", progress=False)
        if data.empty:
            return prices
        close = data["Close"]
        if len(tickers) == 1:
            val = close.iloc[-1]
            # 단일 종목: Series가 반환될 수 있음
            if hasattr(val, 'item'):
                val = val.item()
            if val == val:  # NaN 체크
                prices[tickers[0]] = float(val)
        else:
            for t in tickers:
                if t in close.columns:
                    val = close[t].iloc[-1]
                    if hasattr(val, 'item'):
                        val = val.item()
                    if val == val:
                        prices[t] = float(val)
    except Exception as e:
        logger.warning(f"현재가 조회 실패: {e}")
    return prices


def _resolve_tickers(holdings: List[dict]) -> tuple[Dict[str, str], List[str]]:
    """종목명 → 티커코드 매핑. 매핑 안 되는 종목명 리스트도 반환."""
    ticker_map = load_ticker_map()
    result = {}
    missing = []
    for h in holdings:
        name = h["name"]
        ticker = h.get("ticker", "") or ticker_map.get(name, "")
        if ticker:
            result[name] = ticker
        else:
            missing.append(name)
    return result, missing


def format_dashboard(holdings: List[dict]) -> str:
    """보유 종목 리스트를 섹터별 대시보드 텍스트로 변환."""
    if not holdings:
        return "보유 종목이 없습니다."

    active = [h for h in holdings if h.get("quantity", 0) > 0]
    if not active:
        return "보유 종목이 없습니다."

    # 종목명 → 티커 매핑
    name_to_ticker, missing_tickers = _resolve_tickers(active)
    tickers = list(set(name_to_ticker.values()))

    # 현재가 조회
    current_prices = fetch_current_prices(tickers) if tickers else {}

    # 섹터별 그룹핑
    by_sector: dict[str, list[dict]] = defaultdict(list)
    for h in active:
        by_sector[h.get("sector", "기타")].append(h)

    total_invested = sum(h.get("total_invested", 0) for h in active)
    total_count = len(active)
    today = date.today().strftime("%Y.%m.%d")

    # 총 평가금액 계산
    total_eval = 0
    has_prices = False
    for h in active:
        ticker = name_to_ticker.get(h["name"], "")
        if ticker in current_prices:
            total_eval += current_prices[ticker] * h["quantity"]
            has_prices = True
        else:
            total_eval += h.get("total_invested", 0)

    lines = [
        f"투자 현황 ({today})",
        "━" * 20,
        f"총 투자금: {format_number(total_invested)}원",
    ]

    if has_prices:
        total_pnl = total_eval - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"총 평가금: {format_number(total_eval)}원")
        lines.append(f"총 수익: {sign}{format_number(total_pnl)}원 ({sign}{total_pnl_pct:.1f}%)")

    lines.append(f"보유 종목: {total_count}개")
    lines.append("")

    for sector, items in by_sector.items():
        lines.append(f"[{sector}]")
        for h in items:
            name = h["name"]
            qty = h["quantity"]
            avg = format_number(h["avg_price"])
            invested = format_number(h["total_invested"])
            ticker = name_to_ticker.get(name, "")

            lines.append(f"  {name} | {qty}주 | 평균 {avg}원")

            if ticker in current_prices:
                cur = current_prices[ticker]
                eval_amt = cur * qty
                pnl = eval_amt - h["total_invested"]
                pnl_pct = (pnl / h["total_invested"] * 100) if h["total_invested"] else 0
                s = "+" if pnl >= 0 else ""
                lines.append(f"  현재가: {format_number(cur)}원 → 평가: {format_number(eval_amt)}원")
                lines.append(f"  수익: {s}{format_number(pnl)}원 ({s}{pnl_pct:.1f}%)")
            else:
                lines.append(f"  투자금: {invested}원")

            thesis = h.get("buy_thesis", "")
            if thesis:
                lines.append(f'  "{thesis}"')
            lines.append("")

    lines.append("━" * 20)

    if missing_tickers:
        lines.append("")
        lines.append("⚠ 종목코드 미등록 (현재가 조회 불가):")
        for name in missing_tickers:
            lines.append(f"  - {name}")
        lines.append("매수 시 종목코드를 입력하면 자동 등록됩니다.")

    return "\n".join(lines)


def format_sell_result(
    name: str,
    quantity: int,
    price: float,
    total: float,
    profit_loss: float,
    profit_loss_pct: float,
) -> str:
    """매도 결과 텍스트."""
    sign = "+" if profit_loss >= 0 else ""
    return (
        f"매도 기록 완료!\n"
        f"{name} {quantity}주 x {format_number(price)}원 = {format_number(total)}원\n"
        f"수익: {sign}{format_number(profit_loss)}원 ({sign}{profit_loss_pct:.1f}%)"
    )


def format_buy_result(
    name: str, sector: str, quantity: int, price: float, thesis: str,
    ticker: str = "",
) -> str:
    """매수 기록 확인 텍스트."""
    total = price * quantity
    return (
        f"매수 기록 완료!\n"
        f"{name}{ticker} ({sector}) {quantity}주 x {format_number(price)}원 = {format_number(total)}원\n"
        f'근거: "{thesis}"'
    )


def format_buy_preview(name: str, sector: str, quantity: int, price: float, thesis: str, notes: str) -> str:
    """매수 확인 전 미리보기."""
    total = price * quantity
    lines = [
        "매수 정보를 확인해주세요:",
        f"  종목: {name}",
        f"  섹터: {sector}",
        f"  수량: {quantity}주",
        f"  매수가: {format_number(price)}원",
        f"  총 금액: {format_number(total)}원",
        f'  매수 근거: "{thesis}"',
    ]
    if notes:
        lines.append(f'  참고 자료: "{notes}"')
    return "\n".join(lines)
