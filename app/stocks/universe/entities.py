"""Entities: the investable-universe view of a stock.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py``, the same convention as the earnings and
recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

``ScreenedStock`` is one row of the screened universe: the identity facts the ``stocks``
anchor holds (``ticker`` / ``name`` / ``exchange``) alongside the screen's own figures —
``market_cap`` (the selection criterion) and ``sector``. It is the single shape the
screener returns and the sync persists onto the anchor.

``CompanyClassification`` is the stock's sector + industry, fetched separately (the bulk
screen carries neither) and stored as snake_case slugs by the sync's enrichment pass.

The read side (the ``GET /stocks/ticker`` search + ``GET /stocks/classifications``) adds the
shapes the search flows through: ``StockSearchCriteria`` (a normalized query — free text plus
sector/industry/index-membership filters, a ``StockSort`` field with a ``SortDirection``, and
a limit/offset page), the ``StockSearchResult`` rows it matches wrapped in a
``StockSearchPage`` (carrying the total match count for pagination), and ``Classifications``
(the distinct sector/industry slugs the FE offers as filter menus). All pure value objects —
the SQL that reads them lives in the adapter, the normalization in the use case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ScreenedStock:
    """One company in the screened universe.

    ``market_cap`` is in whole dollars (e.g. ``3.01e12`` for a $3.01T company). Everything
    but the ``ticker`` is optional: ``exchange`` comes from the screen, ``sector`` may be
    absent (the yfinance screen doesn't publish it, so it rides in ``None``), and the name
    may be missing.

    ``price`` is the screen-time regular-market price the screen quote carries. It is *not*
    persisted: the sync uses it (over the quarterly slice's TTM consensus EPS) to derive the
    stored ``pe_ratio`` on the anchor, the same way ``market_cap`` is a price-derived screen
    fact — so both value figures on a row come from one screen snapshot.
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None
    price: float | None = None  # screen-time price; derives pe_ratio, not itself stored


@dataclass(frozen=True)
class CompanyClassification:
    """A stock's sector + industry, as canonical snake_case slugs.

    The screen (``ScreenedStock``) carries neither — Yahoo publishes sector/industry only on
    the per-ticker ``.info`` surface — so this is the shape the sync's enrichment pass fetches
    and persists. Both sides are optional: a symbol Yahoo doesn't classify (or only half
    classifies) yields ``None`` for the missing side, which the sync leaves for a later run.

    Labels are stored as slugs — lower-cased, with every run of non-alphanumeric characters
    collapsed to a single underscore (``"Consumer Electronics"`` → ``consumer_electronics``,
    ``"Oil & Gas E&P"`` → ``oil_gas_e_p``) — a stable, join-friendly key rather than Yahoo's
    display text. ``from_labels`` is the constructor callers use, so the slug rule lives in
    one place.
    """

    sector: str | None = None
    industry: str | None = None

    @classmethod
    def from_labels(cls, sector: object, industry: object) -> "CompanyClassification":
        """Build a classification from raw vendor labels, each slugged to snake_case (and
        dropped to ``None`` when blank or non-string)."""
        return cls(sector=slugify(sector), industry=slugify(industry))


class StockSort(str, Enum):
    """The sortable columns of a universe search.

    A ``str`` enum so FastAPI binds it straight from the ``?sort=`` query param (an unknown
    value is a 422, like ``StockIndex``/``Timeframe``) and it serialises back as its value.
    These name the sortable *columns*; the search applies none of them unless one is asked for
    (omitting ``?sort=`` is a neutral, unsorted ticker order — see ``StockSearchCriteria.sort``),
    so there is no default member. ``MARKET_CAP`` orders biggest-first; ``REVENUE_GROWTH`` /
    ``EPS_GROWTH`` are the annual slice's latest *trailing* year-over-year figures on the anchor
    and ``FORWARD_REVENUE_GROWTH`` / ``FORWARD_EPS_GROWTH`` their *forward* (FY1→FY2 consensus)
    counterparts; ``GROWTH`` / ``FORWARD_GROWTH`` each blend a pair (its equal-weight average) so
    one control ranks the fastest all-round growers, trailing or expected; ``PE`` orders by the
    stored trailing P/E (the consensus-basis figure the universe sync writes) — ascending
    surfaces the cheapest on earnings first. The value → ORM
    column/expression mapping is the adapter's job — the enum just names the choices in domain
    terms.
    """

    MARKET_CAP = "market_cap"
    REVENUE_GROWTH = "revenue_growth"
    EPS_GROWTH = "eps_growth"
    GROWTH = "growth"
    FORWARD_REVENUE_GROWTH = "forward_revenue_growth"
    FORWARD_EPS_GROWTH = "forward_eps_growth"
    FORWARD_GROWTH = "forward_growth"
    PE = "pe"


class SortDirection(str, Enum):
    """Ascending or descending — the ``?order=`` query param, bound the same way."""

    ASC = "asc"
    DESC = "desc"


class MarketCapTier(str, Enum):
    """A market-capitalization size bucket — the ``?market_cap=`` filter.

    A ``str`` enum bound straight from the query param like ``StockSort`` (an unknown value is a
    422). The four conventional cap tiers — ``MEGA`` ≥ $200B, ``LARGE`` $10–200B, ``MID`` $2–10B,
    ``SMALL`` $250M–$2B — expressed as half-open ranges (lower inclusive, upper exclusive) so
    they don't overlap at the seams. The tier → dollar-bounds mapping is the adapter's job (like
    the sort → column map); the enum only names the choices. (The universe floor is $1B, so
    ``SMALL`` in practice surfaces the $1–2B slice.)
    """

    MEGA = "mega"
    LARGE = "large"
    MID = "mid"
    SMALL = "small"


@dataclass(frozen=True)
class StockSearchResult:
    """One row of a universe search — the anchor facts served straight from the ``stocks``
    table, no live price (a page is a single DB read; the FE fetches a quote/card per row on
    demand via ``GET /stocks/ticker/{ticker}``).

    ``in_sp500`` / ``in_nasdaq100`` are definite yes/no (the anchor stores them ``NOT NULL``);
    everything else is nullable — a screened stock always has a ``market_cap`` (the search
    only returns screened rows) but may still lack a name, a classification, the trailing /
    forward growth, or a ``pe_ratio`` until the enriching sync/annual slice reaches it (forward
    growth the most often, since it needs two upcoming years; ``pe_ratio`` stays null until the
    quarterly cache holds four reported quarters, and for a trailing-year loss).
    """

    ticker: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: float | None
    pe_ratio: float | None
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    forward_revenue_growth_yoy: float | None
    forward_eps_growth_yoy: float | None
    in_sp500: bool
    in_nasdaq100: bool


@dataclass(frozen=True)
class StockSearchCriteria:
    """A normalized universe-search request — the shape the use case hands the repository.

    Every field is already cleaned at the use-case edge: ``query`` is trimmed (``None`` when
    blank) and matched as a case-insensitive substring against name *or* ticker; ``sector`` /
    ``industry`` are slugged to the stored convention (``None`` when blank); the index flags
    are tri-state (``None`` = don't filter, else match the boolean); ``market_cap_tier`` narrows
    to one cap bucket (``None`` = every size); ``limit`` is clamped to a sane page and ``offset``
    floored at zero. The adapter turns this into one SQL query.

    ``sort`` is ``None`` for an unsorted search — the adapter then orders by ticker alone (a
    neutral, stable A→Z), the default when a client omits ``?sort=``; a ``StockSort`` value picks
    a column to order by. ``direction`` only bites once a ``sort`` is chosen (an unsorted page is
    always ascending by ticker).
    """

    query: str | None
    sector: str | None
    industry: str | None
    in_sp500: bool | None
    in_nasdaq100: bool | None
    sort: StockSort | None
    direction: SortDirection
    limit: int
    offset: int
    market_cap_tier: MarketCapTier | None = None


@dataclass(frozen=True)
class StockSearchPage:
    """A page of search results plus the total number of matches.

    ``total`` is the full count *before* ``limit``/``offset`` (so the FE can render pagers);
    ``results`` is just this page. ``limit`` / ``offset`` echo the criteria the page was cut
    with, so a client reading only the response knows where it is.
    """

    results: tuple[StockSearchResult, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class Classifications:
    """The distinct sector and industry slugs present in the universe — the FE's filter menus.

    Two flat, sorted, de-duplicated lists (nulls excluded). The FE offers each independently;
    the search endpoint accepts the same slugs back as its ``sector`` / ``industry`` filters.
    """

    sectors: tuple[str, ...]
    industries: tuple[str, ...]


def slugify(label: object) -> str | None:
    """A raw classification label → a snake_case slug, or ``None``.

    Lower-cases, replaces each run of non-alphanumeric characters with a single ``_`` and
    strips leading/trailing underscores, turning display text into a stable key. A non-string
    or a label with no alphanumeric content (``""``, ``"—"``) collapses to ``None``. Idempotent
    on an already-slugged value, so the search use case can run an incoming ``sector`` /
    ``industry`` filter through it whether the client sends the raw label or the stored slug."""
    if not isinstance(label, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or None
