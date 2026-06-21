"""HTTP response model for the stocks endpoint.

Pydantic is a web/serialization detail, so this DTO lives at the edge —
deliberately separate from the Stock entity so the core stays
framework-agnostic.
"""

from datetime import datetime

from pydantic import BaseModel


class StockResponse(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    price: float
    change: float | None = None
    change_percent: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: int | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    as_of: datetime | None = None
