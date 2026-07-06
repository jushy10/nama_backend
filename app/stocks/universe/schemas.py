"""HTTP response DTOs for the universe read endpoints.

Pydantic models at the edge, deliberately separate from the slice ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic (the same split the
other slices keep). These back ``GET /stocks/ticker`` (the search list) and
``GET /stocks/classifications`` (the filter menus).
"""

from pydantic import BaseModel


class StockSearchItemResponse(BaseModel):
    """One row of a universe search — anchor facts only, no live price.

    ``market_cap`` is raw USD; ``pe_ratio`` is the trailing P/E on the analyst-consensus
    (adjusted) basis — the same figure the ticker card serves, materialized here for sorting;
    ``revenue_growth_yoy`` / ``eps_growth_yoy`` are the annual slice's latest trailing
    year-over-year growth (percent, EPS on the analyst-consensus basis); ``in_sp500`` /
    ``in_nasdaq100`` are definite booleans. Everything but the flags and the ticker can be
    ``null`` until the enriching sync / annual slice reaches the stock (``pe_ratio`` also stays
    ``null`` until four quarters are cached, and for a trailing-year loss). The FE fetches a
    live quote or the full card per row on demand via ``GET /stocks/ticker/{ticker}``.
    """

    ticker: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None  # raw USD
    pe_ratio: float | None = None  # trailing P/E, consensus basis (matches the card)
    revenue_growth_yoy: float | None = None  # percent, latest trailing YoY
    eps_growth_yoy: float | None = None  # percent, latest trailing YoY, consensus basis
    in_sp500: bool
    in_nasdaq100: bool


class StockSearchResponse(BaseModel):
    """A page of search results plus the pagination envelope.

    ``total`` is the full match count before the window (so the FE can size its pager),
    ``count`` the number of rows in ``results`` this page, and ``limit`` / ``offset`` echo the
    window the page was cut with — so a client reading only the response knows where it is.
    """

    total: int
    limit: int
    offset: int
    count: int
    results: list[StockSearchItemResponse]


class ClassificationsResponse(BaseModel):
    """The distinct sector and industry slugs present in the universe — the FE's filter menus.

    Two flat, sorted lists; the search endpoint accepts the same slugs back as its ``sector`` /
    ``industry`` filters.
    """

    sectors: list[str]
    industries: list[str]
