"""Application use case for the SEO slice.

One read action per page type. ``GetTickerStockPage`` normalizes the ticker, pulls the
DB-only facts through the ``SeoReadRepository`` port, and hands back a small view the
endpoint renders — pure orchestration, no framework, no vendor, so it runs offline
against a hand-written fake like every other slice.

The view carries only *domain* judgements (is this page worth indexing? what's the
display name?); the title/description/JSON-LD/HTML are presentation and belong at the
edge (the endpoint + the Jinja2 template), not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.stocks.seo.repository import (
    CongressPageTrade,
    EtfPageFacts,
    SectorStock,
    SeoReadRepository,
    StockPageRef,
    TickerPageFacts,
)

# A ticker is 1–5 letters, optionally with a single class suffix (BRK-B, BF-B). Yahoo/the
# universe store the suffix with a hyphen, so a dotted input (BRK.B) is normalized to it.
# Deliberately a touch more permissive than the ticker card's alpha-only guard so the
# class-share names in the universe still get a page.
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")


def normalize_ticker(raw: str) -> str:
    """Trim/upper-case the ticker, fold a dotted class suffix onto the stored hyphen form,
    and reject obvious junk — once, at the edge, so the layers below see a clean symbol
    (the same stance the other slices' ``_normalize_symbol`` takes)."""
    ticker = (raw or "").strip().upper().replace(".", "-")
    if not ticker:
        raise ValueError("A ticker is required.")
    if not _TICKER_RE.match(ticker):
        raise ValueError(f"'{raw}' is not a valid ticker.")
    return ticker


@dataclass(frozen=True)
class TickerStockPage:
    """What the stock content page needs to render: the normalized ticker, its stored facts
    (``None`` when the symbol is unknown to us), and its recent Congressional trades (empty when
    Congress hasn't traded it, or it isn't seeded yet — the section is hidden in that case)."""

    ticker: str
    facts: TickerPageFacts | None
    congress: tuple[CongressPageTrade, ...] = ()

    @property
    def has_data(self) -> bool:
        """Is there anything to show? A row with at least a name or a market cap is a real
        page; an all-empty (or absent) row is a 404 rather than a soft, contentless 200."""
        return self.facts is not None and (
            self.facts.name is not None or self.facts.market_cap is not None
        )

    @property
    def indexable(self) -> bool:
        """Only *screened* stocks (the universe sync filled ``market_cap``) carry the full
        fact set worth putting in the index; anything else is served but ``noindex`` so a
        thin page never dilutes the site."""
        return self.facts is not None and self.facts.market_cap is not None

    @property
    def display_name(self) -> str:
        """The company name if we know it, else the ticker itself — so a header/title
        always has something to render."""
        if self.facts is not None and self.facts.name:
            return self.facts.name
        return self.ticker


class GetTickerStockPage:
    """Use case: assemble a stock's content-page view from DB-only facts, including its recent
    Congressional trades (a best-effort section — hidden when there are none)."""

    # A handful of recent trades — enough to make the section substantive without turning the page
    # into a full ledger (the /congress board and the app carry the complete feed).
    CONGRESS_LIMIT = 12

    def __init__(self, repository: SeoReadRepository) -> None:
        self._repository = repository

    def execute(self, ticker: str) -> TickerStockPage:
        normalized = normalize_ticker(ticker)
        return TickerStockPage(
            ticker=normalized,
            facts=self._repository.get_ticker_facts(normalized),
            congress=self._repository.list_congress_trades_for_ticker(
                normalized, self.CONGRESS_LIMIT
            ),
        )


def normalize_sector_slug(raw: str) -> str:
    """Fold a URL sector slug onto the stored snake_case form: lower-case, and map the
    URL-friendly hyphen back to the stored underscore (``consumer-electronics`` ->
    ``consumer_electronics``). Rejects anything that isn't a plain slug."""
    slug = (raw or "").strip().lower().replace("-", "_")
    if not slug:
        raise ValueError("A sector is required.")
    if not re.match(r"^[a-z0-9_]+$", slug):
        raise ValueError(f"'{raw}' is not a valid sector.")
    return slug


@dataclass(frozen=True)
class SectorPage:
    """What a sector content page renders: the sector (stored slug) and its top stocks."""

    slug: str  # stored snake_case form
    stocks: tuple[SectorStock, ...]

    @property
    def has_data(self) -> bool:
        """A real sector has at least one screened stock; an unknown slug yields none and
        is a 404 rather than an empty page."""
        return len(self.stocks) > 0

    @property
    def url_slug(self) -> str:
        """The hyphenated form used in URLs (better for search than underscores)."""
        return self.slug.replace("_", "-")

    @property
    def label(self) -> str:
        """A human sector label — ``consumer_electronics`` -> ``Consumer Electronics``."""
        return self.slug.replace("_", " ").title()


class GetSectorPage:
    """Use case: a sector's content-page view — its top stocks by market cap, from DB-only
    facts. The listing is the internal-linking hub (sector -> each /stock/ page)."""

    # Enough to be a rich, link-dense page without an unbounded listing.
    LIMIT = 100

    def __init__(self, repository: SeoReadRepository) -> None:
        self._repository = repository

    def execute(self, sector: str) -> SectorPage:
        slug = normalize_sector_slug(sector)
        return SectorPage(
            slug=slug,
            stocks=self._repository.list_sector_stocks(slug, self.LIMIT),
        )


@dataclass(frozen=True)
class EtfPage:
    """What an ETF content page renders: the normalized ticker and its stored fund facts
    (``None`` when the symbol isn't one of our funds)."""

    ticker: str
    facts: EtfPageFacts | None

    @property
    def has_data(self) -> bool:
        return self.facts is not None and (
            self.facts.name is not None or self.facts.net_assets is not None
        )

    @property
    def indexable(self) -> bool:
        # Every fund in the table was screened (AUM filled); this mirrors the stock page's
        # market_cap gate.
        return self.facts is not None and self.facts.net_assets is not None

    @property
    def display_name(self) -> str:
        if self.facts is not None and self.facts.name:
            return self.facts.name
        return self.ticker


class GetEtfPage:
    """Use case: assemble an ETF's content-page view from DB-only facts."""

    def __init__(self, repository: SeoReadRepository) -> None:
        self._repository = repository

    def execute(self, ticker: str) -> EtfPage:
        normalized = normalize_ticker(ticker)
        return EtfPage(ticker=normalized, facts=self._repository.get_etf_facts(normalized))


@dataclass(frozen=True)
class ScreenDef:
    """A curated "best-of" screen: a titled listing of the top stocks by one metric. The
    ``sort_key`` is the stable string the repository maps to an anchor column."""

    slug: str
    heading: str
    description: str  # meta description
    subtitle: str
    sort_key: str
    descending: bool = True
    positive_only: bool = False


# The curated screens — each a high-intent long-tail landing page, generated from the same
# universe the search sorts. Keyed by URL slug.
SCREENS: dict[str, ScreenDef] = {
    screen.slug: screen
    for screen in (
        ScreenDef(
            slug="high-fcf-yield",
            heading="Stocks with the Highest Free Cash Flow Yield",
            description=(
                "The US stocks with the highest free-cash-flow yield — cheap on the cash "
                "they actually generate. Updated daily on Nama Insights."
            ),
            subtitle=(
                "The screened US stocks (≥$1B market cap) with the highest free-cash-flow "
                "yield — free cash flow as a percent of market value."
            ),
            sort_key="fcf_yield",
            descending=True,
        ),
        ScreenDef(
            slug="cheapest-pe",
            heading="Cheapest Stocks by Trailing P/E",
            description=(
                "The US stocks trading at the lowest trailing price-to-earnings ratios. "
                "Updated daily on Nama Insights."
            ),
            subtitle=(
                "The screened US stocks (≥$1B market cap) with the lowest positive trailing "
                "P/E — priced cheaply against their earnings."
            ),
            sort_key="pe_ratio",
            descending=False,
            positive_only=True,
        ),
        ScreenDef(
            slug="highest-revenue-growth",
            heading="Stocks with the Highest Revenue Growth",
            description=(
                "The US stocks growing revenue fastest year-over-year. Updated daily on "
                "Nama Insights."
            ),
            subtitle=(
                "The screened US stocks (≥$1B market cap) with the highest trailing "
                "year-over-year revenue growth."
            ),
            sort_key="revenue_growth_yoy",
            descending=True,
        ),
        ScreenDef(
            slug="largest-companies",
            heading="Largest US Companies by Market Cap",
            description=(
                "The largest US companies by market capitalization, with valuation and "
                "cash-flow metrics. Updated daily on Nama Insights."
            ),
            subtitle="The biggest US companies by market capitalization.",
            sort_key="market_cap",
            descending=True,
        ),
    )
}


def normalize_screen_slug(raw: str) -> str:
    """Lower-case/trim a screen slug and reject non-slug input. Screen slugs are hyphenated
    and matched against the ``SCREENS`` registry (an unknown-but-valid slug is a 404)."""
    slug = (raw or "").strip().lower()
    if not slug:
        raise ValueError("A screen is required.")
    if not re.match(r"^[a-z0-9-]+$", slug):
        raise ValueError(f"'{raw}' is not a valid screen.")
    return slug


@dataclass(frozen=True)
class ScreenPage:
    """What a screen listing page renders: the screen definition and its top stocks."""

    screen: ScreenDef | None
    stocks: tuple[SectorStock, ...]

    @property
    def has_data(self) -> bool:
        """A real screen (a known slug) with at least one stock; an unknown slug or an empty
        universe is a 404."""
        return self.screen is not None and len(self.stocks) > 0


class GetScreenPage:
    """Use case: a "best-of" screen listing page, from DB-only facts."""

    LIMIT = 100

    def __init__(self, repository: SeoReadRepository) -> None:
        self._repository = repository

    def execute(self, slug: str) -> ScreenPage:
        normalized = normalize_screen_slug(slug)
        screen = SCREENS.get(normalized)
        if screen is None:
            return ScreenPage(screen=None, stocks=())
        return ScreenPage(
            screen=screen,
            stocks=self._repository.list_screen_stocks(
                screen.sort_key,
                descending=screen.descending,
                positive_only=screen.positive_only,
                limit=self.LIMIT,
            ),
        )


@dataclass(frozen=True)
class SitemapData:
    """Everything the sitemap lists: stock pages, ETF pages, sector pages, screen pages, and
    the dated market-brief pages (each a fresh, durable URL — compounding SEO)."""

    stock_pages: tuple[StockPageRef, ...]
    etf_pages: tuple[StockPageRef, ...]
    sector_slugs: tuple[str, ...]
    screen_slugs: tuple[str, ...]
    brief_dates: tuple[date, ...]


class GetSitemap:
    """Use case: the URLs for ``sitemap.xml`` — the index-worthy stock and ETF pages plus
    the sector and screen pages.

    Owns the per-file URL ceiling: a single sitemap file tops out at 50,000 URLs, so the
    per-list caps keep us under it (the universe is a few thousand today; when it approaches
    the limit this becomes a sitemap *index* of paginated children). Most-valuable-first
    ordering means a future truncation drops only the smallest names.
    """

    # The sitemaps.org per-file ceiling. Kept comfortably below in practice.
    MAX_URLS = 50_000
    # How far back the dated-brief pages are listed. A brief is written daily, so ~2 years of
    # them is a bounded, still-large set of compounding-SEO URLs (the freshest first, so a
    # future truncation drops only the oldest).
    MAX_BRIEF_PAGES = 730

    def __init__(self, repository: SeoReadRepository) -> None:
        self._repository = repository

    def execute(self) -> SitemapData:
        return SitemapData(
            stock_pages=self._repository.list_stock_pages(self.MAX_URLS),
            etf_pages=self._repository.list_etf_pages(self.MAX_URLS),
            sector_slugs=self._repository.list_sectors(),
            screen_slugs=tuple(SCREENS.keys()),
            brief_dates=self._repository.list_brief_dates(self.MAX_BRIEF_PAGES),
        )


@dataclass(frozen=True)
class CongressBoardPage:
    """What the /congress market-board content page renders: the most recent Congressional trades
    market-wide. Unlike a per-entity page this landing page always renders (it's a keyword page with
    substantial static explanation), so there's no ``has_data`` 404 gate — ``is_empty`` just lets
    the template show a "refreshed weekly" note before the sync has seeded the store."""

    trades: tuple[CongressPageTrade, ...]

    @property
    def is_empty(self) -> bool:
        return len(self.trades) == 0


class GetCongressBoardPage:
    """Use case: the /congress board's view — the most recent Congressional trades market-wide,
    from DB-only facts (a crawler hit is one indexed read, never a live fetch)."""

    # Enough to be a rich, link-dense page without an unbounded listing.
    LIMIT = 100

    def __init__(self, repository: SeoReadRepository) -> None:
        self._repository = repository

    def execute(self) -> CongressBoardPage:
        return CongressBoardPage(
            trades=self._repository.list_recent_congress_trades(self.LIMIT)
        )
