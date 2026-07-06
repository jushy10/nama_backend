"""HTTP response DTOs for the ETF read endpoints.

Pydantic models at the edge, deliberately separate from the slice ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic (the same split the other
slices keep). These back ``GET /stocks/etfs`` (the search list) and ``GET /stocks/etfs/categories``
(the filter menu).
"""

from pydantic import BaseModel


class EtfSearchItemResponse(BaseModel):
    """One row of an ETF search — stored facts only, no live price.

    ``net_assets`` is raw USD (assets under management); ``expense_ratio`` is a percent;
    ``category`` is the fund's Yahoo category slug (e.g. ``large_growth``), ``null`` until the
    enrichment pass reaches the fund (or when Yahoo doesn't categorise it). The FE fetches a live
    quote per row on demand via the shared ``GET /stocks/{symbol}/quote`` (which serves ETFs too).
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    net_assets: float | None = None  # raw USD (AUM)
    expense_ratio: float | None = None  # percent
    category: str | None = None  # Yahoo fund-category slug


class EtfSearchResponse(BaseModel):
    """A page of search results plus the pagination envelope.

    ``total`` is the full match count before the window (so the FE can size its pager),
    ``count`` the number of rows in ``results`` this page, and ``limit`` / ``offset`` echo the
    window the page was cut with — so a client reading only the response knows where it is.
    """

    total: int
    limit: int
    offset: int
    count: int
    results: list[EtfSearchItemResponse]


class EtfCategoriesResponse(BaseModel):
    """The distinct ETF category slugs present in the stored set — the FE's filter menu.

    One flat, sorted list; the search endpoint accepts the same slugs back as its ``category``
    filter.
    """

    categories: list[str]
