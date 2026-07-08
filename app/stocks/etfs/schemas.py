"""HTTP response DTOs for the ETF read endpoints.

Pydantic models at the edge, deliberately separate from the slice ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic (the same split the other
slices keep). These back ``GET /stocks/etfs`` (the search list), ``GET /stocks/etfs/categories``
(the filter menu), and ``GET /stocks/etf/{ticker}`` (one fund's detail card).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


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


class EtfHoldingResponse(BaseModel):
    """One of a fund's top holdings — the underlying position and its weight.

    ``weight`` is a percent of the fund (e.g. ``7.89``); ``ticker`` / ``name`` identify the
    holding (either may be ``null`` for an odd row)."""

    ticker: str | None = None
    name: str | None = None
    weight: float | None = None  # percent of fund


class EtfSectorWeightResponse(BaseModel):
    """A fund's exposure to one market sector, as a percent of the fund.

    ``sector`` is the vendor's sector key (a slug, e.g. ``technology``); ``weight`` is a percent
    (e.g. ``39.13``). The list is sorted by weight descending."""

    sector: str
    weight: float  # percent of fund


class EtfMetricsResponse(BaseModel):
    """The fund's headline size/cost metrics — the opt-in ``metrics`` block.

    ``expense_ratio`` and ``net_assets`` are the stored ``etfs``-table facts (falling back to Yahoo
    only when the table lacks them, so this block agrees with the screener list); ``nav`` (net asset
    value per share) rides the best-effort Yahoo profile. ``expense_ratio`` is a human percent
    (``0.03`` = 0.03%); ``net_assets`` (AUM) and ``nav`` are raw figures. Any field Yahoo/the table
    doesn't carry is ``null``."""

    expense_ratio: float | None = None  # percent
    nav: float | None = None  # net asset value per share (raw price)
    net_assets: float | None = None  # AUM (raw)


class EtfDividendsResponse(BaseModel):
    """The fund's distribution yield — the opt-in ``dividends`` block.

    ``yield_percentage`` is the trailing distribution yield as a human percent (``1.03`` = 1.03%),
    off the best-effort Yahoo profile; ``null`` for a non-distributing fund or an uncovered
    field."""

    yield_percentage: float | None = None  # percent


class EtfPerformanceResponse(StockPerformanceResponse):
    """The fund's trailing returns — the opt-in ``performance`` block.

    Extends the shared trailing-window shape (``1w`` / ``1m`` / ``3m`` / ``6m`` / ``ytd`` / ``1y``,
    the same price-return gains the stock endpoints serve, from Alpaca) with the two longer horizons
    Yahoo publishes: ``three_year_return`` / ``five_year_return`` (annualized average returns, off
    the profile). Every figure is a human percent; any window without enough history — or a
    horizon Yahoo doesn't cover — is ``null``."""

    three_year_return: float | None = None  # percent (annualized avg, Yahoo)
    five_year_return: float | None = None  # percent (annualized avg, Yahoo)


class EtfDetailResponse(BaseModel):
    """One fund's detail card: the live quote, the stored ``etfs`` facts, the always-on Yahoo
    enrichment, and the opt-in blocks (``GET /stocks/etf/{ticker}?include=...``).

    ``ticker`` is the symbol and ``asset_type`` is always ``"etf"`` (the endpoint only serves
    funds — a non-ETF symbol is a 404). ``price`` / ``change`` / ``change_percent`` /
    ``previous_close`` / ``as_of`` are the live quote (Alpaca), the same rules as every other price
    view. ``name`` / ``exchange`` / ``category`` are stored ``etfs``-table facts. The always-on
    Yahoo enrichment — ``fund_family`` / ``description`` / ``top_holdings`` / ``sector_weightings``
    — is best-effort: ``null`` (or ``[]`` for the lists) when Yahoo is blocked or doesn't cover the
    field, still a 200.

    ``metrics`` (expense ratio, NAV, net assets), ``dividends`` (yield) and ``performance``
    (trailing returns) are **opt-in** via ``?include=`` — ``null`` unless requested. Requesting
    ``metrics`` / ``dividends`` costs no extra upstream call (they're drawn from the already-fetched
    profile + stored facts); ``performance`` is the one block with its own call (the Alpaca windows),
    fetched only when asked for and best-effort. Every percent field (``expense_ratio``, the yield,
    the ``*_return`` figures, each holding/sector ``weight``) is a human percent (``0.03`` = 0.03%,
    ``39.13`` = 39.13%); ``net_assets`` and ``nav`` are raw figures."""

    ticker: str
    name: str | None = None
    exchange: str | None = None
    asset_type: Literal["etf"] = "etf"  # always "etf" — this endpoint only serves funds
    # The live quote (Alpaca), primary.
    price: float
    change: float | None = None  # absolute move vs the previous close
    change_percent: float | None = None  # percent move vs the previous close
    previous_close: float | None = None
    as_of: datetime | None = None
    # Stored etfs-table facts.
    category: str | None = None  # fund-category slug (e.g. "large_blend")
    # Always-on best-effort Yahoo (yfinance) enrichment — null / [] when unavailable.
    fund_family: str | None = None
    description: str | None = None
    top_holdings: list[EtfHoldingResponse] = []  # up to 10, largest first; [] if unavailable
    sector_weightings: list[EtfSectorWeightResponse] = []  # weight desc; [] if unavailable
    # Opt-in blocks (?include=metrics,dividends,performance) — null unless requested.
    metrics: EtfMetricsResponse | None = None
    dividends: EtfDividendsResponse | None = None
    performance: EtfPerformanceResponse | None = None
