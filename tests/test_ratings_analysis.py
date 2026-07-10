"""Tests for the AI ratings-review: the GetRatingsFindings use case + its endpoint.

Offline: hand-written fakes for the analyzer + the DB-only context providers, so the use-case
tests exercise only the orchestration (symbol normalization, DB-only context gather, top-firm
derivation, the no-coverage guard, and primary-vs-best-effort failure handling). The endpoint
tests inject a fake use case through ``dependency_overrides`` over the stocks router, checking
the controller + presenter (verdict/confidence/findings + service disclaimer, the cache header,
and the error mapping) — no Bedrock, no Yahoo, no database.
"""

from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.stocks import router as stocks_router
from app.stocks.entities import Confidence, RatingsAnalysis, RatingsVerdict
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import RatingsAnalysisProvider
from app.stocks.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRatingChanges,
    AnalystRecommendations,
    RatingChange,
    RecommendationTrend,
)
from app.stocks.recommendations.ports import (
    RatingChangeProvider,
    RecommendationProvider,
)
from app.stocks.use_cases import GetRatingsFindings


# --- fakes / fixtures --------------------------------------------------------------------------


def _an_analysis(symbol="NVDA") -> RatingsAnalysis:
    return RatingsAnalysis(
        symbol=symbol,
        verdict=RatingsVerdict.BULLISH,
        confidence=Confidence.HIGH,
        summary="Analysts are overwhelmingly positive.",
        findings=("95% rate it buy or better", "Wide target range signals disagreement"),
        model="test-model",
        generated_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
    )


class _FakeAnalyzer(RatingsAnalysisProvider):
    """Records what it was handed and returns a canned analysis (or raises)."""

    def __init__(self, result=None, *, error=None) -> None:
        self._result = result
        self._error = error
        self.received: list[tuple] = []

    def analyze(self, symbol, recommendations=None, top_firms=()) -> RatingsAnalysis:
        self.received.append((symbol, recommendations, top_firms))
        if self._error is not None:
            raise self._error
        return self._result if self._result is not None else _an_analysis(symbol)


class _FakeRecs(RecommendationProvider):
    def __init__(self, run, *, error=None) -> None:
        self._run = run
        self._error = error

    def get_recommendations(self, symbol) -> AnalystRecommendations:
        if self._error is not None:
            raise self._error
        return self._run


class _FakeChanges(RatingChangeProvider):
    def __init__(self, run, *, error=None) -> None:
        self._run = run
        self._error = error

    def get_rating_changes(self, symbol) -> AnalystRatingChanges:
        if self._error is not None:
            raise self._error
        return self._run


def _recs(symbol="NVDA") -> AnalystRecommendations:
    return AnalystRecommendations(
        symbol,
        (
            RecommendationTrend(
                date(2026, 6, 1), strong_buy=10, buy=48, hold=2, sell=1, strong_sell=0
            ),
        ),
        AnalystPriceTargets(mean=301.62, high=500.0, low=180.0, median=300.0),
    )


def _changes(symbol="NVDA") -> AnalystRatingChanges:
    return AnalystRatingChanges(
        symbol,
        (
            RatingChange(
                "RBC Capital",
                date(2026, 5, 21),
                action="main",
                to_grade="Outperform",
                target_current=270.0,
            ),
        ),
    )


# --- use case ----------------------------------------------------------------------------------


def test_gathers_coverage_and_derives_top_firms():
    analyzer = _FakeAnalyzer()
    result = GetRatingsFindings(analyzer, _FakeRecs(_recs()), _FakeChanges(_changes())).execute(
        "  nvda "
    )
    assert result.symbol == "NVDA"
    symbol, recs, top_firms = analyzer.received[0]
    assert symbol == "NVDA"  # normalized once, at the edge
    assert recs is not None and not recs.is_empty
    assert [f.firm for f in top_firms] == ["RBC Capital"]  # derived + credibility-filtered


def test_no_coverage_raises_before_the_model():
    analyzer = _FakeAnalyzer()
    use_case = GetRatingsFindings(
        analyzer,
        _FakeRecs(AnalystRecommendations("ZZZZ", ())),
        _FakeChanges(AnalystRatingChanges("ZZZZ", ())),
    )
    with pytest.raises(StockDataUnavailable):
        use_case.execute("ZZZZ")
    assert analyzer.received == []  # never asked to analyse an empty slate


def test_only_uncredited_events_still_counts_as_no_coverage():
    # Coverage exists (an event) but no consensus and no *credible* firm → nothing to render.
    analyzer = _FakeAnalyzer()
    use_case = GetRatingsFindings(
        analyzer,
        _FakeRecs(AnalystRecommendations("X", ())),
        _FakeChanges(
            AnalystRatingChanges("X", (RatingChange("Rosenblatt", date(2026, 5, 1)),))
        ),
    )
    with pytest.raises(StockDataUnavailable):
        use_case.execute("X")


def test_analyses_with_consensus_even_without_credible_firms():
    # A consensus split is renderable on its own, even if no credible firm has a stored action.
    analyzer = _FakeAnalyzer()
    GetRatingsFindings(
        analyzer,
        _FakeRecs(_recs()),
        _FakeChanges(
            AnalystRatingChanges("NVDA", (RatingChange("Rosenblatt", date(2026, 5, 1)),))
        ),
    ).execute("NVDA")
    _, recs, top_firms = analyzer.received[0]
    assert not recs.is_empty and top_firms == ()


def test_model_failure_propagates():
    analyzer = _FakeAnalyzer(error=StockDataUnavailable("NVDA", "bedrock down"))
    use_case = GetRatingsFindings(analyzer, _FakeRecs(_recs()), _FakeChanges(_changes()))
    with pytest.raises(StockDataUnavailable):
        use_case.execute("NVDA")


def test_context_read_failure_degrades_to_empty():
    # A DB-only context read that raises is treated as no data (best-effort) — here the
    # recommendations read fails, but the rating-change leg still yields a credible firm, so
    # the analysis proceeds with recommendations=None.
    analyzer = _FakeAnalyzer()
    GetRatingsFindings(
        analyzer,
        _FakeRecs(None, error=StockDataUnavailable("NVDA", "db read failed")),
        _FakeChanges(_changes()),
    ).execute("NVDA")
    _, recs, top_firms = analyzer.received[0]
    assert recs is None  # the failed read degraded to None
    assert [f.firm for f in top_firms] == ["RBC Capital"]


def test_rejects_invalid_symbols_before_touching_providers():
    analyzer = _FakeAnalyzer()
    use_case = GetRatingsFindings(analyzer, _FakeRecs(_recs()), _FakeChanges(_changes()))
    for bad in ("   ", "123", "TOOLONG"):
        with pytest.raises(ValueError):
            use_case.execute(bad)
    assert analyzer.received == []


# --- endpoint ----------------------------------------------------------------------------------


class _FakeUseCase:
    def __init__(self, *, result=None, error=None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def execute(self, symbol: str) -> RatingsAnalysis:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._result


def _client(fake: _FakeUseCase) -> TestClient:
    app = FastAPI()
    app.include_router(stocks_router.router)
    app.dependency_overrides[stocks_router.get_ratings_findings] = lambda: fake
    return TestClient(app)


_URL = "/stocks/ticker/NVDA/analyst-info/analysis"


def test_endpoint_returns_200_with_the_analysis_and_disclaimer():
    resp = _client(_FakeUseCase(result=_an_analysis())).get(_URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "NVDA"
    assert body["verdict"] == "bullish"
    assert body["confidence"] == "high"
    assert body["findings"] == [
        "95% rate it buy or better",
        "Wide target range signals disagreement",
    ]
    assert body["disclaimer"]  # service-authored, non-empty
    assert body["model"] == "test-model"
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_endpoint_forwards_the_ticker_to_the_use_case():
    fake = _FakeUseCase(result=_an_analysis())
    _client(fake).get("/stocks/ticker/nvda/analyst-info/analysis")
    assert fake.calls == ["nvda"]  # normalization is the use case's job


def test_endpoint_bad_symbol_is_400():
    fake = _FakeUseCase(error=ValueError("'123' is not a valid stock symbol."))
    assert _client(fake).get("/stocks/ticker/123/analyst-info/analysis").status_code == 400


def test_endpoint_unknown_symbol_is_404():
    fake = _FakeUseCase(error=StockNotFound("ZZZZ"))
    assert _client(fake).get("/stocks/ticker/ZZZZ/analyst-info/analysis").status_code == 404


def test_endpoint_no_coverage_or_model_failure_is_502():
    fake = _FakeUseCase(error=StockDataUnavailable("NVDA", "no analyst coverage to analyse"))
    assert _client(fake).get(_URL).status_code == 502
