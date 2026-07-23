"""The per-client daily generation budget on the AI analysis use cases: consumed only
when a generation actually runs (never on a cache hit), raising QuotaExceeded when spent.
Driven through GetEarningsAnalysis — every analysis use case shares the same guard."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domains.financials.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.domains.research.analysis.entities import EarningsAnalysis, EarningsTrend
from app.domains.research.analysis.interfaces import (
    AiAnalysisCacheAdapter,
    EarningsAnalysisAdapter,
)
from app.domains.research.analysis.use_cases import GetEarningsAnalysis
from app.domains.shared.exceptions import QuotaExceeded
from app.domains.shared.interfaces import GenerationQuotaAdapter


class _FakeQuota(GenerationQuotaAdapter):
    def __init__(self, *, allow=True) -> None:
        self._allow = allow
        self.consumed: list[str] = []

    def try_consume(self, client_id: str) -> bool:
        if not self._allow:
            return False
        self.consumed.append(client_id)
        return True


class _FakeAnalyzer(EarningsAnalysisAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, symbol, quarterly=None, annual=None) -> EarningsAnalysis:
        self.calls += 1
        return _an_analysis(symbol)


class _FakeQuarterly(QuarterlyEarningsAdapter):
    def get_quarterly_earnings(self, symbol) -> QuarterlyEarningsTimeline:
        return QuarterlyEarningsTimeline(
            symbol,
            (
                QuarterlyEarnings(
                    fiscal_year=2026,
                    fiscal_quarter=1,
                    period_end=None,
                    report_date=None,
                    eps_actual=1.5,
                    eps_estimate=1.4,
                    eps_surprise=0.1,
                    eps_surprise_percent=7.1,
                    revenue_estimate=None,
                ),
            ),
        )


class _FakeCache(AiAnalysisCacheAdapter):
    def __init__(self, stored=None) -> None:
        self._stored = stored

    def get(self, symbol):
        return self._stored

    def put(self, symbol, analysis) -> None:
        pass


def _an_analysis(symbol="AAPL") -> EarningsAnalysis:
    return EarningsAnalysis(
        symbol=symbol,
        trend=EarningsTrend.ACCELERATING,
        summary="Beats keep coming.",
        highlights=("4 straight beats",),
        model="test-model",
        generated_at=datetime.now(timezone.utc),
    )


def _use_case(quota, cache=None):
    return GetEarningsAnalysis(
        _FakeAnalyzer(),
        quarterly_provider=_FakeQuarterly(),
        cache=cache,
        cache_ttl=timedelta(minutes=30),
        quota=quota,
    )


def test_a_generation_consumes_one_from_the_budget():
    quota = _FakeQuota()
    _use_case(quota).execute("AAPL", client_id="1.2.3.4")
    assert quota.consumed == ["1.2.3.4"]


def test_an_exhausted_budget_raises_quota_exceeded_before_the_model_runs():
    analyzer = _FakeAnalyzer()
    use_case = GetEarningsAnalysis(
        analyzer, quarterly_provider=_FakeQuarterly(), quota=_FakeQuota(allow=False)
    )
    with pytest.raises(QuotaExceeded):
        use_case.execute("AAPL", client_id="1.2.3.4")
    assert analyzer.calls == 0  # denied before any metered call


def test_a_fresh_cache_hit_is_free():
    quota = _FakeQuota(allow=False)  # would raise if ever consulted
    cached = _an_analysis()
    result = _use_case(quota, cache=_FakeCache(stored=cached)).execute(
        "AAPL", client_id="1.2.3.4"
    )
    assert result is cached


def test_no_client_id_skips_the_quota():
    # Non-HTTP callers (tests, internal composition) carry no client identity.
    quota = _FakeQuota(allow=False)
    _use_case(quota).execute("AAPL")


def test_no_quota_wired_is_a_no_op():
    _use_case(None).execute("AAPL", client_id="1.2.3.4")
