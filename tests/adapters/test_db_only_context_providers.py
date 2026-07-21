from datetime import date

from app.stocks.adapters.db_only_context_providers import (
    DbOnlyAnnualEarningsProvider,
    DbOnlyQuarterlyEarningsProvider,
    DbOnlyRecommendationsProvider,
)
from app.stocks.earnings.annual.entities import AnnualEarnings, AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.recommendations.entities import (
    AnalystRecommendations,
    RecommendationTrend,
)


class _FakeRepo:
    def __init__(self, stored=None, raises=None):
        self._stored = stored
        self._raises = raises
        self.calls = 0

    def get(self, symbol):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._stored


def _quarterly() -> QuarterlyEarningsTimeline:
    return QuarterlyEarningsTimeline(
        "AAPL",
        (
            QuarterlyEarnings(
                fiscal_year=2026,
                fiscal_quarter=1,
                period_end=None,
                report_date=None,
                eps_actual=1.0,
                eps_estimate=0.9,
                eps_surprise=0.1,
                eps_surprise_percent=11.1,
                revenue_estimate=None,
            ),
        ),
    )


def _annual() -> AnnualEarningsTimeline:
    return AnnualEarningsTimeline(
        "AAPL",
        (
            AnnualEarnings(
                fiscal_year=2025,
                period_end=None,
                eps_actual=5.0,
                eps_estimate=None,
                revenue_actual=1000.0,
                revenue_estimate=None,
            ),
        ),
    )


def _recommendations() -> AnalystRecommendations:
    return AnalystRecommendations(
        "AAPL",
        (
            RecommendationTrend(
                period=date(2026, 7, 1),
                strong_buy=1,
                buy=2,
                hold=3,
                sell=0,
                strong_sell=0,
            ),
        ),
    )


def test_quarterly_serves_stored_timeline():
    stored = _quarterly()
    provider = DbOnlyQuarterlyEarningsProvider(_FakeRepo(stored))
    assert provider.get_quarterly_earnings("AAPL") is stored


def test_quarterly_miss_yields_empty_not_a_fetch():
    repo = _FakeRepo(stored=None)  # None == never cached
    result = DbOnlyQuarterlyEarningsProvider(repo).get_quarterly_earnings("AAPL")
    assert result.is_empty
    assert result.symbol == "AAPL"
    assert repo.calls == 1  # read once, no retry/fall-through


def test_quarterly_read_error_degrades_to_empty():
    provider = DbOnlyQuarterlyEarningsProvider(_FakeRepo(raises=RuntimeError("db down")))
    assert provider.get_quarterly_earnings("AAPL").is_empty


def test_annual_serves_stored_and_misses_to_empty():
    stored = _annual()
    assert DbOnlyAnnualEarningsProvider(_FakeRepo(stored)).get_annual_earnings("AAPL") is stored
    miss = DbOnlyAnnualEarningsProvider(_FakeRepo(None)).get_annual_earnings("AAPL")
    assert miss.is_empty and miss.symbol == "AAPL"


def test_annual_read_error_degrades_to_empty():
    provider = DbOnlyAnnualEarningsProvider(_FakeRepo(raises=RuntimeError("boom")))
    assert provider.get_annual_earnings("AAPL").is_empty


def test_recommendations_serves_stored_and_misses_to_empty():
    stored = _recommendations()
    assert (
        DbOnlyRecommendationsProvider(_FakeRepo(stored)).get_recommendations("AAPL")
        is stored
    )
    miss = DbOnlyRecommendationsProvider(_FakeRepo(None)).get_recommendations("AAPL")
    assert miss.is_empty and miss.symbol == "AAPL"


def test_recommendations_read_error_degrades_to_empty():
    provider = DbOnlyRecommendationsProvider(_FakeRepo(raises=RuntimeError("boom")))
    assert provider.get_recommendations("AAPL").is_empty
