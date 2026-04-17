from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Transaction:
    type: str  # "buy" or "sell"
    name: str
    price: float
    quantity: int
    total_amount: float
    sector: str = ""
    date: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    thesis: str = ""
    research_notes: str = ""
    margin_ratio: int = 100  # 증거금비율 (100=현금, 40/50/60=신용)
    # sell 전용
    profit_loss: float = 0.0
    profit_loss_pct: float = 0.0
    sell_reason: str = ""
    holding_id: str = ""
    retrospective_id: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "sector": self.sector,
            "date": self.date,
            "price": self.price,
            "quantity": self.quantity,
            "total_amount": self.total_amount,
        }
        if self.type == "buy":
            d["thesis"] = self.thesis
            d["research_notes"] = self.research_notes
            d["margin_ratio"] = self.margin_ratio
        else:
            d["profit_loss"] = self.profit_loss
            d["profit_loss_pct"] = self.profit_loss_pct
            d["sell_reason"] = self.sell_reason
            d["holding_id"] = self.holding_id
            d["retrospective_id"] = self.retrospective_id
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Transaction:
        return cls(
            id=data["id"],
            type=data["type"],
            name=data["name"],
            sector=data.get("sector", ""),
            date=data["date"],
            price=data["price"],
            quantity=data["quantity"],
            total_amount=data["total_amount"],
            thesis=data.get("thesis", ""),
            research_notes=data.get("research_notes", ""),
            margin_ratio=data.get("margin_ratio", 100),
            profit_loss=data.get("profit_loss", 0.0),
            profit_loss_pct=data.get("profit_loss_pct", 0.0),
            sell_reason=data.get("sell_reason", ""),
            holding_id=data.get("holding_id", ""),
            retrospective_id=data.get("retrospective_id", ""),
        )
