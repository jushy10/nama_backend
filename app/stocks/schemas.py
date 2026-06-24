"""HTTP response model for the stocks endpoint.

Pydantic is a web/serialization detail, so this DTO lives at the edge —
deliberately separate from the Stock entity so the core stays
framework-agnostic.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StockPerformanceResponse(BaseModel):
    """Trailing price-return windows (percent), keyed finance-style in JSON.

    Field names are valid Python identifiers; aliases produce the "1w"/"1m"/…
    JSON keys (FastAPI serializes response models by alias).
    """

    model_config = ConfigDict(populate_by_name=True)

    one_week: float | None = Field(default=None, alias="1w")
    one_month: float | None = Field(default=None, alias="1m")
    three_month: float | None = Field(default=None, alias="3m")
    six_month: float | None = Field(default=None, alias="6m")
    ytd: float | None = Field(default=None, alias="ytd")
    one_year: float | None = Field(default=None, alias="1y")


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
    market_cap: float | None = None  # raw USD
    dividend_per_share: float | None = None  # $ per share, annual
    dividend_yield: float | None = None  # percent
    performance: StockPerformanceResponse | None = None
