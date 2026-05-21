"""국내 개별주식선물 거래 모델.

type:
  - "open": 신규/추가 진입
  - "close": 청산 (전량/부분)
  - "roll_close": 롤오버의 청산 쪽 (당월물)
  - "roll_open": 롤오버의 진입 쪽 (차월물)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FuturesTransaction:
    type: str                        # open | close | roll_close | roll_open
    name: str                        # 기초자산명
    symbol: str                      # 기초자산 종목코드
    contract_code: str               # 선물 종목코드
    contract_month: str              # YYYYMM
    expiry_date: str                 # YYYY-MM-DD
    direction: str                   # long | short
    contracts: int
    price: float
    multiplier: int = 10
    margin: float = 0.0              # open/roll_open 시 위탁증거금, close 시 환급액
    sector: str = ""
    date: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    reason: str = ""
    position_id: str = ""

    # open/roll_open 전용
    thesis: str = ""

    # close/roll_close 전용
    pnl: float = 0.0
    pnl_pct: float = 0.0
    buy_thesis: str = ""             # 진입 사유 스냅샷 (회고에 사용)
    retrospective_id: str = ""

    # 롤오버 페어 연결
    linked_tx_id: str = ""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def notional(self) -> float:
        return self.price * self.contracts * self.multiplier

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "symbol": self.symbol,
            "contract_code": self.contract_code,
            "contract_month": self.contract_month,
            "expiry_date": self.expiry_date,
            "direction": self.direction,
            "contracts": self.contracts,
            "price": self.price,
            "multiplier": self.multiplier,
            "margin": self.margin,
            "sector": self.sector,
            "date": self.date,
            "reason": self.reason,
            "position_id": self.position_id,
            "linked_tx_id": self.linked_tx_id,
        }
        if self.type in ("open", "roll_open"):
            d["thesis"] = self.thesis
        if self.type in ("close", "roll_close"):
            d["pnl"] = self.pnl
            d["pnl_pct"] = self.pnl_pct
            d["buy_thesis"] = self.buy_thesis
            d["retrospective_id"] = self.retrospective_id
        return d

    @classmethod
    def from_dict(cls, data: dict) -> FuturesTransaction:
        return cls(
            id=data["id"],
            type=data["type"],
            name=data["name"],
            symbol=data.get("symbol", ""),
            contract_code=data.get("contract_code", ""),
            contract_month=data.get("contract_month", ""),
            expiry_date=data.get("expiry_date", ""),
            direction=data.get("direction", "long"),
            contracts=data.get("contracts", 0),
            price=data.get("price", 0.0),
            multiplier=data.get("multiplier", 10),
            margin=data.get("margin", 0.0),
            sector=data.get("sector", ""),
            date=data.get("date", ""),
            reason=data.get("reason", ""),
            position_id=data.get("position_id", ""),
            thesis=data.get("thesis", ""),
            pnl=data.get("pnl", 0.0),
            pnl_pct=data.get("pnl_pct", 0.0),
            buy_thesis=data.get("buy_thesis", ""),
            retrospective_id=data.get("retrospective_id", ""),
            linked_tx_id=data.get("linked_tx_id", ""),
        )
