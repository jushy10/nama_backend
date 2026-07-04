"""HTTP response DTOs for the stock-search endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. A search hit is the
same handful of fields a ``ScreenedStock`` carries; ``market_cap`` is whole dollars.
"""

from pydantic import BaseModel


class StockSearchResult(BaseModel):
    """One matched company in a search response."""

    ticker: str
    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None


class StockSearchResponse(BaseModel):
    """The result set for a search query, largest market cap first.

    ``query`` echoes the (trimmed) term searched and ``count`` is how many results are
    returned (``<= limit``); an empty ``results`` means nothing in the universe matched.
    """

    query: str
    count: int
    results: list[StockSearchResult]
