"""Unit tests for the yfinance recommendations adapter.

No network: a fake Ticker (canned pandas frames) is injected through the factory, and
"today" is pinned so the relative month labels resolve deterministically. Verifies the
adapter anchors Yahoo's ``0m``/``-1m``/… labels on the current month, maps the stance
counts, orders newest-first, treats an uncovered symbol as empty coverage (not an error),
and turns vendor failures into domain errors.
"""

from datetime import date

import pandas as pd
import pytest

from app.stocks.adapters.yfinance_recommendations_adapter import (
    YfinanceRecommendationProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.recommendations.entities import AnalystRecommendations

_TODAY = date(2026, 7, 15)


class _FakeTicker:
    def __init__(self, recommendations=None, error=None) -> None:
        self._recommendations = recommendations
        self._error = error

    @property
    def recommendations(self):
        if self._error is not None:
            raise self._error
        return self._recommendations


def provider_with(frame=None, error=None) -> YfinanceRecommendationProvider:
    return YfinanceRecommendationProvider(
        ticker_factory=lambda symbol: _FakeTicker(frame, error),
        today=lambda: _TODAY,
    )


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_maps_rows_anchors_periods_on_today_and_orders_newest_first():
    # Out of order on purpose — the adapter must return newest month first.
    frame = _frame(
        [
            {"period": "-1m", "strongBuy": 7, "buy": 23, "hold": 15, "sell": 1, "strongSell": 2},
            {"period": "0m", "strongBuy": 6, "buy": 22, "hold": 16, "sell": 1, "strongSell": 2},
            {"period": "-2m", "strongBuy": 7, "buy": 25, "hold": 14, "sell": 1, "strongSell": 1},
        ]
    )
    recs = provider_with(frame).get_recommendations("AAPL")
    assert isinstance(recs, AnalystRecommendations)
    # 0m anchors on today's month (July 2026), each step is one month back.
    assert [t.period for t in recs.trends] == [
        date(2026, 7, 1),
        date(2026, 6, 1),
        date(2026, 5, 1),
    ]
    latest = recs.latest
    assert (latest.strong_buy, latest.buy, latest.hold) == (6, 22, 16)
    assert latest.total == 47


def test_relative_months_cross_a_year_boundary():
    provider = YfinanceRecommendationProvider(
        ticker_factory=lambda symbol: _FakeTicker(
            _frame([{"period": "0m", "buy": 1}, {"period": "-1m", "buy": 2}])
        ),
        today=lambda: date(2026, 1, 10),
    )
    recs = provider.get_recommendations("AAPL")
    assert [t.period for t in recs.trends] == [date(2026, 1, 1), date(2025, 12, 1)]


def test_labels_in_the_index_are_supported():
    # Older yfinance versions carry the period label in the index, not a column.
    frame = pd.DataFrame(
        {"strongBuy": [6, 7], "buy": [22, 23]}, index=["0m", "-1m"]
    )
    recs = provider_with(frame).get_recommendations("AAPL")
    assert [t.period for t in recs.trends] == [date(2026, 7, 1), date(2026, 6, 1)]
    assert recs.latest.buy == 22


def test_empty_frame_is_no_coverage_not_an_error():
    recs = provider_with(pd.DataFrame()).get_recommendations("ZZZZ")
    assert recs.is_empty
    assert recs.latest is None


def test_missing_frame_is_no_coverage_not_an_error():
    recs = provider_with(None).get_recommendations("ZZZZ")
    assert recs.is_empty


def test_rows_without_a_parseable_period_are_dropped():
    frame = _frame(
        [
            {"period": "0m", "buy": 5},
            {"period": "1y", "buy": 9},  # not a month label
            {"period": None, "buy": 9},
        ]
    )
    recs = provider_with(frame).get_recommendations("AAPL")
    assert [t.period for t in recs.trends] == [date(2026, 7, 1)]


def test_duplicate_months_keep_the_first_row():
    frame = _frame([{"period": "0m", "buy": 5}, {"period": "0m", "buy": 9}])
    recs = provider_with(frame).get_recommendations("AAPL")
    assert len(recs.trends) == 1
    assert recs.latest.buy == 5


def test_missing_and_nan_counts_default_to_zero():
    frame = _frame([{"period": "0m", "buy": 3, "strongBuy": float("nan")}])
    t = provider_with(frame).get_recommendations("AAPL").latest
    assert t.buy == 3
    assert t.strong_buy == t.hold == t.sell == t.strong_sell == 0


def test_vendor_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider_with(error=RuntimeError("rate limited")).get_recommendations("AAPL")


def test_ticker_construction_failure_raises_unavailable():
    def _boom(symbol):
        raise RuntimeError("no network")

    provider = YfinanceRecommendationProvider(
        ticker_factory=_boom, today=lambda: _TODAY
    )
    with pytest.raises(StockDataUnavailable):
        provider.get_recommendations("AAPL")
