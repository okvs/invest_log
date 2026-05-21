"""국내 개별주식선물 보유 포지션 모델.

개별주식선물: 기초자산 10주 = 1계약 (multiplier 기본 10).
방향(long/short), 만기물(분기 결제), 위탁증거금/유지증거금을 함께 관리한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


DEFAULT_MULTIPLIER = 10  # 개별주식선물 거래승수 (기초자산 10주 = 1계약)


@dataclass
class FuturesPosition:
    name: str                       # 기초자산명 (예: 삼성전자)
    symbol: str                     # 기초자산 종목코드 (예: 005930)
    contract_code: str              # 선물 종목코드 (예: 1AB6000)
    contract_month: str             # 결제월 YYYYMM (예: 202606)
    expiry_date: str                # 만기일 YYYY-MM-DD
    direction: str                  # "long" | "short"
    contracts: int                  # 보유 계약수
    avg_entry_price: float          # 평균 진입가
    initial_margin: float           # 위탁증거금 누적
    multiplier: int = DEFAULT_MULTIPLIER
    maintenance_margin: float = 0.0  # 유지증거금 (참고)
    entry_date: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    sector: str = ""
    thesis: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    transaction_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "symbol": self.symbol,
            "contract_code": self.contract_code,
            "contract_month": self.contract_month,
            "expiry_date": self.expiry_date,
            "direction": self.direction,
            "contracts": self.contracts,
            "multiplier": self.multiplier,
            "avg_entry_price": self.avg_entry_price,
            "initial_margin": self.initial_margin,
            "maintenance_margin": self.maintenance_margin,
            "entry_date": self.entry_date,
            "sector": self.sector,
            "thesis": self.thesis,
            "transaction_ids": self.transaction_ids,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FuturesPosition:
        return cls(
            id=data["id"],
            name=data["name"],
            symbol=data.get("symbol", ""),
            contract_code=data.get("contract_code", ""),
            contract_month=data["contract_month"],
            expiry_date=data["expiry_date"],
            direction=data["direction"],
            contracts=data["contracts"],
            multiplier=data.get("multiplier", DEFAULT_MULTIPLIER),
            avg_entry_price=data["avg_entry_price"],
            initial_margin=data.get("initial_margin", 0.0),
            maintenance_margin=data.get("maintenance_margin", 0.0),
            entry_date=data.get("entry_date", ""),
            sector=data.get("sector", ""),
            thesis=data.get("thesis", ""),
            transaction_ids=data.get("transaction_ids", []),
        )

    def add_entry(
        self,
        price: float,
        contracts: int,
        margin: float,
        transaction_id: str,
    ) -> None:
        """추가 진입 — 같은 방향·같은 결제월물 가정. 평균진입가/증거금 누적."""
        if contracts <= 0:
            raise ValueError("계약수는 1 이상이어야 합니다.")
        old_notional = self.avg_entry_price * self.contracts
        new_notional = price * contracts
        total_contracts = self.contracts + contracts
        self.avg_entry_price = (old_notional + new_notional) / total_contracts
        self.contracts = total_contracts
        self.initial_margin += margin
        self.transaction_ids.append(transaction_id)

    def close(self, price: float, contracts: int) -> tuple[float, float, float]:
        """포지션 청산. 청산한 만큼의 (실현손익, 환급 증거금, 청산 계약수) 반환.
        부분 청산이면 잔여 포지션 유지, 전량이면 contracts=0이 된다.
        """
        if contracts <= 0:
            raise ValueError("청산 계약수는 1 이상이어야 합니다.")
        if contracts > self.contracts:
            raise ValueError(
                f"보유 계약수({self.contracts})보다 많은 수량({contracts})을 청산할 수 없습니다."
            )

        sign = 1 if self.direction == "long" else -1
        pnl = (price - self.avg_entry_price) * contracts * self.multiplier * sign

        # 증거금 비례 환급
        margin_release = 0.0
        if self.initial_margin > 0 and self.contracts > 0:
            margin_release = self.initial_margin * (contracts / self.contracts)
            self.initial_margin -= margin_release

        self.contracts -= contracts
        return pnl, margin_release, float(contracts)

    def notional(self) -> float:
        """현재 명목 가치 = 평균진입가 × 계약수 × 승수."""
        return self.avg_entry_price * self.contracts * self.multiplier

    def unrealized_pnl(self, current_price: float) -> float:
        """현재가 기준 미실현 손익."""
        sign = 1 if self.direction == "long" else -1
        return (current_price - self.avg_entry_price) * self.contracts * self.multiplier * sign
