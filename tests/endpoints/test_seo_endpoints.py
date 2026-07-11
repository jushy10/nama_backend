"""Tests for the SEO content-page endpoints (GET /stock/{ticker}).

Offline: a fake use case injected through ``dependency_overrides`` + FastAPI's TestClient,
so this checks the controller + presenter + template render — without a database. Asserts
the SEO essentials that make these pages worth shipping: a unique title/description, the
canonical + robots directives, the JSON-LD block, the visible facts, and the error mapping.
"""

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import seo_endpoints as endpoints
from app.stocks.seo.repository import (
    EtfPageFacts,
    SectorStock,
    StockPageRef,
    TickerPageFacts,
)
from app.stocks.seo.use_cases import (
    SCREENS,
    EtfPage,
    ScreenPage,
    SectorPage,
    SitemapData,
    TickerStockPage,
)


class _FakeUseCase:
    """Stands in for GetTickerStockPage; returns a canned page or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, ticker: str) -> TickerStockPage:
        self.calls.append(ticker)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_ticker_stock_page_use_case] = lambda: fake
    return TestClient(app)


def _screened_facts(**overrides) -> TickerPageFacts:
    base = dict(
        name="Micron Technology",
        exchange="NASDAQ",
        sector="technology",
        industry="semiconductors",
        market_cap=1_090_000_000_000.0,
        pe_ratio=22.4,
        fcf_yield=4.99,
        revenue_growth_yoy=61.6,
        eps_growth_yoy=587.4,
        fcf_growth_yoy=42.0,
        in_sp500=True,
        in_nasdaq100=True,
    )
    base.update(overrides)
    return TickerPageFacts(**base)


def _a_page(ticker: str = "MU", **fact_overrides) -> TickerStockPage:
    return TickerStockPage(ticker=ticker, facts=_screened_facts(**fact_overrides))


def test_screened_stock_renders_indexable_page() -> None:
    fake = _FakeUseCase(result=_a_page())
    resp = _client(fake).get("/stock/mu")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "public, max-age=3600"
    # The use case saw the raw path segment (normalization happens inside it).
    assert fake.calls == ["mu"]

    body = resp.text
    # Unique, fact-bearing title + description (not the SPA's one static title).
    assert "Micron Technology (MU) Stock" in body
    assert '<meta name="description"' in body
    # Canonical points at the public origin's singular /stock/ path.
    assert '<link rel="canonical" href="https://www.namainsights.com/stock/MU"' in body
    # Screened -> indexable.
    assert '<meta name="robots" content="index,follow"' in body
    # Structured data present, with the entity's ticker.
    assert 'application/ld+json' in body
    assert '"tickerSymbol": "MU"' in body
    assert '"@type": "Corporation"' in body
    # Visible facts a reader (and an AI extractor) can lift.
    assert "$1.09T" in body
    assert "5.0%" in body  # FCF yield 4.99 -> formatted to 1 decimal
    assert "S&amp;P 500" in body  # index-membership chip (HTML-escaped ampersand)


def test_unscreened_stock_renders_but_noindex() -> None:
    # A name-only, never-screened row: real enough to serve, too thin to index.
    page = TickerStockPage(
        ticker="ZZZZ",
        facts=_screened_facts(
            name="Some Micro Co",
            market_cap=None,
            pe_ratio=None,
            fcf_yield=None,
            revenue_growth_yoy=None,
            eps_growth_yoy=None,
            fcf_growth_yoy=None,
            in_sp500=False,
            in_nasdaq100=False,
        ),
    )
    resp = _client(_FakeUseCase(result=page)).get("/stock/ZZZZ")

    assert resp.status_code == 200
    assert '<meta name="robots" content="noindex,follow"' in resp.text


def test_unknown_symbol_is_404() -> None:
    # No anchor row at all -> nothing to show -> 404 (no soft 200s in the index).
    page = TickerStockPage(ticker="NOPE", facts=None)
    resp = _client(_FakeUseCase(result=page)).get("/stock/NOPE")
    assert resp.status_code == 404


def test_malformed_ticker_is_400() -> None:
    fake = _FakeUseCase(error=ValueError("'123' is not a valid ticker."))
    resp = _client(fake).get("/stock/123")
    assert resp.status_code == 400


# --- Crawler files -----------------------------------------------------------------------


class _FakeSitemap:
    """Stands in for GetSitemap; returns canned SitemapData."""

    def __init__(self, data) -> None:
        self._data = data

    def execute(self):
        return self._data


def test_robots_txt_welcomes_ai_crawlers_and_points_at_sitemap() -> None:
    resp = _client(_FakeUseCase(result=_a_page())).get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "User-agent: GPTBot" in body  # AI crawlers explicitly allowed
    assert "User-agent: ClaudeBot" in body
    assert "User-agent: PerplexityBot" in body
    assert "Sitemap: https://www.namainsights.com/sitemap.xml" in body


def test_llms_txt_served() -> None:
    resp = _client(_FakeUseCase(result=_a_page())).get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# Nama Insights" in resp.text


def test_sitemap_lists_stock_and_sector_pages() -> None:
    app = FastAPI()
    app.include_router(endpoints.router)
    data = SitemapData(
        stock_pages=(
            StockPageRef(ticker="MU", last_modified=date(2026, 7, 3)),
            StockPageRef(ticker="AAPL", last_modified=None),  # no stamp -> no <lastmod>
        ),
        etf_pages=(StockPageRef(ticker="VOO", last_modified=date(2026, 7, 5)),),
        sector_slugs=("technology", "consumer_electronics"),
        screen_slugs=("high-fcf-yield", "cheapest-pe"),
    )
    app.dependency_overrides[endpoints.get_sitemap_use_case] = lambda: _FakeSitemap(data)
    resp = TestClient(app).get("/sitemap.xml")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    body = resp.text
    assert "<urlset" in body
    assert "<loc>https://www.namainsights.com/stock/MU</loc>" in body
    assert "<lastmod>2026-07-03</lastmod>" in body
    # The stampless page still appears, just without a lastmod element.
    assert "<loc>https://www.namainsights.com/stock/AAPL</loc>" in body
    # Homepage is included.
    assert "<loc>https://www.namainsights.com/</loc>" in body
    # ETF, sector and screen pages are all listed.
    assert "<loc>https://www.namainsights.com/etf/VOO</loc>" in body
    assert "<loc>https://www.namainsights.com/sector/technology</loc>" in body
    # Stored underscore slug hyphenated for the sector URL.
    assert "<loc>https://www.namainsights.com/sector/consumer-electronics</loc>" in body
    assert "<loc>https://www.namainsights.com/screen/high-fcf-yield</loc>" in body


# --- Sector pages ------------------------------------------------------------------------


class _FakeSectorUseCase:
    """Stands in for GetSectorPage; returns a canned page or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, sector: str) -> SectorPage:
        self.calls.append(sector)
        if self._error is not None:
            raise self._error
        return self._result


def _sector_client(fake: _FakeSectorUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_sector_page_use_case] = lambda: fake
    return TestClient(app)


def test_sector_page_renders_linked_stock_listing() -> None:
    page = SectorPage(
        slug="consumer_electronics",
        stocks=(
            SectorStock(ticker="AAPL", name="Apple Inc.", market_cap=3.5e12, pe_ratio=31.2, fcf_yield=3.1),
            SectorStock(ticker="SONY", name="Sony Group", market_cap=1.2e11, pe_ratio=17.8, fcf_yield=4.6),
        ),
    )
    fake = _FakeSectorUseCase(result=page)
    resp = _sector_client(fake).get("/sector/consumer-electronics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # The raw hyphenated slug reaches the use case (normalization is inside it).
    assert fake.calls == ["consumer-electronics"]

    body = resp.text
    assert "Consumer Electronics Stocks" in body
    # Canonical uses the hyphenated slug.
    assert '<link rel="canonical" href="https://www.namainsights.com/sector/consumer-electronics"' in body
    assert '<meta name="robots" content="index,follow"' in body
    # Each stock links to its /stock/ page — the internal-linking hub.
    assert 'href="https://www.namainsights.com/stock/AAPL"' in body
    assert "Apple Inc." in body
    assert "$3.50T" in body
    # JSON-LD ItemList of the constituents.
    assert '"@type": "ItemList"' in body


def test_unknown_sector_is_404() -> None:
    # No stocks in the sector -> not a real sector -> 404.
    page = SectorPage(slug="nonsense", stocks=())
    resp = _sector_client(_FakeSectorUseCase(result=page)).get("/sector/nonsense")
    assert resp.status_code == 404


def test_malformed_sector_is_400() -> None:
    fake = _FakeSectorUseCase(error=ValueError("'a/b' is not a valid sector."))
    resp = _sector_client(fake).get("/sector/a b")
    assert resp.status_code == 400


# --- Screen ("best-of") pages ------------------------------------------------------------


class _FakeScreenUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error

    def execute(self, slug: str) -> ScreenPage:
        if self._error is not None:
            raise self._error
        return self._result


def _screen_client(fake: _FakeScreenUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_screen_page_use_case] = lambda: fake
    return TestClient(app)


def test_screen_page_renders_ranked_listing() -> None:
    page = ScreenPage(
        screen=SCREENS["high-fcf-yield"],
        stocks=(
            SectorStock(ticker="GM", name="General Motors", market_cap=6.0e10, pe_ratio=5.4, fcf_yield=18.2),
            SectorStock(ticker="F", name="Ford Motor", market_cap=4.5e10, pe_ratio=6.9, fcf_yield=14.0),
        ),
    )
    resp = _screen_client(_FakeScreenUseCase(result=page)).get("/screen/high-fcf-yield")

    assert resp.status_code == 200
    body = resp.text
    assert "Highest Free Cash Flow Yield" in body
    assert '<link rel="canonical" href="https://www.namainsights.com/screen/high-fcf-yield"' in body
    assert '<meta name="robots" content="index,follow"' in body
    assert 'href="https://www.namainsights.com/stock/GM"' in body
    assert '"@type": "ItemList"' in body


def test_unknown_screen_is_404() -> None:
    page = ScreenPage(screen=None, stocks=())
    resp = _screen_client(_FakeScreenUseCase(result=page)).get("/screen/not-a-screen")
    assert resp.status_code == 404


# --- ETF pages ---------------------------------------------------------------------------


class _FakeEtfUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, ticker: str) -> EtfPage:
        self.calls.append(ticker)
        if self._error is not None:
            raise self._error
        return self._result


def _etf_client(fake: _FakeEtfUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_etf_page_use_case] = lambda: fake
    return TestClient(app)


def _etf_facts(**overrides) -> EtfPageFacts:
    base = dict(
        name="Vanguard S&P 500 ETF",
        exchange="NYSE",
        category="large_blend",
        net_assets=1.3e12,
        expense_ratio=0.03,
        fund_family="Vanguard",
        dividend_yield=1.29,
        nav=512.34,
        description="The fund seeks to track the S&P 500 Index.",
    )
    base.update(overrides)
    return EtfPageFacts(**base)


def test_etf_page_renders_indexable() -> None:
    page = EtfPage(ticker="VOO", facts=_etf_facts())
    fake = _FakeEtfUseCase(result=page)
    resp = _etf_client(fake).get("/etf/voo")

    assert resp.status_code == 200
    assert fake.calls == ["voo"]
    body = resp.text
    assert "Vanguard S&amp;P 500 ETF (VOO) ETF" in body  # title (ampersand escaped)
    assert '<link rel="canonical" href="https://www.namainsights.com/etf/VOO"' in body
    assert '<meta name="robots" content="index,follow"' in body
    assert '"@type": "FinancialProduct"' in body
    assert "$1.30T" in body  # AUM
    assert "0.03%" in body  # expense ratio (2 decimals for the tiny ETF figures)


def test_unknown_etf_is_404() -> None:
    page = EtfPage(ticker="NOPE", facts=None)
    resp = _etf_client(_FakeEtfUseCase(result=page)).get("/etf/NOPE")
    assert resp.status_code == 404


def test_malformed_etf_ticker_is_400() -> None:
    fake = _FakeEtfUseCase(error=ValueError("'1' is not a valid ticker."))
    resp = _etf_client(fake).get("/etf/1")
    assert resp.status_code == 400
