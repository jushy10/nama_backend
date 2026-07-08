"""Entities: the top-ETFs view of a US exchange-traded fund.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py`` or the stock ``universe`` slice's — the same
convention as the earnings and recommendations sub-slices). Pure and vendor-agnostic —
stdlib only.

``ScreenedEtf`` is one row of what the *bulk screen* carries: the identity facts (``ticker`` /
``name`` / ``exchange``) alongside ``net_assets`` (assets under management, the ETF analogue of
a stock's market cap and the natural "top" ranking) and ``expense_ratio``. The fund's
``category`` is deliberately *not* on it — the bulk screen doesn't publish one, exactly like the
stock screen carries no sector — so it's filled separately by the sync's per-fund enrichment
pass (which reads it off the same ``EtfProfile`` as the rest of the fund's profile).

The read side (``GET /stocks/etfs`` + ``GET /stocks/etfs/categories``) adds the shapes the
search flows through: ``EtfSearchCriteria`` (a normalized query — free text, a ``category``
filter, an ``EtfSort`` field with a ``SortDirection`` and a limit/offset page), the
``EtfSearchResult`` rows it matches wrapped in an ``EtfSearchPage`` (carrying the total match
count for pagination), and ``EtfCategories`` (the distinct category slugs the FE offers as a
filter menu). All pure value objects — the SQL that reads them lives in the adapter, the
normalization in the use case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    # Only for the ``EtfDetail`` annotations — the shared ``Quote`` / ``StockPerformance`` are
    # themselves pure domain entities, but the annotations are stringized (``from __future__ import
    # annotations``) so nothing is imported at runtime, keeping this module import-light.
    from app.stocks.entities import Quote, StockPerformance


@dataclass(frozen=True)
class ScreenedEtf:
    """One fund in the screened top-ETF set — the facts the *bulk screen* carries.

    ``net_assets`` is assets under management in whole dollars (e.g. ``7.84e11`` for a $784B
    fund) — the fund's size, and the default "top" ranking. ``expense_ratio`` is a percent
    (``0.39`` = 0.39% a year). Everything but the ``ticker`` is optional: ``exchange`` and the
    name come from the screen, and either figure the screen omits rides in ``None``. The fund's
    ``category`` is not here — the screen doesn't carry it; the enrichment pass fills it (off the
    same ``EtfProfile`` it persists the rest of the profile from).
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    net_assets: float | None = None
    expense_ratio: float | None = None


class EtfSort(str, Enum):
    """The sortable columns of an ETF search.

    A ``str`` enum so FastAPI binds it straight from the ``?sort=`` query param (an unknown
    value is a 422, like ``StockSort``) and it serialises back as its value. ``NET_ASSETS`` is
    the natural default (biggest fund first — the "top" ETFs); ``EXPENSE_RATIO`` sorts by cost
    (cheapest first with ``order=asc``). Category is a *filter*, not a sort — it's a label, not a
    number. The value → column mapping is the adapter's job.
    """

    NET_ASSETS = "net_assets"
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
    screen's selection figure) but may still lack a name, an expense ratio, or a ``category``
    until the enrichment pass reaches it (or forever, for a fund Yahoo doesn't categorise).
    """

    ticker: str
    name: str | None
    exchange: str | None
    net_assets: float | None
    expense_ratio: float | None
    category: str | None


@dataclass(frozen=True)
class EtfSearchCriteria:
    """A normalized ETF-search request — the shape the use case hands the repository.

    Every field is already cleaned at the use-case edge: ``query`` is trimmed (``None`` when
    blank) and matched as a case-insensitive substring against name *or* ticker; ``category`` is
    slugged to the stored convention (``None`` when blank = don't filter); ``limit`` is clamped
    to a sane page and ``offset`` floored at zero. The adapter turns this into one SQL query.
    """

    query: str | None
    category: str | None
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


@dataclass(frozen=True)
class EtfCategories:
    """The distinct ETF category slugs present in the stored set — the FE's filter menu.

    One flat, sorted, de-duplicated list (nulls excluded); the search endpoint accepts the same
    slugs back as its ``category`` filter.
    """

    categories: tuple[str, ...]


# --- The ETF detail view (GET /stocks/etf/{ticker}) ---------------------------------------------
#
# A single fund's full card: the live quote (primary), the stored ``etfs``-table facts, and the
# best-effort profile enrichment. Unlike the search list this is a per-ticker read, so it carries
# the richer fund facts a detail page shows (fund family, NAV, trailing returns, holdings) that the
# bulk screen/table doesn't keep.


@dataclass(frozen=True)
class EtfHolding:
    """One of a fund's top holdings — the underlying position and its weight.

    ``weight`` is a percent of the fund (e.g. ``7.89`` for 7.89%), normalized from the vendor's
    fraction at the adapter. ``name`` is the holding's display name; ``ticker`` its symbol (either
    may be absent for an odd row, though the top holdings almost always carry both)."""

    ticker: str | None
    name: str | None
    weight: float | None  # percent of fund


@dataclass(frozen=True)
class EtfSectorWeight:
    """A fund's exposure to one market sector, as a percent of the fund.

    ``sector`` is the vendor's sector key (already a snake_case-ish slug, e.g. ``technology`` /
    ``consumer_cyclical``); ``weight`` is a percent (e.g. ``39.13``), normalized from the vendor's
    fraction at the adapter."""

    sector: str
    weight: float  # percent of fund


@dataclass(frozen=True)
class EtfProfile:
    """One fund's full profile — the facts the bulk screen doesn't carry.

    Two lives: the sync's yfinance adapter *produces* it from Yahoo's per-ticker ``.info`` /
    ``funds_data`` surfaces and the repository *persists* it (the scalars onto the ``etfs`` row,
    the two lists into their child tables); the detail read *reconstructs* it from those stored
    rows. Either way it's the same normalized shape. All percent figures are normalized to human
    percent here in the domain's vocabulary (the adapter owns the vendor's unit quirks):
    ``dividend_yield``, ``ytd_return``, ``three_year_return`` and ``five_year_return`` are
    percents; ``expense_ratio`` is a percent too. ``net_assets`` (AUM) and ``nav`` are raw
    figures. ``category`` is the fund's classification slug (e.g. ``large_growth``) — it rides the
    same ``.info`` fetch, so the enrichment pass reads it here rather than through a second call.
    ``top_holdings`` is capped and ordered largest first; ``sector_weightings`` is sorted by
    weight descending. Empty lists mean "unavailable", never "the fund holds nothing".

    ``net_assets`` / ``expense_ratio`` are here because the adapter reads them off the same blob,
    but the sync does **not** persist them from the profile — the screen owns those columns — so a
    profile rebuilt from storage leaves them ``None`` (the detail resolves them from the stored
    screen facts instead)."""

    category: str | None = None  # classification slug (e.g. "large_growth")
    fund_family: str | None = None
    net_assets: float | None = None  # AUM (raw), Yahoo's totalAssets (screen owns the stored col)
    expense_ratio: float | None = None  # percent (screen owns the stored col)
    nav: float | None = None  # net asset value per share (raw price)
    dividend_yield: float | None = None  # percent — feeds the card's 'dividends' block
    # ytd_return is still parsed but deliberately NOT surfaced on the detail card: the
    # 'performance' block's ``ytd`` is the Alpaca window (one vocabulary with 1w/1m/…/1y), so
    # Yahoo's own year-to-date figure would only duplicate/disagree with it.
    ytd_return: float | None = None  # percent (unsurfaced; see note above)
    three_year_return: float | None = None  # percent (annualized) — card's 'performance' block
    five_year_return: float | None = None  # percent (annualized) — card's 'performance' block
    description: str | None = None
    top_holdings: tuple[EtfHolding, ...] = ()
    sector_weightings: tuple[EtfSectorWeight, ...] = ()

    @classmethod
    def empty(cls) -> "EtfProfile":
        """The all-null profile — what a blocked or uncovered Yahoo read degrades to, so the
        detail endpoint still serves the quote + stored facts around it."""
        return cls()


@dataclass(frozen=True)
class EtfDetail:
    """Everything ``GET /stocks/etf/{ticker}`` serves for one fund, assembled from the three
    sources: the live quote (primary — Alpaca), the stored ``etfs``-table facts (name, exchange,
    category, net_assets, expense_ratio), and the best-effort Yahoo ``profile``.

    A composition of the three, assembled by the use case (like the ticker slice's ``TickerCard``
    bundles the quote and enrichment), so it lives here beside the entities it draws on rather than
    a separate concept. ``asset_type`` is always ``"etf"`` — this endpoint only serves funds (a
    non-ETF symbol is a 404 before this is built). The table facts win over the profile where both
    carry the same figure (net_assets, expense_ratio): the stored value is what the screener list
    shows, so the detail page must agree with it — the profile only fills the *gap* when the table
    lacks one. ``price``/``change``/``change_percent``/``previous_close``/``as_of`` are read off
    the live ``quote`` (its own change rules), so the fund's move never disagrees with the shared
    quote endpoint.

    ``include`` records which opt-in blocks the caller asked for (``metrics`` / ``dividends`` /
    ``performance``), so the presenter can tell "not requested" from "requested but unavailable" —
    the same stance the ticker card's ``TickerCard.include`` takes. ``performance`` is the trailing
    price-return windows (Alpaca), fetched only when that block is requested; the 3y/5y annualized
    returns it also carries ride the always-fetched ``profile``. The always-on enrichment
    (``fund_family`` / ``description`` / ``top_holdings`` / ``sector_weightings``) stays on the
    ``profile`` and is served regardless of the includes."""

    ticker: str
    quote: "Quote"  # live price + the day's move (primary source)
    name: str | None  # from the etfs table
    exchange: str | None  # from the etfs table
    category: str | None  # slug, from the etfs table
    net_assets: float | None  # AUM (raw): the table's, falling back to the profile's
    expense_ratio: float | None  # percent: the table's, falling back to the profile's
    profile: EtfProfile = field(default_factory=EtfProfile.empty)
    include: frozenset[str] = field(default_factory=frozenset)  # opt-in blocks asked for
    performance: "StockPerformance | None" = None  # trailing windows; only with 'performance'

    @classmethod
    def assemble(
        cls,
        ticker: str,
        quote: "Quote",
        facts: "EtfSearchResult",
        profile: EtfProfile,
        *,
        include: frozenset[str] = frozenset(),
        performance: "StockPerformance | None" = None,
    ) -> "EtfDetail":
        """Compose the detail from the live quote, the stored ``etfs`` facts, and the Yahoo
        profile — resolving net_assets/expense_ratio table-first, profile-as-fallback (so the
        detail page never contradicts the screener list, but a gap the table hasn't filled still
        gets a value when Yahoo has one). ``include`` (the requested opt-in blocks) and the
        best-effort ``performance`` (fetched only when that block was asked for) ride through."""
        return cls(
            ticker=ticker,
            quote=quote,
            name=facts.name,
            exchange=facts.exchange,
            category=facts.category,
            net_assets=facts.net_assets if facts.net_assets is not None else profile.net_assets,
            expense_ratio=(
                facts.expense_ratio
                if facts.expense_ratio is not None
                else profile.expense_ratio
            ),
            profile=profile,
            include=frozenset(include),
            performance=performance,
        )


def slugify(label: object) -> str | None:
    """A raw category label → a snake_case slug, or ``None``.

    Lower-cases, replaces each run of non-alphanumeric characters with a single ``_`` and strips
    leading/trailing underscores, turning display text into a stable key. A non-string or a
    label with no alphanumeric content (``""``, ``"—"``) collapses to ``None``. Idempotent on an
    already-slugged value, so the search use case can run an incoming ``category`` filter through
    it whether the client sends the raw label or the stored slug."""
    if not isinstance(label, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or None
