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
    ticker: str
    name: str | None = None
    exchange: str | None = None
    net_assets: float | None = None
    expense_ratio: float | None = None


class EtfSort(str, Enum):
    NET_ASSETS = "net_assets"
    EXPENSE_RATIO = "expense_ratio"
    DIVIDEND_YIELD = "dividend_yield"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class EtfSearchResult:
    ticker: str
    name: str | None
    exchange: str | None
    net_assets: float | None
    expense_ratio: float | None
    category: str | None
    dividend_yield: float | None = None


@dataclass(frozen=True)
class EtfSearchCriteria:
    query: str | None
    categories: tuple[str, ...]
    sort: EtfSort
    direction: SortDirection
    limit: int
    offset: int


@dataclass(frozen=True)
class EtfSearchPage:
    results: tuple[EtfSearchResult, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class EtfScreenIntent:
    query: str | None = None
    categories: tuple[str, ...] = ()
    sort: EtfSort | None = None
    direction: SortDirection = SortDirection.DESC
    limit: int | None = None


@dataclass(frozen=True)
class EtfCategories:
    categories: tuple[str, ...]


# --- The ETF detail view (GET /stocks/etf/{ticker}) ---------------------------------------------
#
# A single fund's full card: the live quote (primary), the stored ``etfs``-table facts, and the
# best-effort profile enrichment. Unlike the search list this is a per-ticker read, so it carries
# the richer fund facts a detail page shows (fund family, NAV, trailing returns, holdings) that the
# bulk screen/table doesn't keep.


@dataclass(frozen=True)
class EtfHolding:
    ticker: str | None
    name: str | None
    weight: float | None  # percent of fund


@dataclass(frozen=True)
class EtfSectorWeight:
    sector: str
    weight: float  # percent of fund


@dataclass(frozen=True)
class EtfProfile:
    category: str | None = None  # classification slug (e.g. "large_growth")
    fund_family: str | None = None
    net_assets: float | None = None  # AUM (raw), Yahoo's totalAssets (screen owns the stored col)
    expense_ratio: float | None = None  # percent (screen owns the stored col)
    nav: float | None = None  # net asset value per share (raw price)
    dividend_yield: float | None = None  # percent — feeds the card's 'dividends' block
    # The trailing-return ladder is not stored (see the class docstring) — on the detail read it's
    # overlaid from a live Yahoo read, only when the 'performance' block is requested.
    # ytd_return is parsed but deliberately NOT surfaced on the card: the 'performance' block's
    # ``ytd`` is the Alpaca window (one vocabulary with 1w/1m/…/1y), so Yahoo's own year-to-date
    # figure would only duplicate/disagree with it.
    ytd_return: float | None = None  # percent (live-read; unsurfaced; see note above)
    three_year_return: float | None = None  # percent (annualized, live-read) — 'performance' block
    five_year_return: float | None = None  # percent (annualized, live-read) — 'performance' block
    description: str | None = None
    top_holdings: tuple[EtfHolding, ...] = ()
    sector_weightings: tuple[EtfSectorWeight, ...] = ()

    @classmethod
    def empty(cls) -> "EtfProfile":
        return cls()


@dataclass(frozen=True)
class EtfDetail:
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
    if not isinstance(label, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or None
