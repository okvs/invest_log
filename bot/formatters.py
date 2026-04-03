from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import List


def format_number(n: float) -> str:
    """숫자를 천 단위 콤마 포맷."""
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.1f}"


def format_dashboard(holdings: List[dict]) -> str:
    """보유 종목 리스트를 섹터별 대시보드 텍스트로 변환."""
    if not holdings:
        return "보유 종목이 없습니다."

    # 보유량 0인 종목 제외
    active = [h for h in holdings if h.get("quantity", 0) > 0]
    if not active:
        return "보유 종목이 없습니다."

    # 섹터별 그룹핑
    by_sector: dict[str, list[dict]] = defaultdict(list)
    for h in active:
        by_sector[h.get("sector", "기타")].append(h)

    total_invested = sum(h.get("total_invested", 0) for h in active)
    total_count = len(active)
    today = date.today().strftime("%Y.%m.%d")

    lines = [
        f"투자 현황 ({today})",
        "━" * 20,
        f"총 투자금: {format_number(total_invested)}원",
        f"보유 종목: {total_count}개",
        "",
    ]

    for sector, items in by_sector.items():
        lines.append(f"[{sector}]")
        for h in items:
            name = h["name"]
            qty = h["quantity"]
            avg = format_number(h["avg_price"])
            invested = format_number(h["total_invested"])
            thesis = h.get("buy_thesis", "")
            lines.append(f"  {name} | {qty}주 | 평균 {avg}원")
            lines.append(f"  투자금: {invested}원")
            if thesis:
                lines.append(f'  "{thesis}"')
            lines.append("")

    lines.append("━" * 20)
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


def format_buy_result(name: str, sector: str, quantity: int, price: float, thesis: str) -> str:
    """매수 기록 확인 텍스트."""
    total = price * quantity
    return (
        f"매수 기록 완료!\n"
        f"{name} ({sector}) {quantity}주 x {format_number(price)}원 = {format_number(total)}원\n"
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
