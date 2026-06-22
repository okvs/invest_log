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
    buy_thesis: str = ""  # sell 시점의 원래 매수 근거 스냅샷 (회고에 사용)
    retrospective_id: str = ""
    currency: str = "KRW"  # "KRW"(기본) | "USD"(미국주식, 나무/NH). price/total_amount 의 통화.
    # 연금계좌 거래 여부. True 면 기록(히스토리) 탭에만 '연금'으로 보이고, 보유/평가/총자산/
    # 예수금/섹터/4사분면/증권사구성/자산그래프/실현손익 등 모든 계산에서 제외된다. 기본 off.
    is_pension: bool = False
    # 원가/보유 정보 없이 들어온 매도(카톡 "보유 종목 없음" 자동스킵 등). True 면 손익 계산 없이
    # 기록에만 남기며, 보유/예수금에 영향을 주지 않는다(연금 전량매도 가시화 용도).
    orphan: bool = False
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
            "currency": self.currency,
            "is_pension": self.is_pension,
        }
        if self.orphan:
            d["orphan"] = True
        if self.type == "buy":
            d["thesis"] = self.thesis
            d["research_notes"] = self.research_notes
            d["margin_ratio"] = self.margin_ratio
        else:
            d["profit_loss"] = self.profit_loss
            d["profit_loss_pct"] = self.profit_loss_pct
            d["sell_reason"] = self.sell_reason
            d["holding_id"] = self.holding_id
            d["buy_thesis"] = self.buy_thesis
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
            buy_thesis=data.get("buy_thesis", ""),
            retrospective_id=data.get("retrospective_id", ""),
            currency=data.get("currency", "KRW"),
            is_pension=data.get("is_pension", False),
            orphan=data.get("orphan", False),
        )
