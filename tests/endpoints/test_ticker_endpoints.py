from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.endpoints import ticker_endpoints as endpoints
from app.stocks.entities import (
    Quote,
    StockPerformance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ticker.entities import TickerOptionsMetrics, TickerValuation
from app.stocks.ticker.use_cases import TickerCard, TickerClassification
from app.stocks.universe.entities import (
    Classifications,
    IndustryValuation,
    MarketCapTier,
    PeerCompany,
    PeerComparison,
    PeerMedians,
    ScreenIntent,
    SortDirection,
    StockSearchPage,
    StockSearchResult,
    StockSort,
)


class _FakeUseCase:
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
                # Consensus-basis TTM: trailing_pe = 975.56 / 43.55 -> 22.4.
                ttm_eps=43.55,
                # Per-share cash off the anchor -> price_to_fcf = 975.56 / 48.7 -> 20.03,
                # fcf_yield = 48.7 / 975.56 * 100 -> 4.99; ocf_yield = 60 / 975.56 * 100 -> 6.15.
                fcf_per_share=48.7,
                ocf_per_share=60.0,
                # Per-share book value / sales off the anchor -> pb = 975.56 / 65 -> 15.01,
                # ps = 975.56 / 45 -> 21.68; eps_growth feeds peg = 22.4 / 587.4 -> 0.04.
                book_value_per_share=65.0,
                sales_per_share=45.0,
                eps_growth_yoy=587.4,
                # EV inputs off the anchor, priced live: enterprise_value = 975.56 * 1e9 +
                # 13e9 - 5e9 = 983_560_000_000; ev_ebitda = 983.56e9 / 40e9 -> 24.59.
                shares_outstanding=1_000_000_000.0,
                total_debt=13_000_000_000.0,
                cash_and_equivalents=5_000_000_000.0,
                ebitda=40_000_000_000.0,
            )
            if "metrics" in include
            else None
        ),
        name="Micron Technology",
        # Market cap, ratios and the dividend all ride the anchor read now.
        market_cap=1_090_000_000_000.0,
        sector="technology",
        industry="semiconductors",
        revenue_growth_yoy=61.6,
        eps_growth_yoy=587.4,
        fcf_growth_yoy=42.0,  # off the anchor (annual slice), like the growth pair
        forward_revenue_growth_yoy=25.0,  # forward consensus off the anchor
        forward_eps_growth_yoy=30.0,
        # The trailing ratios ride the anchor (fundamentals slice) and the presenter serves
        # them directly. The dividend per share is vendor-noisy on purpose (0.4649): the
        # presenter rounds it to 0.46 and prices the yield off the live quote (0.4649 / 975.56
        # * 100 -> 0.05), replacing the retired stored dividend_yield.
        gross_margin=52.1,
        operating_margin=38.9,
        net_margin=33.5,
        roe=40.0,
        current_ratio=2.5,
        debt_to_equity=0.3,
        beta=1.24,
        dividend_per_share=0.4649,
        # Forward multiples priced off the stored forward consensus (set directly on the fake
        # card the way the use case computes them from the estimates read).
        forward_pe=18.0,
        forward_ps=8.0,
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
    assert body["change"] == 12.3  # vs the previous close, same rule as every price view
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
    # The default card's quote carries no regular close (and its timestamp is out of session),
    # so the extended-hours split is absent.
    assert body["extended_hours"] is None
    assert fake.calls == [("MU", None)]


def test_presents_the_extended_hours_split_outside_the_regular_session():
    # A quote whose latest print is after-hours (16:33 ET Friday) with a regular close to
    # anchor against: the presenter emits the two-part split so the FE can show the day's move
    # and the after-bell move apart, while the top-level price/change stay the blended figures.
    card = replace(
        _a_card(),
        quote=Quote(
            symbol="MU",
            price=980.00,
            previous_close=963.26,
            bid=None,
            ask=None,
            as_of=datetime(2026, 7, 17, 20, 33, tzinfo=timezone.utc),
            regular_close=975.56,
        ),
    )
    resp = _client(_FakeUseCase(result=card)).get("/stocks/ticker/MU")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Top-level stays the latest (extended) print and its blended move — unchanged contract.
    assert body["price"] == 980.0
    assert body["change"] == 16.74  # 980 - 963.26, off the after-hours print
    eh = body["extended_hours"]
    assert eh is not None
    assert eh["session"] == "after_hours"
    assert eh["price"] == 980.0
    assert eh["change"] == 4.44  # the after-bell move: print vs the regular close
    assert eh["change_percent"] == 0.46
    assert eh["regular_price"] == 975.56  # the anchor a client shows as the primary number
    assert eh["regular_change"] == 12.3  # the day's move: regular close vs previous close
    assert eh["regular_change_percent"] == 1.28
    assert eh["as_of"].startswith("2026-07-17T20:33")


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
    # The price-anchored multiples (P/E, P/B, P/S, PEG, FCF/OCF) ride the valuation at the
    # live quote; the forward multiples come off the stored forward consensus; the trailing
    # ratios and the growth (trailing trio + forward pair) ride the same anchor read.
    assert body["metrics"] == {
        "pe": 22.4,  # 975.56 / 43.55 — the valuation's trailing_pe
        "pb": 15.01,  # 975.56 / 65
        "ps": 21.68,  # 975.56 / 45
        "peg": 0.04,  # 22.4 / 587.4
        "eps": 43.55,  # the TTM EPS the P/E divides by
        "forward_pe": 18.0,
        "forward_ps": 8.0,
        "enterprise_value": 983_560_000_000.0,  # 975.56 * 1e9 + 13e9 - 5e9, raw USD
        "ev_ebitda": 24.59,  # 983.56e9 / 40e9
        "price_to_fcf": 20.03,  # 975.56 / 48.7
        "fcf_yield": 4.99,  # 48.7 / 975.56 * 100
        "ocf_yield": 6.15,  # 60 / 975.56 * 100
        "gross_margin": 52.1,
        "operating_margin": 38.9,
        "net_margin": 33.5,
        "roe": 40.0,
        "current_ratio": 2.5,
        "debt_to_equity": 0.3,
        "beta": 1.24,
        "revenue_growth_yoy": 61.6,  # off the anchor
        "eps_growth_yoy": 587.4,
        "fcf_growth_yoy": 42.0,  # off the anchor
        "forward_revenue_growth_yoy": 25.0,
        "forward_eps_growth_yoy": 30.0,
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


def test_blocks_requested_but_anchor_unsynced_degrade_to_nulls():
    # An unsynced anchor (no stored margins/dividend/cash) leaves the requested blocks'
    # fields null rather than sinking the card. The valuation carries the consensus-basis
    # TTM but no per-share cash, so the P/E serves while the FCF/OCF reads null out.
    card = _a_card(include=frozenset({"dividend", "metrics"}))
    fake = _FakeUseCase(
        result=TickerCard(
            quote=card.quote,
            include=card.include,
            valuation=TickerValuation(symbol="MU", price=975.56, ttm_eps=43.55),
            performance=None,
            name=None,
            exchange=None,
            # market_cap unset -> null (no anchor row); the growth pair, also off the
            # anchor, still serves. Margins + dividend_per_share default to null too.
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
    # The dividend block appears (it was requested) with null fields — nothing on the
    # anchor to serve or to price a yield from.
    assert body["dividend"] == {"yield_percentage": None, "per_share": None}
    # The metrics block still appears (it was requested) with its anchor-backed ratios
    # null; the FCF/OCF/P-B/P-S reads and PEG null out because this hand-built valuation
    # carries no per-share inputs or growth (an uncovered anchor), and the forward
    # multiples/growth null with no estimates. The valuation-backed trailing P/E (quarterly
    # TTM) with its EPS and the anchor-backed trailing growth pair still serve.
    assert body["metrics"] == {
        "pe": 22.4,
        "pb": None,
        "ps": None,
        "peg": None,
        "eps": 43.55,
        "forward_pe": None,
        "forward_ps": None,
        "enterprise_value": None,  # no shares_outstanding on the unsynced anchor
        "ev_ebitda": None,
        "price_to_fcf": None,
        "fcf_yield": None,
        "ocf_yield": None,
        "gross_margin": None,
        "operating_margin": None,
        "net_margin": None,
        "roe": None,
        "current_ratio": None,
        "debt_to_equity": None,
        "beta": None,
        "revenue_growth_yoy": 61.6,
        "eps_growth_yoy": 587.4,
        "fcf_growth_yoy": None,
        "forward_revenue_growth_yoy": None,
        "forward_eps_growth_yoy": None,
    }


def test_options_metrics_requested_but_unavailable_is_null():
    # A Yahoo-blocked chain read leaves the block null — a 200, never an error.
    card = _a_card()
    fake = _FakeUseCase(
        result=TickerCard(
            quote=card.quote,
            include=frozenset({"options_metrics"}),
            valuation=None,
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
    def __init__(self, result: Classifications) -> None:
        self._result = result

    def execute(self) -> Classifications:
        return self._result


class _FakeIndustryValuation:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.industry: str | None = None

    def execute(self, industry):
        self.industry = industry
        if self._error is not None:
            raise self._error
        return self._result


def _search_client(
    *, search=None, classifications=None, industry_valuation=None
) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    if search is not None:
        app.dependency_overrides[endpoints.get_search_use_case] = lambda: search
    if classifications is not None:
        app.dependency_overrides[endpoints.get_classifications_use_case] = (
            lambda: classifications
        )
    if industry_valuation is not None:
        app.dependency_overrides[endpoints.get_industry_valuation_use_case] = (
            lambda: industry_valuation
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
                fcf_yield=1.9,
                ev_ebitda=27.4,
                revenue_growth_yoy=61.6,
                eps_growth_yoy=587.4,
                fcf_growth_yoy=60.8,
                forward_revenue_growth_yoy=52.1,
                forward_eps_growth_yoy=48.3,
                in_sp500=True,
                in_nasdaq100=True,
                country="US",
                currency="USD",
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
        "fcf_yield": 1.9,
        "ev_ebitda": 27.4,
        "revenue_growth_yoy": 61.6,
        "eps_growth_yoy": 587.4,
        "fcf_growth_yoy": 60.8,
        "forward_revenue_growth_yoy": 52.1,
        "forward_eps_growth_yoy": 48.3,
        "in_sp500": True,
        "in_nasdaq100": True,
        "country": "US",
        "currency": "USD",
        "has_us_listing": False,
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
    # A single value of each repeatable filter arrives as a one-element list (the use case, not
    # the endpoint, slugs/normalizes it).
    assert fake.kwargs == {
        "query": "nv",
        "sectors": ["Technology"],
        "industries": ["semiconductors"],
        "in_sp500": True,
        "in_nasdaq100": False,
        "market_cap_tiers": [MarketCapTier.LARGE],
        "sort": StockSort.REVENUE_GROWTH,
        "direction": SortDirection.ASC,
        "limit": 10,
        "offset": 20,
        "countries": None,
        "include_interlisted": False,
    }


def test_search_passes_repeated_filters_through_as_lists():
    # The multi-select axes repeat: several sectors and several cap tiers at once, each binding
    # to a list the use case ORs together.
    fake = _FakeSearch(page=_a_page())
    resp = _search_client(search=fake).get(
        "/stocks/ticker",
        params=[
            ("sector", "technology"),
            ("sector", "energy"),
            ("industry", "semiconductors"),
            ("industry", "oil_gas_integrated"),
            ("market_cap", "large"),
            ("market_cap", "mid"),
        ],
    )

    assert resp.status_code == 200
    assert fake.kwargs["sectors"] == ["technology", "energy"]
    assert fake.kwargs["industries"] == ["semiconductors", "oil_gas_integrated"]
    assert fake.kwargs["market_cap_tiers"] == [MarketCapTier.LARGE, MarketCapTier.MID]


def test_search_passes_country_filter_through_as_a_list():
    # ?country= binds to a list the use case normalizes (uppercases) — repeat for a union.
    fake = _FakeSearch(page=_a_page())
    resp = _search_client(search=fake).get(
        "/stocks/ticker", params=[("country", "us"), ("country", "ca")]
    )

    assert resp.status_code == 200
    assert fake.kwargs["countries"] == ["us", "ca"]


def test_search_passes_include_interlisted_through():
    # ?include_interlisted=true reaches the use case; default is False (hide the duplicates).
    fake = _FakeSearch(page=_a_page())
    resp = _search_client(search=fake).get(
        "/stocks/ticker", params={"include_interlisted": "true"}
    )

    assert resp.status_code == 200
    assert fake.kwargs["include_interlisted"] is True


def test_search_uses_defaults_when_no_params_given():
    fake = _FakeSearch(page=_a_page())
    _search_client(search=fake).get("/stocks/ticker")

    assert fake.kwargs == {
        "query": None,
        "sectors": None,
        "industries": None,
        "in_sp500": None,
        "in_nasdaq100": None,
        "market_cap_tiers": None,
        # No ?sort= => no sort (the use case orders an unsorted browse by ticker); the
        # direction default rides along unused until a sort is chosen.
        "sort": None,
        "direction": SortDirection.DESC,
        "limit": 25,
        "offset": 0,
        "countries": None,
        "include_interlisted": False,
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


# --- The per-industry P/E benchmark (GET /stocks/industries/{industry}/pe) ------------------


def test_industry_pe_returns_the_expected_json_shape():
    fake = _FakeIndustryValuation(
        result=IndustryValuation(
            industry="semiconductors",
            count=34,
            median_pe=21.0,
            p25_pe=15.5,
            p75_pe=30.2,
        )
    )
    resp = _search_client(industry_valuation=fake).get(
        "/stocks/industries/Semiconductors/pe"
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "industry": "semiconductors",
        "count": 34,
        "median_pe": 21.0,
        "p25_pe": 15.5,
        "p75_pe": 30.2,
    }
    # The raw path label is handed to the use case (which slugs it) — the endpoint doesn't.
    assert fake.industry == "Semiconductors"


def test_industry_pe_unknown_industry_is_200_with_null_stats():
    fake = _FakeIndustryValuation(
        result=IndustryValuation(
            industry="nonesuch", count=0, median_pe=None, p25_pe=None, p75_pe=None
        )
    )
    resp = _search_client(industry_valuation=fake).get("/stocks/industries/nonesuch/pe")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["median_pe"] is None


def test_industry_pe_maps_value_error_to_400():
    fake = _FakeIndustryValuation(error=ValueError("An industry is required."))
    resp = _search_client(industry_valuation=fake).get("/stocks/industries/%20/pe")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "An industry is required."


def test_industry_pe_sets_a_short_cache_header():
    fake = _FakeIndustryValuation(result=IndustryValuation("x", 1, 20.0, 20.0, 20.0))
    resp = _search_client(industry_valuation=fake).get("/stocks/industries/x/pe")
    assert resp.headers["cache-control"] == "public, max-age=60"


# --- The AI-driven screen (GET /stocks/ai-search) -----------------------------------------


class _FakeAiScreen:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result if result is not None else ScreenIntent()
        self._error = error
        self.kwargs: dict | None = None

    def execute(self, **kwargs):
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._result


def _ai_client(use_case) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_ai_search_use_case] = lambda: use_case
    return TestClient(app)


def test_ai_search_returns_the_interpreted_filters_only():
    intent = ScreenIntent(
        sectors=("technology",),
        market_cap_tiers=(MarketCapTier.MEGA,),
        sort=StockSort.MARKET_CAP,
        direction=SortDirection.DESC,
    )
    resp = _ai_client(_FakeAiScreen(result=intent)).get(
        "/stocks/ai-search", params={"q": "mega cap tech stocks"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "interpreted": {
            "query": None,
            "sectors": ["technology"],
            "industries": [],
            "in_sp500": None,
            "in_nasdaq100": None,
            "market_cap_tiers": ["mega"],
            "sort": "market_cap",
            "direction": "desc",
            "limit": None,
        }
    }
    # The endpoint returns only the interpretation — no result page.
    assert "results" not in body


def test_ai_search_passes_the_query_through():
    fake = _FakeAiScreen(result=ScreenIntent())
    resp = _ai_client(fake).get("/stocks/ai-search", params={"q": "  top 5 banks "})
    assert resp.status_code == 200
    assert fake.kwargs == {"query": "  top 5 banks "}


def test_ai_search_requires_a_query():
    # Missing q -> 422 (the param is required); blank q -> the use case's 400.
    resp = _ai_client(_FakeAiScreen()).get("/stocks/ai-search")
    assert resp.status_code == 422


def test_ai_search_blank_query_is_a_400():
    fake = _FakeAiScreen(error=ValueError("A search request is required."))
    resp = _ai_client(fake).get("/stocks/ai-search", params={"q": "x"})
    assert resp.status_code == 400


def test_ai_search_translation_failure_is_a_502():
    fake = _FakeAiScreen(error=StockDataUnavailable("q", "model down"))
    resp = _ai_client(fake).get("/stocks/ai-search", params={"q": "tech"})
    assert resp.status_code == 502


def test_ai_search_sets_a_short_cache_header():
    fake = _FakeAiScreen(result=ScreenIntent())
    resp = _ai_client(fake).get("/stocks/ai-search", params={"q": "tech"})
    assert resp.headers["cache-control"] == "public, max-age=60"


# --- GET /stocks/ticker/{ticker}/peers (the peer comparison) -------------------------------


class _FakePeerComparison:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.ticker: str | None = None

    def execute(self, ticker):
        self.ticker = ticker
        if self._error is not None:
            raise self._error
        return self._result


def _peers_client(fake: _FakePeerComparison) -> TestClient:
    app = FastAPI()
    app.include_router(endpoints.router)
    app.dependency_overrides[endpoints.get_peer_comparison_use_case] = lambda: fake
    return TestClient(app)


def _a_peer(ticker, **overrides):
    base = dict(
        name=f"{ticker} Inc.",
        market_cap=1e12,
        pe_ratio=None,
        ev_ebitda=None,
        fcf_yield=None,
        net_margin=None,
        revenue_growth_yoy=None,
        tier=MarketCapTier.MEGA,
        is_anchor=False,
    )
    base.update(overrides)
    return PeerCompany(ticker=ticker, **base)


def test_peers_returns_the_expected_json_shape():
    comparison = PeerComparison(
        ticker="NVDA",
        industry="semiconductors",
        cohort="mega",
        anchor=_a_peer("NVDA", market_cap=3e12, pe_ratio=46.5, ev_ebitda=40.0, net_margin=55.123, is_anchor=True),
        peers=(
            _a_peer("AMD", market_cap=2e11, pe_ratio=30.0, ev_ebitda=25.0),
            _a_peer("AVGO", market_cap=6e11, pe_ratio=35.0, ev_ebitda=28.0),
        ),
        medians=PeerMedians(
            pe_ratio=35.0, ev_ebitda=28.0, fcf_yield=None, net_margin=40.0, revenue_growth_yoy=None
        ),
    )
    fake = _FakePeerComparison(result=comparison)

    resp = _peers_client(fake).get("/stocks/ticker/nvda/peers")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["ticker"], body["industry"], body["cohort"], body["count"]) == (
        "NVDA", "semiconductors", "mega", 2,
    )
    assert body["anchor"]["ticker"] == "NVDA" and body["anchor"]["is_anchor"] is True
    assert body["anchor"]["net_margin"] == 55.12  # rounded at the presenter
    assert [p["ticker"] for p in body["peers"]] == ["AMD", "AVGO"]
    assert body["medians"] == {
        "pe_ratio": 35.0,
        "ev_ebitda": 28.0,
        "fcf_yield": None,
        "net_margin": 40.0,
        "revenue_growth_yoy": None,
    }


def test_peers_empty_comparison_is_a_200_not_a_404():
    # An unclassified stock: null industry, null anchor, no peers — a 200, never a 404.
    comparison = PeerComparison(
        ticker="XYZ",
        industry=None,
        cohort="industry",
        anchor=None,
        peers=(),
        medians=PeerMedians(None, None, None, None, None),
    )
    resp = _peers_client(_FakePeerComparison(result=comparison)).get("/stocks/ticker/XYZ/peers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["industry"] is None
    assert body["anchor"] is None
    assert body["peers"] == []
    assert body["count"] == 0


def test_peers_malformed_ticker_is_a_400():
    fake = _FakePeerComparison(error=ValueError("A ticker is required."))
    resp = _peers_client(fake).get("/stocks/ticker/%20/peers")
    assert resp.status_code == 400


def test_peers_sets_a_short_cache_header():
    comparison = PeerComparison(
        ticker="NVDA", industry="semiconductors", cohort="mega", anchor=None, peers=(),
        medians=PeerMedians(None, None, None, None, None),
    )
    resp = _peers_client(_FakePeerComparison(result=comparison)).get("/stocks/ticker/NVDA/peers")
    assert resp.headers["cache-control"] == "public, max-age=60"
