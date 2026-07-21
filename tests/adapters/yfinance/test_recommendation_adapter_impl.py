from datetime import date

import pandas as pd
import pytest

from app.stocks.adapters.yfinance.recommendation_adapter_impl import (
    RecommendationAdapterImpl,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.company.recommendations.entities import AnalystRecommendations

_TODAY = date(2026, 7, 15)


class _FakeTicker:
    def __init__(
        self, recommendations=None, error=None, price_targets=None, targets_error=None
    ) -> None:
        self._recommendations = recommendations
        self._error = error
        self._price_targets = price_targets
        self._targets_error = targets_error

    @property
    def recommendations(self):
        if self._error is not None:
            raise self._error
        return self._recommendations

    @property
    def analyst_price_targets(self):
        if self._targets_error is not None:
            raise self._targets_error
        return self._price_targets


def provider_with(
    frame=None, error=None, price_targets=None, targets_error=None
) -> RecommendationAdapterImpl:
    return RecommendationAdapterImpl(
        ticker_factory=lambda symbol: _FakeTicker(
            frame, error, price_targets, targets_error
        ),
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
    provider = RecommendationAdapterImpl(
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


def test_attaches_price_targets_from_the_dict():
    frame = _frame([{"period": "0m", "buy": 5}])
    targets = {"current": 313.4, "mean": 315.5, "high": 400.0, "low": 215.0, "median": 315.0}
    recs = provider_with(frame, price_targets=targets).get_recommendations("AAPL")
    assert recs.price_targets is not None
    # `current` is the live price, not a target — deliberately not carried.
    assert (recs.price_targets.mean, recs.price_targets.high) == (315.5, 400.0)
    assert (recs.price_targets.low, recs.price_targets.median) == (215.0, 315.0)


def test_non_positive_and_nan_targets_drop_to_none():
    frame = _frame([{"period": "0m", "buy": 5}])
    targets = {"mean": 315.5, "high": float("nan"), "low": 0.0, "median": -1.0}
    recs = provider_with(frame, price_targets=targets).get_recommendations("AAPL")
    assert recs.price_targets.mean == 315.5
    assert recs.price_targets.high is None  # NaN
    assert recs.price_targets.low is None  # 0 is "no target"
    assert recs.price_targets.median is None  # negative is junk


def test_price_targets_absent_is_none_not_an_error():
    frame = _frame([{"period": "0m", "buy": 5}])
    assert provider_with(frame, price_targets={}).get_recommendations("AAPL").price_targets is None
    assert provider_with(frame, price_targets=None).get_recommendations("AAPL").price_targets is None


def test_price_target_failure_never_sinks_the_trends():
    # A blocked price-target read is best-effort: the trends still come back, targets just null.
    frame = _frame([{"period": "0m", "buy": 5}])
    recs = provider_with(
        frame, targets_error=RuntimeError("crumb 401")
    ).get_recommendations("AAPL")
    assert recs.latest.buy == 5
    assert recs.price_targets is None


def test_vendor_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider_with(error=RuntimeError("rate limited")).get_recommendations("AAPL")


def test_ticker_construction_failure_raises_unavailable():
    def _boom(symbol):
        raise RuntimeError("no network")

    provider = RecommendationAdapterImpl(
        ticker_factory=_boom, today=lambda: _TODAY
    )
    with pytest.raises(StockDataUnavailable):
        provider.get_recommendations("AAPL")
