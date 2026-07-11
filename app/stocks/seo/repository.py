"""Abstract persistence port for the SEO slice.

The read shape a content page needs, owned by this slice rather than borrowed from the
ticker card's ``StoredTickerFacts`` — a page surfaces a couple of columns the card
doesn't (the materialized trailing P/E and FCF yield, the index-membership flags), so it
gets its own projection instead of widening a shared one.

A *Repository*, not a *Provider*: it fronts our own ``stocks`` anchor, never a vendor —
which is what keeps these pages crawl-safe (a bot hit is one indexed DB read, never a
live Alpaca/Yahoo call). The concrete SQLAlchemy implementation is in ``db_repository.py``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TickerPageFacts:
    """The stored facts a stock content page renders — all DB-only, per-field ``None``
    for whatever the anchor row hasn't learned yet.

    ``market_cap`` doubles as the *screened* signal: only rows the universe sync has
    valued carry it, and it's what the use case reads to decide a page is index-worthy
    (a merely-incidental, ticker-only row is served but ``noindex``). ``pe_ratio`` and
    ``fcf_yield`` are the universe sync's materialized valuation snapshots; the growth
    figures are the annual slice's latest trailing YoY (percent). ``in_sp500`` /
    ``in_nasdaq100`` are the index-membership flags (never ``None`` — a definite yes/no).
    """

    name: str | None
    exchange: str | None
    sector: str | None
    industry: str | None
    market_cap: float | None
    pe_ratio: float | None
    fcf_yield: float | None
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    fcf_growth_yoy: float | None
    in_sp500: bool
    in_nasdaq100: bool


@dataclass(frozen=True)
class StockPageRef:
    """One entry in the sitemap: a stock page's ticker and when its data last changed.

    ``last_modified`` is the anchor's ``screened_at`` stamp (date only — a sitemap
    ``lastmod`` needn't be to the second, and a date avoids needless churn); ``None`` when
    the row carries no stamp, in which case the sitemap simply omits ``lastmod`` for it."""

    ticker: str
    last_modified: date | None


@dataclass(frozen=True)
class SectorStock:
    """One row on a sector page: enough to render a linked, sortable listing without a
    second read per stock. All the figures come off the anchor (screened rows only)."""

    ticker: str
    name: str | None
    market_cap: float | None
    pe_ratio: float | None
    fcf_yield: float | None


class SeoReadRepository(ABC):
    """A read-only view of the ``stocks`` anchor for the content pages."""

    @abstractmethod
    def get_ticker_facts(self, ticker: str) -> TickerPageFacts | None:
        """Return the page facts for the (already-normalized) ticker, or ``None`` when
        no anchor row exists at all. A ``None`` is the "unknown symbol" signal the
        endpoint maps to a 404; a present-but-unscreened row (``market_cap is None``) is
        a real, servable page that just isn't index-worthy yet."""
        raise NotImplementedError

    @abstractmethod
    def list_stock_pages(self, limit: int) -> tuple[StockPageRef, ...]:
        """Every index-worthy stock page for the sitemap — the *screened* rows
        (``market_cap`` filled, the same gate a single page uses to decide it's
        indexable), most valuable first, capped at ``limit``. Ordering by market cap
        means a truncated sitemap still lists the biggest, most-searched names."""
        raise NotImplementedError

    @abstractmethod
    def list_sector_stocks(self, sector: str, limit: int) -> tuple[SectorStock, ...]:
        """The screened stocks in one sector (by stored snake_case slug), most valuable
        first, capped at ``limit``. Empty when the sector is unknown — the "not a real
        sector" signal the endpoint maps to a 404."""
        raise NotImplementedError

    @abstractmethod
    def list_sectors(self) -> tuple[str, ...]:
        """The distinct sector slugs across the screened universe, sorted — the set of
        ``/sector/{slug}`` pages that exist, for the sitemap."""
        raise NotImplementedError
