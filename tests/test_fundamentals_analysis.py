"""Tests for the AI fundamentals review: the GetFundamentalsAnalysis use case + its endpoint.

Offline: the use-case tests build a real ``GetStockInfo`` over hand-written fakes (so the
enriched snapshot the analyzer reasons over is assembled exactly as production does), plus a fake
analyzer and a fake industry repository. They exercise only the orchestration — symbol
normalization, the no-fundamentals guard, the best-effort industry-P/E benchmark, and
primary-vs-best-effort failure handling. The endpoint tests inject a fake use case through
``dependency_overrides`` over the stocks router, checking the controller + presenter
(verdict/confidence/findings + service disclaimer, the cache header, and the error mapping) — no
Bedrock, no Finnhub, no database.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks.analysis.entities import (
    Confidence,
    FundamentalsAnalysis,
    FundamentalsVerdict,
)
from app.stocks.analysis.ports import AiAnalysisCache, FundamentalsAnalysisProvider
from app.stocks.analysis.use_cases import GetFundamentalsAnalysis, GetStockInfo
from app.stocks.endpoints import analysis_endpoints as stocks_router
from app.stocks.entities import AnalystEstimates, Stock
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import AnalystEstimatesProvider, StockDataProvider
from app.stocks.ticker.entities import PeHistoryStats, ValuationSignal
from app.stocks.universe.entities import AnchorMetrics, MarketCapTier
from app.stocks.universe.repository import StockSearchRepository


# --- fixtures ----------------------------------------------------------------------------------


def _a_stock(**overrides) -> Stock:
    base = dict(
        symbol="AAPL", name="Apple Inc.", exchange="NASDAQ", price=297.86,
        open=298.44, high=300.56, low=295.635, previous_close=296.07,
        volume=1278873, bid=283.52, ask=313.43,
        as_of=datetime(2026, 6, 18, 19, 59, 59, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Stock(**base)


def _an_anchor(**overrides) -> AnchorMetrics:
    """The fundamentals the app materializes on the ``stocks`` anchor — the DB-only source
    the analysis overlay now reads instead of a live Finnhub vendor."""
    base = dict(
        market_cap=3_120_000_000_000.0, dividend_per_share=1.0,
        gross_margin=44.0, operating_margin=30.0, net_margin=25.0,
        return_on_equity=147.4, current_ratio=0.9, debt_to_equity=1.5, beta=1.2,
        book_value_per_share=45.0, sales_per_share=90.0, fcf_per_share=6.43,
        revenue_growth_yoy=8.0, eps_growth_yoy=12.0, name="Apple Inc.",
    )
    base.update(overrides)
    return AnchorMetrics(**base)


def _an_estimates(**overrides) -> AnalystEstimates:
    base = dict(
        fiscal_year=2026, period_end=date(2026, 9, 30),
        eps_avg=8.0, revenue_avg=420_000_000_000.0,
        fiscal_year_fy2=2027, eps_avg_fy2=9.2, revenue_avg_fy2=455_000_000_000.0,
    )
    base.update(overrides)
    return AnalystEstimates(**base)


def _an_analysis(symbol="AAPL") -> FundamentalsAnalysis:
    return FundamentalsAnalysis(
        symbol=symbol,
        verdict=FundamentalsVerdict.STRONG,
        confidence=Confidence.HIGH,
        summary="Profitable and growing, at a reasonable price.",
        findings=("Fat net margin", "Revenue still growing double digits"),
        model="test-model",
        generated_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )


# --- fakes -------------------------------------------------------------------------------------


class _FakeProvider(StockDataProvider):
    def __init__(self, stock=None, *, raises=None):
        self._stock = stock
        self._raises = raises

    def get_stock(self, symbol: str) -> Stock:
        if self._raises is not None:
            raise self._raises
        return self._stock


class _FakeEstimates(AnalystEstimatesProvider):
    def __init__(self, estimates):
        self._estimates = estimates

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        return self._estimates


class _FakeAnalyzer(FundamentalsAnalysisProvider):
    """Records what it was handed and returns a canned analysis (or raises)."""

    def __init__(self, result=None, *, error=None) -> None:
        self._result = result
        self._error = error
        self.received: list[tuple] = []
        self.pe_history_seen: list = []

    def analyze(
        self, stock, industry_valuation=None, pe_history=None
    ) -> FundamentalsAnalysis:
        self.received.append((stock, industry_valuation))
        self.pe_history_seen.append(pe_history)
        if self._error is not None:
            raise self._error
        return self._result if self._result is not None else _an_analysis(stock.symbol)


class _FakePeHistory:
    """Stands in for GetStockPeHistory: returns an object exposing ``.stats`` (or raises the
    way a Yahoo-blocked P/E-history read would)."""

    def __init__(self, stats=None, *, error=None) -> None:
        self._stats = stats
        self._error = error

    def execute(self, symbol: str):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(stats=self._stats)


class _FakeSearchRepo(StockSearchRepository):
    """The anchor reads the analysis path uses — the ticker's fundamentals (overlaid onto the
    snapshot via ``anchor_metrics_for_ticker``), its industry, its cap tier, and its peers —
    configurable per test. Every peer defaults to the MID tier, so ``for_stock_peers`` yields
    one whole-industry cohort. ``anchor`` seeds the overlaid fundamentals (an empty
    ``AnchorMetrics`` by default — an unsynced stock). The screen/classification methods aren't
    exercised here."""

    def __init__(self, *, industry=None, pe_ratios=(), anchor=None, raises=None):
        self._industry = industry
        self._peers = tuple((pe, MarketCapTier.MID) for pe in pe_ratios)
        self._anchor = anchor if anchor is not None else AnchorMetrics()
        self._raises = raises

    def industry_for_ticker(self, ticker):
        if self._raises is not None:
            raise self._raises
        return self._industry

    def anchor_metrics_for_ticker(self, ticker):
        if self._raises is not None:
            raise self._raises
        return self._anchor

    def tier_for_ticker(self, ticker):
        if self._raises is not None:
            raise self._raises
        return MarketCapTier.MID

    def industry_peers(self, industry):
        if self._raises is not None:
            raise self._raises
        return self._peers

    def pe_ratios_for_industry(self, industry):  # pragma: no cover - not the analysis path
        raise NotImplementedError

    def peers_for_industry(self, industry):  # pragma: no cover - not the analysis path
        raise NotImplementedError

    def search(self, criteria):  # pragma: no cover - not the analysis path
        raise NotImplementedError

    def classifications(self):  # pragma: no cover - not the analysis path
        raise NotImplementedError


def _enriched_info(**stock_overrides) -> GetStockInfo:
    """A real GetStockInfo carrying the forward estimates the way the analyzer sees it in
    production. The trailing fundamentals (metrics + market cap + dividend) are no longer read
    here — they're overlaid from the ``stocks`` anchor by ``GetFundamentalsAnalysis`` — so a
    snapshot from this alone carries the price + estimates, and its fundamentals fill from
    whatever anchor the test wires as the ``industry_repository``."""
    return GetStockInfo(
        _FakeProvider(stock=_a_stock(**stock_overrides)),
        estimates_provider=_FakeEstimates(_an_estimates()),
    )


# --- use case ----------------------------------------------------------------------------------


def test_gathers_fundamentals_and_industry_benchmark():
    analyzer = _FakeAnalyzer()
    use_case = GetFundamentalsAnalysis(
        _enriched_info(),
        analyzer,
        _FakeSearchRepo(
            industry="semiconductors",
            pe_ratios=(10.0, 20.0, 30.0, 40.0, 50.0),
            anchor=_an_anchor(),  # the fundamentals overlaid from the anchor
        ),
    )
    result = use_case.execute("  aapl ")
    assert result.symbol == "AAPL"
    stock, valuation = analyzer.received[0]
    # The metrics block was overlaid from the anchor; the estimates rode the snapshot.
    assert stock.metrics is not None and stock.analyst_estimates is not None
    assert stock.metrics.gross_margin == 44.0  # off the anchor, not a live vendor
    assert valuation is not None
    assert valuation.industry == "semiconductors"
    assert valuation.median_pe == 30.0  # median of the five peers


def test_no_fundamentals_raises_before_the_model():
    # A bare snapshot (Alpaca price only, no estimates) over an EMPTY anchor carries nothing
    # fundamental — the overlay fills nothing, so fail rather than ask the model to reason
    # over a price.
    analyzer = _FakeAnalyzer()
    info = GetStockInfo(_FakeProvider(stock=_a_stock()))  # no estimates
    use_case = GetFundamentalsAnalysis(
        info, analyzer, _FakeSearchRepo(anchor=AnchorMetrics())  # all None
    )
    with pytest.raises(StockDataUnavailable):
        use_case.execute("AAPL")
    assert analyzer.received == []  # never asked to analyse a bare price


def test_market_cap_alone_is_enough_to_analyse():
    # Even without a full metrics block, an anchor carrying only a market cap gives the
    # snapshot *something* fundamental — the analysis proceeds (best-effort, on whatever it's
    # handed).
    analyzer = _FakeAnalyzer()
    info = GetStockInfo(_FakeProvider(stock=_a_stock()))  # no estimates
    use_case = GetFundamentalsAnalysis(
        info, analyzer, _FakeSearchRepo(anchor=AnchorMetrics(market_cap=1_000_000.0))
    )
    use_case.execute("AAPL")
    stock, _ = analyzer.received[0]
    assert stock.market_cap == 1_000_000.0  # overlaid from the anchor


def test_industry_benchmark_is_best_effort():
    # A failing anchor read degrades to an omitted benchmark; the analysis still proceeds.
    analyzer = _FakeAnalyzer()
    use_case = GetFundamentalsAnalysis(
        _enriched_info(),
        analyzer,
        _FakeSearchRepo(raises=StockDataUnavailable("AAPL", "db down")),
    )
    result = use_case.execute("AAPL")
    assert result.verdict is FundamentalsVerdict.STRONG
    _, valuation = analyzer.received[0]
    assert valuation is None


def test_thin_industry_benchmark_is_omitted():
    # Under MIN_REPRESENTATIVE_PEERS (4 valued peers) the "median" describes those names, not
    # the industry, so it's not handed to the model as a peer anchor.
    analyzer = _FakeAnalyzer()
    use_case = GetFundamentalsAnalysis(
        _enriched_info(),
        analyzer,
        _FakeSearchRepo(industry="uranium", pe_ratios=(10.0, 20.0, 30.0, 40.0)),
    )
    use_case.execute("AAPL")
    _, valuation = analyzer.received[0]
    assert valuation is None


def test_industry_omitted_when_unscreened():
    # A symbol with no industry on the anchor yields no benchmark rather than an empty shell.
    analyzer = _FakeAnalyzer()
    use_case = GetFundamentalsAnalysis(
        _enriched_info(), analyzer, _FakeSearchRepo(industry=None)
    )
    use_case.execute("AAPL")
    _, valuation = analyzer.received[0]
    assert valuation is None


def test_no_industry_repository_omits_the_benchmark():
    analyzer = _FakeAnalyzer()
    GetFundamentalsAnalysis(_enriched_info(), analyzer).execute("AAPL")
    _, valuation = analyzer.received[0]
    assert valuation is None


def _a_pe_stats() -> PeHistoryStats:
    return PeHistoryStats(
        current_pe=18.0, median_pe=24.0, p25_pe=20.0, p75_pe=30.0,
        min_pe=12.0, max_pe=40.0, current_percentile=15.0,
        discount_to_median_percent=-25.0, signal=ValuationSignal.CHEAP, sample_size=16,
    )


def test_pe_history_signal_is_gathered_and_passed():
    # The "cheap for this stock?" anchor: the P/E-history stats are read best-effort and handed
    # to the analyzer alongside the peer benchmark.
    analyzer = _FakeAnalyzer()
    stats = _a_pe_stats()
    use_case = GetFundamentalsAnalysis(
        _enriched_info(),
        analyzer,
        _FakeSearchRepo(anchor=_an_anchor()),
        pe_history=_FakePeHistory(stats=stats),
    )
    use_case.execute("AAPL")
    assert analyzer.pe_history_seen[0] is stats


def test_pe_history_is_best_effort():
    # A Yahoo-blocked P/E-history read (the one non-DB-only context leg) degrades to no signal;
    # the analysis still proceeds.
    analyzer = _FakeAnalyzer()
    use_case = GetFundamentalsAnalysis(
        _enriched_info(),
        analyzer,
        _FakeSearchRepo(anchor=_an_anchor()),
        pe_history=_FakePeHistory(error=StockDataUnavailable("AAPL", "yahoo blocked")),
    )
    result = use_case.execute("AAPL")
    assert result.verdict is FundamentalsVerdict.STRONG
    assert analyzer.pe_history_seen[0] is None


def test_model_failure_propagates():
    analyzer = _FakeAnalyzer(error=StockDataUnavailable("AAPL", "bedrock down"))
    use_case = GetFundamentalsAnalysis(_enriched_info(), analyzer)
    with pytest.raises(StockDataUnavailable):
        use_case.execute("AAPL")


def test_snapshot_failure_propagates_before_the_model():
    analyzer = _FakeAnalyzer()
    info = GetStockInfo(_FakeProvider(raises=StockNotFound("ZZZZ")))
    with pytest.raises(StockNotFound):
        GetFundamentalsAnalysis(info, analyzer).execute("ZZZZ")
    assert analyzer.received == []  # analyzer never called when the snapshot fails


def test_rejects_invalid_symbols_before_touching_providers():
    analyzer = _FakeAnalyzer()
    use_case = GetFundamentalsAnalysis(_enriched_info(), analyzer)
    for bad in ("   ", "123", "TOOLONG"):
        with pytest.raises(ValueError):
            use_case.execute(bad)
    assert analyzer.received == []


# --- result cache ------------------------------------------------------------------------------


class _FakeCache(AiAnalysisCache):
    """In-memory stand-in for the generic AI-analysis result cache; records puts."""

    def __init__(self, stored=None, key: str = "AAPL") -> None:
        self._store = {key: stored} if stored is not None else {}
        self.puts: list[tuple] = []

    def get(self, key):
        return self._store.get(key)

    def put(self, key, analysis):
        self.puts.append((key, analysis))
        self._store[key] = analysis


def _analysis_at(when: datetime, *, summary="cached", findings=("f",)) -> FundamentalsAnalysis:
    return FundamentalsAnalysis(
        symbol="AAPL",
        verdict=FundamentalsVerdict.STRONG,
        confidence=Confidence.HIGH,
        summary=summary,
        findings=findings,
        model="m",
        generated_at=when,
    )


def test_fresh_cached_read_skips_generation():
    # A fresh stored read is returned verbatim — no snapshot gather, no model call. The
    # analyzer would raise if reached, proving the short-circuit.
    fresh = _analysis_at(datetime.now(timezone.utc))
    analyzer = _FakeAnalyzer(error=AssertionError("model must not be called"))
    cache = _FakeCache(stored=fresh)
    result = GetFundamentalsAnalysis(
        _enriched_info(), analyzer, cache=cache
    ).execute("aapl")  # normalizes to AAPL, matching the cached key
    assert result is fresh
    assert analyzer.received == []
    assert cache.puts == []


def test_cache_miss_generates_and_stores():
    generated = _an_analysis()  # complete (summary + findings)
    analyzer = _FakeAnalyzer(result=generated)
    cache = _FakeCache()
    result = GetFundamentalsAnalysis(_enriched_info(), analyzer, cache=cache).execute("AAPL")
    assert result is generated
    assert cache.puts == [("AAPL", generated)]


def test_stale_cache_is_regenerated_and_stored():
    stale = _analysis_at(datetime(2020, 1, 1, tzinfo=timezone.utc))
    generated = _an_analysis()
    analyzer = _FakeAnalyzer(result=generated)
    cache = _FakeCache(stored=stale)
    result = GetFundamentalsAnalysis(
        _enriched_info(), analyzer, cache=cache, cache_ttl=timedelta(minutes=30)
    ).execute("AAPL")
    assert result is generated  # regenerated, not the stale read
    assert cache.puts == [("AAPL", generated)]


def test_incomplete_read_is_not_cached():
    incomplete = _analysis_at(datetime.now(timezone.utc), summary="", findings=())
    analyzer = _FakeAnalyzer(result=incomplete)
    cache = _FakeCache()
    result = GetFundamentalsAnalysis(_enriched_info(), analyzer, cache=cache).execute("AAPL")
    assert result is incomplete  # still returned to the caller
    assert cache.puts == []  # but not stored


# --- endpoint ----------------------------------------------------------------------------------


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> FundamentalsAnalysis:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(stocks_router.router)
    app.dependency_overrides[stocks_router.get_fundamentals_analysis] = lambda: fake
    return TestClient(app)


_URL = "/stocks/AAPL/fundamentals/analysis"


def test_endpoint_returns_200_with_the_analysis_and_disclaimer():
    resp = _client(_FakeUseCase(result=_an_analysis())).get(_URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "AAPL"
    assert body["verdict"] == "strong"
    assert body["confidence"] == "high"
    assert body["findings"] == [
        "Fat net margin",
        "Revenue still growing double digits",
    ]
    assert body["disclaimer"]  # service-authored, non-empty
    assert body["model"] == "test-model"
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_endpoint_forwards_the_symbol_to_the_use_case():
    fake = _FakeUseCase(result=_an_analysis())
    _client(fake).get("/stocks/aapl/fundamentals/analysis")
    assert fake.calls == ["aapl"]  # normalization is the use case's job


def test_endpoint_bad_symbol_is_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/123/fundamentals/analysis").status_code == 400


def test_endpoint_unknown_symbol_is_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ZZZZ/fundamentals/analysis").status_code == 404


def test_endpoint_no_fundamentals_or_model_failure_is_502():
    fake = _FakeUseCase(error=StockDataUnavailable("AAPL", "no fundamentals data to analyse"))
    assert _client(fake).get(_URL).status_code == 502
