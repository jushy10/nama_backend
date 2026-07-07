"""Tests for the ticker endpoints module (GET /stocks/ticker/{ticker}, GET /stocks/ticker,
GET /stocks/classifications).

Offline: fake use cases injected through dependency_overrides + FastAPI's TestClient, so this
checks only the controllers + presenters — without touching Alpaca, Finnhub, or the database.
Two groups, since this module hosts both the ticker card and the universe read side that
shares the ``/stocks/ticker`` resource:

- the card (``GET /stocks/ticker/{ticker}``): the JSON shape (symbol renamed to ``ticker``,
  the day move, the opt-in ``dividend``/``performance``/``metrics`` blocks with the
  ``1w``/``1m`` performance aliases), the include pass-through, the cache header,
  unrequested/unavailable blocks as nulls (not a 404), and the error mapping;
- the search + filter menus (``GET /stocks/ticker`` / ``GET /stocks/classifications``): the
  JSON shape, the query-param → use-case pass-through, FastAPI's enum/bounds validation (422),
  the ValueError → 400 mapping, and the cache headers.
"""

from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import ticker_endpoints as endpoints
from app.stocks.entities import (
    KeyMetrics,
    Quote,
    StockFundamentals,
    StockPerformance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ticker.entities import TickerOptionsMetrics, TickerValuation
from app.stocks.ticker.use_cases import TickerCard, TickerClassification
from app.stocks.universe.entities import (
    Classifications,
    MarketCapTier,
    SortDirection,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)


class _FakeUseCase:
    """Stands in for GetTickerCard; returns a canned card or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, list[str] | None]] = []

    def execute(self, symbol: str, include=None) -> TickerCard:
        self.calls.append((symbol, include))
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_ticker_card_use_case] = lambda: fake
    return TestClient(app)


def _a_card(
    *, include: frozenset[str] = frozenset(), asset_type: str = "equity"
) -> TickerCard:
    """A canned card; the opt-in blocks are populated only when in ``include``,
    the way the use case builds it."""
    return TickerCard(
        quote=Quote(
            symbol="MU",
            price=975.56,
            previous_close=963.26,
            bid=None,
            ask=None,
            as_of=datetime(2026, 7, 3, tzinfo=timezone.utc),
        ),
        include=include,
        asset_type=asset_type,
        valuation=(
            TickerValuation(
                symbol="MU",
                price=975.56,
                forward_pe=13.3,
                forward_eps_growth=104.1,
                # Consensus-basis TTM: trailing_pe = 975.56 / 43.55 -> 22.4.
                ttm_eps=43.55,
            )
            if "metrics" in include
            else None
        ),
        name="Micron Technology",
        # Market cap now rides the anchor (card.market_cap), so the fundamentals'
        # own market_cap is a wrong-answer sentinel: the presenter must ignore it.
        market_cap=1_090_000_000_000.0,
        sector="technology",
        industry="semiconductors",
        revenue_growth_yoy=61.6,
        eps_growth_yoy=587.4,
        fundamentals=StockFundamentals(
            market_cap=999.0,  # sentinel: presenter reads card.market_cap, not this
            # Vendor-noisy on purpose: the presenter must round both to 2 decimals.
            dividend_per_share=0.4649,
            dividend_yield=0.047123,
            metrics=KeyMetrics(
                pe=22.4,
                eps_growth_yoy=700.7,  # peg property -> 0.03
                gross_margin=52.1,
                operating_margin=38.9,
                net_margin=33.5,
            ),
        ),
        performance=(
            StockPerformance(
                one_week=1.5, one_month=8.0, three_month=40.0, six_month=90.0,
                ytd=120.0, one_year=150.0,
            )
            if "performance" in include
            else None
        ),
        exchange="NASDAQ",
        options_metrics=(
            TickerOptionsMetrics(
                # Chain-arithmetic-noisy on purpose: the presenter must round the
                # figures (not the dates) to 2 decimals.
                implied_volatility=26.000000000000004,
                expected_move_percent=5.0049999,
                expected_move_by=date(2026, 7, 31),
                insurance_cost_percent=4.0012,
                insurance_expires=date(2026, 10, 2),
                put_call_ratio=1.2004,
            )
            if "options_metrics" in include
            else None
        ),
    )


def test_presents_the_core_card_with_null_optin_blocks_by_default():
    fake = _FakeUseCase(result=_a_card())
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticker"] == "MU"  # the symbol, in this endpoint's vocabulary
    assert body["name"] == "Micron Technology"  # profile vendor's clean display name
    assert body["exchange"] == "NASDAQ"  # DB-backed, always served
    assert body["asset_type"] == "equity"  # always present; a stock here
    assert body["price"] == 975.56
    assert body["change"] == 12.3  # vs the previous close, same rule as /quote
    assert body["change_percent"] == 1.28
    assert body["market_cap"] == 1_090_000_000_000.0  # off the anchor, not fundamentals
    assert body["sector"] == "technology"  # universe-screen fact off the anchor
    assert body["industry"] == "semiconductors"
    # Opt-in blocks stay null until requested (and the fundamentals call that backs
    # some of them isn't even made for a bare card now that market cap is DB-sourced).
    assert body["dividend"] is None
    assert body["performance"] is None
    assert body["metrics"] is None
    assert body["options_metrics"] is None
    assert fake.calls == [("MU", None)]


def test_asset_type_is_etf_for_a_fund():
    # A ticker in the ETF universe carries asset_type "etf" (the presenter passes the card's
    # value straight through); always present and non-null.
    fake = _FakeUseCase(result=_a_card(asset_type="etf"))
    resp = _client(fake).get("/stocks/ticker/VOO")
    assert resp.status_code == 200, resp.text
    assert resp.json()["asset_type"] == "etf"


def test_presents_the_optin_blocks_when_included():
    fake = _FakeUseCase(
        result=_a_card(
            include=frozenset(
                {"dividend", "performance", "metrics", "options_metrics"}
            )
        )
    )
    resp = _client(fake).get(
        "/stocks/ticker/MU?include=dividend&include=performance&include=metrics"
        "&include=options_metrics"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dividend"] == {"yield_percentage": 0.05, "per_share": 0.46}  # rounded
    # Performance keeps the finance-style aliases the snapshot uses.
    assert body["performance"] == {
        "1w": 1.5, "1m": 8.0, "3m": 40.0, "6m": 90.0, "ytd": 120.0, "1y": 150.0,
    }
    # The trailing P/E rides the valuation's consensus-basis TTM (not the
    # vendor's KeyMetrics.pe); PEG + margins ride the fundamentals; forward PEG
    # the stored consensus.
    assert body["metrics"] == {
        "pe": 22.4,  # 975.56 / 43.55 — the valuation's trailing_pe
        "peg": 0.03,  # 22.4 / 700.7 — the degenerate trailing read, for contrast
        "forward_peg": 0.13,
        "gross_margin": 52.1,
        "operating_margin": 38.9,
        "net_margin": 33.5,
        "revenue_growth_yoy": 61.6,  # off the anchor, alongside the forward legs
        "eps_growth_yoy": 587.4,
    }
    # The options figures are rounded at the edge; the sampled expiries are dates.
    assert body["options_metrics"] == {
        "implied_volatility": 26.0,
        "expected_move_percent": 5.0,
        "expected_move_by": "2026-07-31",
        "insurance_cost_percent": 4.0,
        "insurance_expires": "2026-10-02",
        "put_call_ratio": 1.2,
    }


def test_passes_the_raw_include_params_through_to_the_use_case():
    # Comma-separated values arrive as one raw param; splitting/validating is the
    # use case's job (it owns the vocabulary), not the controller's.
    fake = _FakeUseCase(result=_a_card())
    _client(fake).get("/stocks/ticker/MU?include=dividend,metrics")
    assert fake.calls == [("MU", ["dividend,metrics"])]


def test_blocks_requested_but_fundamentals_unavailable_degrade_to_nulls():
    card = _a_card(include=frozenset({"dividend", "metrics"}))
    fake = _FakeUseCase(
        result=TickerCard(
            quote=card.quote,
            include=card.include,
            valuation=card.valuation,  # the consensus half still serves
            fundamentals=None,  # keyless or failed Finnhub
            performance=None,
            name=None,
            exchange=None,
            # market_cap unset -> null (no anchor row); the growth pair, also off
            # the anchor, still serves — it never rode Finnhub.
            revenue_growth_yoy=61.6,
            eps_growth_yoy=587.4,
        )
    )
    resp = _client(fake).get("/stocks/ticker/MU?include=dividend,metrics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] is None
    assert body["exchange"] is None
    assert body["market_cap"] is None
    assert body["dividend"] is None  # requested, but nothing to serve
    # The metrics block still appears (it was requested) with its
    # fundamentals-backed half null; the valuation-backed pair — the trailing
    # P/E (quarterly TTM) and the forward PEG (stored consensus) — and the
    # anchor-backed growth pair still serve, since none of them ride Finnhub.
    assert body["metrics"] == {
        "pe": 22.4,
        "peg": None,
        "forward_peg": 0.13,
        "gross_margin": None,
        "operating_margin": None,
        "net_margin": None,
        "revenue_growth_yoy": 61.6,
        "eps_growth_yoy": 587.4,
    }


def test_options_metrics_requested_but_unavailable_is_null():
    # A Yahoo-blocked chain read leaves the block null — a 200, never an error.
    card = _a_card()
    fake = _FakeUseCase(
        result=TickerCard(
            quote=card.quote,
            include=frozenset({"options_metrics"}),
            valuation=None,
            fundamentals=card.fundamentals,
            performance=None,
            name=card.name,
            exchange=card.exchange,
            options_metrics=None,
        )
    )
    resp = _client(fake).get("/stocks/ticker/MU?include=options_metrics")
    assert resp.status_code == 200, resp.text
    assert resp.json()["options_metrics"] is None


def test_sets_the_cache_header():
    fake = _FakeUseCase(result=_a_card())
    resp = _client(fake).get("/stocks/ticker/MU")
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_bad_symbol_or_include_is_a_400():
    fake = _FakeUseCase(error=ValueError("Unknown include(s): earnings."))
    assert _client(fake).get("/stocks/ticker/MU?include=earnings").status_code == 400


def test_unknown_symbol_is_a_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ticker/ZZZZ").status_code == 404


def test_upstream_failure_is_a_502():
    fake = _FakeUseCase(error=StockDataUnavailable("MU", "boom"))
    assert _client(fake).get("/stocks/ticker/MU").status_code == 502


# --- The lightweight type classifier (GET /stocks/type/{ticker}) -------------------


class _FakeClassify:
    """Stands in for ClassifyTicker; echoes a canned classification or raises."""

    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def classify(self, symbol: str) -> TickerClassification:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _type_client(fake: _FakeClassify) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_classify_ticker_use_case] = lambda: fake
    return TestClient(app)


def test_type_endpoint_classifies_an_etf():
    fake = _FakeClassify(result=TickerClassification(ticker="VOO", asset_type="etf"))

    res = _type_client(fake).get("/stocks/type/voo")

    assert res.status_code == 200
    assert res.json() == {"ticker": "VOO", "asset_type": "etf"}
    # The raw path symbol reaches the use case (which normalizes it).
    assert fake.calls == ["voo"]
    assert res.headers["Cache-Control"] == "public, max-age=3600"


def test_type_endpoint_classifies_an_equity():
    fake = _FakeClassify(result=TickerClassification(ticker="AAPL", asset_type="equity"))

    res = _type_client(fake).get("/stocks/type/AAPL")

    assert res.status_code == 200
    assert res.json() == {"ticker": "AAPL", "asset_type": "equity"}


def test_type_endpoint_bad_symbol_is_a_400():
    fake = _FakeClassify(error=ValueError("A stock symbol is required."))

    assert _type_client(fake).get("/stocks/type/123").status_code == 400


# --- The universe search + filter menus (GET /stocks/ticker, GET /stocks/classifications) ---


class _FakeSearch:
    """Stands in for SearchStocks; records the kwargs it was called with, returns a page."""

    def __init__(self, *, page=None, error=None) -> None:
        self._page = page
        self._error = error
        self.kwargs: dict | None = None

    def execute(self, **kwargs):
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._page


class _FakeClassifications:
    """Stands in for ListClassifications; returns a canned Classifications."""

    def __init__(self, result: Classifications) -> None:
        self._result = result

    def execute(self) -> Classifications:
        return self._result


def _search_client(*, search=None, classifications=None) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    if search is not None:
        app.dependency_overrides[endpoints.get_search_use_case] = lambda: search
    if classifications is not None:
        app.dependency_overrides[endpoints.get_classifications_use_case] = (
            lambda: classifications
        )
    return TestClient(app)


def _a_page() -> StockSearchPage:
    return StockSearchPage(
        results=(
            StockSearchResult(
                ticker="NVDA",
                name="Nvidia",
                sector="technology",
                industry="semiconductors",
                market_cap=3e12,
                pe_ratio=48.2,
                revenue_growth_yoy=61.6,
                eps_growth_yoy=587.4,
                forward_revenue_growth_yoy=52.1,
                forward_eps_growth_yoy=48.3,
                in_sp500=True,
                in_nasdaq100=True,
            ),
        ),
        total=1,
        limit=25,
        offset=0,
    )


def test_search_returns_the_expected_json_shape():
    resp = _search_client(search=_FakeSearch(page=_a_page())).get("/stocks/ticker")

    assert resp.status_code == 200
    body = resp.json()
    assert (body["total"], body["limit"], body["offset"], body["count"]) == (1, 25, 0, 1)
    (row,) = body["results"]
    assert row == {
        "ticker": "NVDA",
        "name": "Nvidia",
        "sector": "technology",
        "industry": "semiconductors",
        "market_cap": 3e12,
        "pe_ratio": 48.2,
        "revenue_growth_yoy": 61.6,
        "eps_growth_yoy": 587.4,
        "forward_revenue_growth_yoy": 52.1,
        "forward_eps_growth_yoy": 48.3,
        "in_sp500": True,
        "in_nasdaq100": True,
    }


def test_search_passes_query_params_through_to_the_use_case():
    fake = _FakeSearch(page=_a_page())
    resp = _search_client(search=fake).get(
        "/stocks/ticker",
        params={
            "q": "nv",
            "sector": "Technology",  # raw — the use case (not the endpoint) slugs it
            "industry": "semiconductors",
            "in_sp500": "true",
            "in_nasdaq100": "false",
            "market_cap": "large",
            "sort": "revenue_growth",
            "order": "asc",
            "limit": "10",
            "offset": "20",
        },
    )

    assert resp.status_code == 200
    assert fake.kwargs == {
        "query": "nv",
        "sector": "Technology",
        "industry": "semiconductors",
        "in_sp500": True,
        "in_nasdaq100": False,
        "market_cap_tier": MarketCapTier.LARGE,
        "sort": StockSort.REVENUE_GROWTH,
        "direction": SortDirection.ASC,
        "limit": 10,
        "offset": 20,
    }


def test_search_uses_defaults_when_no_params_given():
    fake = _FakeSearch(page=_a_page())
    _search_client(search=fake).get("/stocks/ticker")

    assert fake.kwargs == {
        "query": None,
        "sector": None,
        "industry": None,
        "in_sp500": None,
        "in_nasdaq100": None,
        "market_cap_tier": None,
        # No ?sort= => no sort (the use case orders an unsorted browse by ticker); the
        # direction default rides along unused until a sort is chosen.
        "sort": None,
        "direction": SortDirection.DESC,
        "limit": 25,
        "offset": 0,
    }


def test_search_accepts_the_growth_blend_sort():
    # The combined EPS+revenue blend binds to StockSort.GROWTH like the other sort values.
    fake = _FakeSearch(page=_a_page())
    resp = _search_client(search=fake).get("/stocks/ticker", params={"sort": "growth"})

    assert resp.status_code == 200
    assert fake.kwargs["sort"] is StockSort.GROWTH


def test_search_accepts_the_pe_sort():
    # The trailing-P/E sort binds to StockSort.PE like the other sort values.
    fake = _FakeSearch(page=_a_page())
    resp = _search_client(search=fake).get("/stocks/ticker", params={"sort": "pe"})

    assert resp.status_code == 200
    assert fake.kwargs["sort"] is StockSort.PE


@pytest.mark.parametrize(
    "value, expected",
    [
        ("forward_revenue_growth", StockSort.FORWARD_REVENUE_GROWTH),
        ("forward_eps_growth", StockSort.FORWARD_EPS_GROWTH),
        ("forward_growth", StockSort.FORWARD_GROWTH),
    ],
)
def test_search_accepts_the_forward_growth_sorts(value, expected):
    # The forward (FY1->FY2 consensus) sort values bind like the trailing ones.
    fake = _FakeSearch(page=_a_page())
    resp = _search_client(search=fake).get("/stocks/ticker", params={"sort": value})

    assert resp.status_code == 200
    assert fake.kwargs["sort"] is expected


@pytest.mark.parametrize(
    "param, value",
    [("sort", "bogus"), ("order", "sideways"), ("market_cap", "humongous")],
)
def test_search_rejects_an_unknown_enum_value(param, value):
    resp = _search_client(search=_FakeSearch(page=_a_page())).get(
        "/stocks/ticker", params={param: value}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("limit", [0, -1, 101, 9999])
def test_search_rejects_an_out_of_range_limit(limit):
    resp = _search_client(search=_FakeSearch(page=_a_page())).get(
        "/stocks/ticker", params={"limit": limit}
    )
    assert resp.status_code == 422


def test_search_rejects_a_negative_offset():
    resp = _search_client(search=_FakeSearch(page=_a_page())).get(
        "/stocks/ticker", params={"offset": -1}
    )
    assert resp.status_code == 422


def test_search_maps_a_value_error_to_400():
    fake = _FakeSearch(error=ValueError("bad filter"))
    resp = _search_client(search=fake).get("/stocks/ticker")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad filter"


def test_search_sets_a_short_cache_header():
    resp = _search_client(search=_FakeSearch(page=_a_page())).get("/stocks/ticker")
    assert resp.headers["cache-control"] == "public, max-age=60"


def test_classifications_returns_the_expected_json_shape():
    fake = _FakeClassifications(
        Classifications(("energy", "technology"), ("oil_gas", "semiconductors"))
    )
    resp = _search_client(classifications=fake).get("/stocks/classifications")

    assert resp.status_code == 200
    assert resp.json() == {
        "sectors": ["energy", "technology"],
        "industries": ["oil_gas", "semiconductors"],
    }


def test_classifications_sets_a_longer_cache_header():
    fake = _FakeClassifications(Classifications((), ()))
    resp = _search_client(classifications=fake).get("/stocks/classifications")

    assert resp.headers["cache-control"] == "public, max-age=300"
