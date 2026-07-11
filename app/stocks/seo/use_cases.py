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

from app.stocks.seo.repository import (
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
    """What the stock content page needs to render: the normalized ticker and its stored
    facts (``None`` when the symbol is unknown to us)."""

    ticker: str
    facts: TickerPageFacts | None

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
    """Use case: assemble a stock's content-page view from DB-only facts."""

    def __init__(self, repository: SeoReadRepository) -> None:
        self._repository = repository

    def execute(self, ticker: str) -> TickerStockPage:
        normalized = normalize_ticker(ticker)
        return TickerStockPage(
            ticker=normalized,
            facts=self._repository.get_ticker_facts(normalized),
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
class SitemapData:
    """Everything the sitemap lists: the stock pages and the sector pages."""

    stock_pages: tuple[StockPageRef, ...]
    sector_slugs: tuple[str, ...]


class GetSitemap:
    """Use case: the URLs for ``sitemap.xml`` — the index-worthy stock pages plus the
    sector pages.

    Owns the per-file URL ceiling: a single sitemap file tops out at 50,000 URLs, so the
    stock cap keeps us under it (the universe is a few thousand today; when it approaches
    the limit this becomes a sitemap *index* of paginated children). Most-valuable-first
    ordering means a future truncation drops only the smallest names.
    """

    # The sitemaps.org per-file ceiling. Kept comfortably below in practice.
    MAX_URLS = 50_000

    def __init__(self, repository: SeoReadRepository) -> None:
        self._repository = repository

    def execute(self) -> SitemapData:
        return SitemapData(
            stock_pages=self._repository.list_stock_pages(self.MAX_URLS),
            sector_slugs=self._repository.list_sectors(),
        )
