"""Tests for the ticker read endpoint (GET /stocks/ticker/{ticker}).

Offline: a fake GetTickerCard is injected through dependency_overrides + FastAPI's
TestClient, so this checks only the controller + presenter — the JSON shape (symbol
renamed to ``ticker``, the day move, the opt-in ``dividend``/``performance``/``metrics``
blocks with the ``1w``/``1m`` performance aliases), the include pass-through, the cache
header, unrequested/unavailable blocks as nulls (not a 404), and the error mapping —
without touching Alpaca, Finnhub, or the database.
"""

from datetime import date, datetime, timezone

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
from app.stocks.ticker.use_cases import TickerCard


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


def _a_card(*, include: frozenset[str] = frozenset()) -> TickerCard:
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
        valuation=(
            TickerValuation(
                symbol="MU", price=975.56, forward_pe=13.3, forward_eps_growth=104.1
            )
            if "metrics" in include
            else None
        ),
        name="Micron Technology",
        fundamentals=StockFundamentals(
            market_cap=1_090_000_000_000.0,
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
    assert body["price"] == 975.56
    assert body["change"] == 12.3  # vs the previous close, same rule as /quote
    assert body["change_percent"] == 1.28
    assert body["market_cap"] == 1_090_000_000_000.0
    # Opt-in blocks stay null until requested — even though the fundamentals
    # (which carry the dividend) were fetched for the market cap.
    assert body["dividend"] is None
    assert body["performance"] is None
    assert body["metrics"] is None
    assert body["options_metrics"] is None
    assert fake.calls == [("MU", None)]


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
    # Trailing PEG + margins ride the fundamentals; forward PEG the stored consensus.
    assert body["metrics"] == {
        "peg": 0.03,  # 22.4 / 700.7 — the degenerate trailing read, for contrast
        "forward_peg": 0.13,
        "gross_margin": 52.1,
        "operating_margin": 38.9,
        "net_margin": 33.5,
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
        )
    )
    resp = _client(fake).get("/stocks/ticker/MU?include=dividend,metrics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] is None
    assert body["exchange"] is None
    assert body["market_cap"] is None
    assert body["dividend"] is None  # requested, but nothing to serve
    # The metrics block still appears (it was requested) with its trailing half
    # null and the consensus-backed forward PEG intact.
    assert body["metrics"] == {
        "peg": None,
        "forward_peg": 0.13,
        "gross_margin": None,
        "operating_margin": None,
        "net_margin": None,
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
