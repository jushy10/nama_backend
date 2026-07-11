"""Shared HTTP response models for the stocks feature.

Pydantic is a web/serialization detail, so these DTOs live at the edge —
deliberately separate from the entities so the core stays framework-agnostic.
Only the DTOs shared across sub-slices live here (the trailing-performance
block appears on the ticker card, the ETF card and the sector board); a DTO
used by exactly one sub-slice lives in that slice's own ``schemas.py``.
"""

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
