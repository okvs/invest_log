"""개별주식선물 만기일 계산.

KRX 개별주식선물은 통상 분기물(3/6/9/12월)이며,
만기일은 결제월의 두 번째 목요일이다.
영업일이 아닌 경우의 보정은 단순화하여 두 번째 목요일을 그대로 사용한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


# 개별주식선물 분기 결제월
QUARTERLY_MONTHS = (3, 6, 9, 12)


def second_thursday(year: int, month: int) -> date:
    """해당 월의 두 번째 목요일 (date)."""
    first = date(year, month, 1)
    # weekday(): 월=0, ..., 목=3
    days_to_thu = (3 - first.weekday()) % 7
    first_thu = first + timedelta(days=days_to_thu)
    return first_thu + timedelta(days=7)


@dataclass(frozen=True)
class FuturesMonth:
    """선물 결제월 정보."""
    contract_month: str   # YYYYMM
    expiry_date: date     # 만기일

    @property
    def expiry_iso(self) -> str:
        return self.expiry_date.isoformat()

    @property
    def year(self) -> int:
        return int(self.contract_month[:4])

    @property
    def month(self) -> int:
        return int(self.contract_month[4:6])

    def days_to_expiry(self, today: date | None = None) -> int:
        today = today or date.today()
        return (self.expiry_date - today).days

    def label(self) -> str:
        return f"{self.year % 100:02d}년 {self.month:02d}월물 (만기 {self.expiry_date:%m/%d})"


def upcoming_quarterly_months(today: date | None = None, count: int = 4) -> list[FuturesMonth]:
    """오늘 기준 만기가 지나지 않은 분기 결제월을 가까운 순으로 count개 반환."""
    today = today or date.today()
    out: list[FuturesMonth] = []
    year = today.year
    while len(out) < count:
        for m in QUARTERLY_MONTHS:
            exp = second_thursday(year, m)
            if exp < today:
                continue
            out.append(FuturesMonth(
                contract_month=f"{year}{m:02d}",
                expiry_date=exp,
            ))
            if len(out) >= count:
                break
        year += 1
    return out


def upcoming_months(today: date | None = None, count: int = 6) -> list[FuturesMonth]:
    """오늘 기준 만기가 지나지 않은 모든 결제월(매월)을 가까운 순으로 count개 반환.

    KRX 개별주식선물은 분기물(3/6/9/12) 외에 차근월 1·2개월물도 거래되므로
    핸들러 키보드에서 7월물 같은 근월물을 보여주려면 이 함수를 쓴다.
    """
    today = today or date.today()
    out: list[FuturesMonth] = []
    year, month = today.year, today.month
    while len(out) < count:
        exp = second_thursday(year, month)
        if exp >= today:
            out.append(FuturesMonth(
                contract_month=f"{year}{month:02d}",
                expiry_date=exp,
            ))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def parse_contract_month(s: str) -> FuturesMonth:
    """'202607' 또는 '2026-07' 같은 입력에서 FuturesMonth 생성.

    분기물만 거래된다는 옛 가정을 풀어서, 1~12월 어떤 결제월이든 허용한다.
    실제 거래되지 않는 월의 만기일은 두 번째 목요일로 계산되지만,
    KRX가 상장하지 않은 종목·월 조합은 시장에서 체결되지 않을 뿐이다.
    """
    s = s.strip().replace("-", "").replace("/", "")
    if len(s) != 6 or not s.isdigit():
        raise ValueError(f"결제월 형식이 올바르지 않습니다: {s}")
    year = int(s[:4])
    month = int(s[4:6])
    if not 1 <= month <= 12:
        raise ValueError(f"결제월이 1~12 범위를 벗어났습니다: {month}월")
    return FuturesMonth(
        contract_month=s,
        expiry_date=second_thursday(year, month),
    )
