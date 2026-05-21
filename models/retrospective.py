from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Retrospective:
    transaction_id: str
    stock_name: str
    sell_date: str
    original_thesis: str
    thesis_correct: bool | None = None  # True / False / None(부분적)
    what_went_well: str = ""
    regrets: str = ""
    avoidable: str = ""  # "피할 수 있었다" / "통제 불가" / "모르겠다"
    lessons: str = ""
    is_futures: bool = False
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "transaction_id": self.transaction_id,
            "stock_name": self.stock_name,
            "sell_date": self.sell_date,
            "original_thesis": self.original_thesis,
            "thesis_correct": self.thesis_correct,
            "what_went_well": self.what_went_well,
            "regrets": self.regrets,
            "avoidable": self.avoidable,
            "lessons": self.lessons,
            "is_futures": self.is_futures,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Retrospective:
        # 하위호환: is_futures가 없는 기존 데이터
        if "is_futures" not in data:
            data = {**data, "is_futures": False}
        return cls(**data)
