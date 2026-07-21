from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import seo_endpoints as endpoints
from app.stocks.seo.repository import CongressPageTrade, TickerPageFacts
from app.stocks.seo.use_cases import CongressBoardPage, TickerStockPage


def _trade(ticker="NVDA", member="Nancy Pelosi", chamber="House", tx_type="Purchase"):
    return CongressPageTrade(
        ticker=ticker,
        name=f"{ticker} Inc.",
        member=member,
        chamber=chamber,
        tx_type=tx_type,
        amount_range="$1,001 - $15,000",
        transaction_date=date(2026, 6, 20),
        disclosure_date=date(2026, 7, 2),
    )


class _FakeBoardUseCase:
    def __init__(self, page):
        self._page = page

    def execute(self):
        return self._page


def _board_client(page) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_congress_board_page_use_case] = lambda: _FakeBoardUseCase(page)
    return TestClient(app)


def test_congress_board_renders_ledger_and_seo_essentials():
    page = CongressBoardPage(
        trades=(
            _trade(member="Nancy Pelosi", tx_type="Purchase"),
            _trade(ticker="LMT", member="Tommy Tuberville", chamber="Senate", tx_type="Sale"),
        )
    )
    resp = _board_client(page).get("/congress")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "US Congress Stock Trades" in html
    assert "Nancy Pelosi" in html and "Tommy Tuberville" in html
    assert 'href="https://www.namainsights.com/stock/NVDA"' in html  # ticker links into the app
    assert 'rel="canonical"' in html and "/congress" in html
    assert "index,follow" in html
    assert '"@type": "Dataset"' in html and '"@type": "FAQPage"' in html
    # Buy/sell direction classes drive the ledger's left rule.
    assert 'class="buy"' in html and 'class="sell"' in html


def test_congress_board_empty_state_still_renders():
    resp = _board_client(CongressBoardPage(trades=())).get("/congress")
    assert resp.status_code == 200  # a landing page, not a 404
    assert "refreshed weekly" in resp.text.lower()


def test_congress_board_sets_cache_header():
    resp = _board_client(CongressBoardPage(trades=(_trade(),))).get("/congress")
    assert resp.headers["cache-control"] == "public, max-age=3600"


# --- the per-stock section on /stock/{ticker} --------------------------------------------


class _FakeTickerUseCase:
    def __init__(self, page):
        self._page = page

    def execute(self, ticker):
        return self._page


def _stock_client(page) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_ticker_stock_page_use_case] = lambda: _FakeTickerUseCase(page)
    return TestClient(app)


def _facts():
    return TickerPageFacts(
        name="NVIDIA Corporation",
        exchange="NASDAQ",
        sector="technology",
        industry="semiconductors",
        market_cap=3_200_000_000_000.0,
        pe_ratio=55.0,
        fcf_yield=1.8,
        revenue_growth_yoy=90.0,
        eps_growth_yoy=110.0,
        fcf_growth_yoy=80.0,
        in_sp500=True,
        in_nasdaq100=True,
    )


def test_stock_page_shows_congress_section_when_present():
    page = TickerStockPage(
        ticker="NVDA",
        facts=_facts(),
        congress=(_trade(member="Nancy Pelosi"), _trade(member="Tommy Tuberville", chamber="Senate", tx_type="Sale")),
    )
    html = _stock_client(page).get("/stock/NVDA").text
    assert "Recent Congressional trades" in html
    assert "Nancy Pelosi" in html and "Tommy Tuberville" in html
    assert 'href="https://www.namainsights.com/congress"' in html  # links to the board


def test_stock_page_hides_congress_section_when_absent():
    page = TickerStockPage(ticker="NVDA", facts=_facts(), congress=())
    html = _stock_client(page).get("/stock/NVDA").text
    assert "Recent Congressional trades" not in html
