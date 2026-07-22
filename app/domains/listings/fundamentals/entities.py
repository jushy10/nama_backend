from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Fundamentals:
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    return_on_equity: float | None = None
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    beta: float | None = None
    book_value_per_share: float | None = None
    sales_per_share: float | None = None
    dividend_per_share: float | None = None
    ebitda: float | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    shares_outstanding: float | None = None
    name: str | None = None

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))
