"""Entities: the top-ETFs view of a US exchange-traded fund.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py`` or the stock ``universe`` slice's — the same
convention as the earnings and recommendations sub-slices). Pure and vendor-agnostic —
stdlib only.

``ScreenedEtf`` is one row of the screened top-ETF set: the identity facts (``ticker`` /
``name`` / ``exchange``) alongside the figures the screen ranks and describes a fund by —
``net_assets`` (assets under management, the ETF analogue of a stock's market cap and the
natural "top" ranking), ``expense_ratio`` and ``ytd_return``. It is the single shape the
screener returns and the sync persists into the ``etfs`` table.

The read side (``GET /stocks/etfs``) adds the shapes the search flows through:
``EtfSearchCriteria`` (a normalized query — free text plus a ``EtfSort`` field with a
``SortDirection`` and a limit/offset page), the ``EtfSearchResult`` rows it matches wrapped in
an ``EtfSearchPage`` (carrying the total match count for pagination). All pure value objects —
the SQL that reads them lives in the adapter, the normalization in the use case.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ScreenedEtf:
    """One fund in the screened top-ETF set.

    ``net_assets`` is assets under management in whole dollars (e.g. ``7.84e11`` for a $784B
    fund) — the fund's size, and the default "top" ranking. ``expense_ratio`` and
    ``ytd_return`` are percents (``0.39`` = 0.39% a year; ``5.40`` = up 5.4% year-to-date).
    Everything but the ``ticker`` is optional: ``exchange`` and the name come from the screen,
    and any figure the screen omits rides in ``None``.
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    net_assets: float | None = None
    expense_ratio: float | None = None
    ytd_return: float | None = None


class EtfSort(str, Enum):
    """The sortable columns of an ETF search.

    A ``str`` enum so FastAPI binds it straight from the ``?sort=`` query param (an unknown
    value is a 422, like ``StockSort``) and it serialises back as its value. ``NET_ASSETS`` is
    the natural default (biggest fund first — the "top" ETFs); ``YTD_RETURN`` ranks by
    year-to-date performance and ``EXPENSE_RATIO`` by cost (cheapest first with ``order=asc``).
    The value → column mapping is the adapter's job — the enum just names the choices in domain
    terms.
    """

    NET_ASSETS = "net_assets"
    YTD_RETURN = "ytd_return"
    EXPENSE_RATIO = "expense_ratio"


class SortDirection(str, Enum):
    """Ascending or descending — the ``?order=`` query param, bound the same way.

    Slice-local (the stock ``universe`` slice keeps its own copy) so this slice stays
    self-contained rather than importing another slice's entities.
    """

    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class EtfSearchResult:
    """One row of an ETF search — the facts served straight from the ``etfs`` table, no live
    price (a page is a single DB read; the FE fetches a live quote per row on demand via the
    shared ``GET /stocks/{symbol}/quote``, which serves ETFs too).

    Everything but the ``ticker`` is nullable — a screened ETF always has ``net_assets`` (the
    screen's selection figure) but the name or a given ratio may be absent.
    """

    ticker: str
    name: str | None
    exchange: str | None
    net_assets: float | None
    expense_ratio: float | None
    ytd_return: float | None


@dataclass(frozen=True)
class EtfSearchCriteria:
    """A normalized ETF-search request — the shape the use case hands the repository.

    Every field is already cleaned at the use-case edge: ``query`` is trimmed (``None`` when
    blank) and matched as a case-insensitive substring against name *or* ticker; ``limit`` is
    clamped to a sane page and ``offset`` floored at zero. The adapter turns this into one SQL
    query.
    """

    query: str | None
    sort: EtfSort
    direction: SortDirection
    limit: int
    offset: int


@dataclass(frozen=True)
class EtfSearchPage:
    """A page of search results plus the total number of matches.

    ``total`` is the full count *before* ``limit``/``offset`` (so the FE can render pagers);
    ``results`` is just this page. ``limit`` / ``offset`` echo the criteria the page was cut
    with, so a client reading only the response knows where it is.
    """

    results: tuple[EtfSearchResult, ...]
    total: int
    limit: int
    offset: int
