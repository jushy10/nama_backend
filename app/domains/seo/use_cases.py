from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.domains.shared.entities import base_ticker, is_canadian
from app.domains.seo.interfaces import (
    CongressPageTrade,
    EtfPageFacts,
    SectorStock,
    SeoReadRepositoryAdapter,
    StockPageRef,
    TickerPageFacts,
)

# A ticker is 1–5 letters, optionally with a single class suffix (BRK-B, BF-B). Yahoo/the
# universe store a US class suffix with a hyphen, so a dotted input (BRK.B) is normalized to it.
# Deliberately a touch more permissive than the ticker card's alpha-only guard so the
# class-share names in the universe still get a page.
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")


def normalize_ticker(raw: str) -> str:
    text = (raw or "").strip().upper()
    if not text:
        raise ValueError("A ticker is required.")
    if is_canadian(text):
        base = base_ticker(text)
        if not base.isalpha() or len(base) > 5:
            raise ValueError(f"'{raw}' is not a valid ticker.")
        return text  # keep the Canadian suffix — the universe stores SHOP.TO, not SHOP-TO
    ticker = text.replace(".", "-")
    if not _TICKER_RE.match(ticker):
        raise ValueError(f"'{raw}' is not a valid ticker.")
    return ticker


@dataclass(frozen=True)
class TickerStockPage:
    ticker: str
    facts: TickerPageFacts | None
    congress: tuple[CongressPageTrade, ...] = ()

    @property
    def has_data(self) -> bool:
        return self.facts is not None and (
            self.facts.name is not None or self.facts.market_cap is not None
        )

    @property
    def indexable(self) -> bool:
        return self.facts is not None and self.facts.market_cap is not None

    @property
    def display_name(self) -> str:
        if self.facts is not None and self.facts.name:
            return self.facts.name
        return self.ticker


class GetTickerStockPage:
    # A handful of recent trades — enough to make the section substantive without turning the page
    # into a full ledger (the /congress board and the app carry the complete feed).
    CONGRESS_LIMIT = 12

    def __init__(self, repository: SeoReadRepositoryAdapter) -> None:
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
    slug = (raw or "").strip().lower().replace("-", "_")
    if not slug:
        raise ValueError("A sector is required.")
    if not re.match(r"^[a-z0-9_]+$", slug):
        raise ValueError(f"'{raw}' is not a valid sector.")
    return slug


@dataclass(frozen=True)
class SectorPage:
    slug: str  # stored snake_case form
    stocks: tuple[SectorStock, ...]

    @property
    def has_data(self) -> bool:
        return len(self.stocks) > 0

    @property
    def url_slug(self) -> str:
        return self.slug.replace("_", "-")

    @property
    def label(self) -> str:
        return self.slug.replace("_", " ").title()


class GetSectorPage:
    # Enough to be a rich, link-dense page without an unbounded listing.
    LIMIT = 100

    def __init__(self, repository: SeoReadRepositoryAdapter) -> None:
        self._repository = repository

    def execute(self, sector: str) -> SectorPage:
        slug = normalize_sector_slug(sector)
        return SectorPage(
            slug=slug,
            stocks=self._repository.list_sector_stocks(slug, self.LIMIT),
        )


@dataclass(frozen=True)
class EtfPage:
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
    def __init__(self, repository: SeoReadRepositoryAdapter) -> None:
        self._repository = repository

    def execute(self, ticker: str) -> EtfPage:
        normalized = normalize_ticker(ticker)
        return EtfPage(ticker=normalized, facts=self._repository.get_etf_facts(normalized))


@dataclass(frozen=True)
class ScreenDef:
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
    slug = (raw or "").strip().lower()
    if not slug:
        raise ValueError("A screen is required.")
    if not re.match(r"^[a-z0-9-]+$", slug):
        raise ValueError(f"'{raw}' is not a valid screen.")
    return slug


@dataclass(frozen=True)
class ScreenPage:
    screen: ScreenDef | None
    stocks: tuple[SectorStock, ...]

    @property
    def has_data(self) -> bool:
        return self.screen is not None and len(self.stocks) > 0


class GetScreenPage:
    LIMIT = 100

    def __init__(self, repository: SeoReadRepositoryAdapter) -> None:
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
    stock_pages: tuple[StockPageRef, ...]
    etf_pages: tuple[StockPageRef, ...]
    sector_slugs: tuple[str, ...]
    screen_slugs: tuple[str, ...]
    brief_dates: tuple[date, ...]


class GetSitemap:
    # The sitemaps.org per-file ceiling. Kept comfortably below in practice.
    MAX_URLS = 50_000
    # How far back the dated-brief pages are listed. A brief is written daily, so ~2 years of
    # them is a bounded, still-large set of compounding-SEO URLs (the freshest first, so a
    # future truncation drops only the oldest).
    MAX_BRIEF_PAGES = 730

    def __init__(self, repository: SeoReadRepositoryAdapter) -> None:
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
    trades: tuple[CongressPageTrade, ...]

    @property
    def is_empty(self) -> bool:
        return len(self.trades) == 0


class GetCongressBoardPage:
    # Enough to be a rich, link-dense page without an unbounded listing.
    LIMIT = 100

    def __init__(self, repository: SeoReadRepositoryAdapter) -> None:
        self._repository = repository

    def execute(self) -> CongressBoardPage:
        return CongressBoardPage(
            trades=self._repository.list_recent_congress_trades(self.LIMIT)
        )
