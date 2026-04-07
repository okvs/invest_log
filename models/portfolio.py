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
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    transaction_ids: list[str] = field(default_factory=list)

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
            "buy_thesis": self.buy_thesis,
            "research_notes": self.research_notes,
            "transaction_ids": self.transaction_ids,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Holding:
        return cls(**data)

    def add_buy(self, price: float, quantity: int, transaction_id: str) -> None:
        """추가 매수 시 평균단가 재계산."""
        new_total = self.total_invested + price * quantity
        new_qty = self.quantity + quantity
        self.avg_price = round(new_total / new_qty)
        self.quantity = new_qty
        self.total_invested = new_total
        self.transaction_ids.append(transaction_id)

    def remove_sell(self, quantity: int) -> None:
        """매도 시 보유량 차감."""
        if quantity > self.quantity:
            raise ValueError(f"보유량({self.quantity})보다 많은 수량({quantity})을 매도할 수 없습니다.")
        self.quantity -= quantity
        self.total_invested = self.avg_price * self.quantity
