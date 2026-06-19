from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class Holding:
    name: str
    sector: str
    buy_date: str
    avg_price: float
    quantity: int
    total_invested: float
    ticker: str = ""
    buy_thesis: str = ""
    research_notes: str = ""
    credit_loan: float = 0.0
    # 통화: "KRW"(국내, 기본) | "USD"(미국, 나무/NH). USD면 avg_price·total_invested 가
    # USD 단위이고, 대시보드/NAV 는 실시간 USD/KRW 로 환산해 합산한다.
    currency: str = "KRW"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    transaction_ids: list[str] = field(default_factory=list)
    # 계좌별 분해(KB/신한). 백데이터에만 기록 — PWA/대시보드는 합산(이 항목 1개)로 표시.
    # 예: [{"account":"KB","quantity":30,"avg_price":2033100,"funding":""},
    #      {"account":"신한","quantity":5,"avg_price":1801000,"funding":"자기융자"}]
    by_account: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "ticker": self.ticker,
            "sector": self.sector,
            "buy_date": self.buy_date,
            "avg_price": self.avg_price,
            "quantity": self.quantity,
            "total_invested": self.total_invested,
            "credit_loan": self.credit_loan,
            "currency": self.currency,
            "buy_thesis": self.buy_thesis,
            "research_notes": self.research_notes,
            "transaction_ids": self.transaction_ids,
            "by_account": self.by_account,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Holding:
        # credit_loan/currency 없는 기존 데이터 하위호환
        if "credit_loan" not in data:
            data = {**data, "credit_loan": 0.0}
        if "currency" not in data:
            data = {**data, "currency": "KRW"}
        return cls(**data)

    def add_buy(
        self, price: float, quantity: int, transaction_id: str,
        margin_ratio: int = 100,
    ) -> None:
        """추가 매수 시 평균단가 재계산."""
        buy_amount = price * quantity
        new_total = self.total_invested + buy_amount
        new_qty = self.quantity + quantity
        self.avg_price = round(new_total / new_qty)
        self.quantity = new_qty
        self.total_invested = new_total
        if margin_ratio < 100:
            self.credit_loan += buy_amount * (1 - margin_ratio / 100)
        self.transaction_ids.append(transaction_id)

    def remove_sell(self, quantity: int) -> float:
        """매도 시 보유량 차감. 비례 상환할 대출금을 반환."""
        if quantity > self.quantity:
            raise ValueError(f"보유량({self.quantity})보다 많은 수량({quantity})을 매도할 수 없습니다.")
        # 대출금 비례 상환
        loan_repay = 0.0
        if self.credit_loan > 0 and self.quantity > 0:
            loan_repay = self.credit_loan * (quantity / self.quantity)
            self.credit_loan -= loan_repay
        self.quantity -= quantity
        self.total_invested = self.avg_price * self.quantity
        return loan_repay
