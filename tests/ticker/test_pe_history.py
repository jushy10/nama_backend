"""Tests for the P/E-history entity and use case.

Offline: the ``PeHistory.build`` derivation (rolling TTM, the as-of close match, the
warm-up and loss guards) is exercised directly, and ``GetStockPeHistory`` runs against
hand-written fakes for the two ports — so this checks only the orchestration (symbol
normalization, the primary-vs-best-effort split between the reliable Alpaca closes and
the best-effort Yahoo EPS, and the short-circuit when there aren't enough quarters),
independent of Alpaca, Yahoo, or the DB.
"""

from datetime import date, datetime, timezone

import pytest

from app.stocks.entities import Candle, CandleSeries, Timeframe
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import CandleProvider
from app.stocks.ticker.entities import (
    PeHistory,
    PeHistoryPoint,
    ReportedEps,
    ValuationSignal,
)
from app.stocks.ticker.ports import EpsHistoryProvider
from app.stocks.ticker.use_cases import GetStockPeHistory


def _quarters(pairs: list[tuple[str, float]]) -> tuple[ReportedEps, ...]:
    return tuple(ReportedEps(date.fromisoformat(d), eps) for d, eps in pairs)


def _series(symbol: str, closes: dict[date, float]) -> CandleSeries:
    candles = tuple(
        Candle(
            timestamp=datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=None,
        )
        for d, c in sorted(closes.items())
    )
    return CandleSeries(symbol=symbol, timeframe=Timeframe.DAY_1, candles=candles)


class _FakeCandles(CandleProvider):
    def __init__(self, closes: dict[date, float] | None = None, error=None) -> None:
        self._closes = closes or {}
        self._error = error
        self.calls: list[tuple] = []

    def get_candles(self, symbol, timeframe, *, start, end):
        self.calls.append((symbol, timeframe, start, end))
        if self._error is not None:
            raise self._error
        return _series(symbol, self._closes)


class _FakeEpsHistory(EpsHistoryProvider):
    def __init__(self, eps: tuple[ReportedEps, ...] = (), error=None) -> None:
        self._eps = eps
        self._error = error
        self.calls: list[str] = []

    def get_eps_history(self, symbol):
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._eps


# --- The entity: PeHistory.build --------------------------------------------------------


def test_build_rolls_ttm_and_divides_the_close():
    eps = _quarters(
        [
            ("2023-05-01", 1.0),
            ("2023-08-01", 1.0),
            ("2023-11-01", 1.0),
            ("2024-02-01", 2.0),  # first full trailing year ends here: TTM 5.0
            ("2024-05-01", 3.0),  # TTM 7.0
        ]
    )
    closes = {date(2024, 2, 1): 50.0, date(2024, 5, 1): 70.0}
    history = PeHistory.build("AAPL", eps, closes)

    assert history.symbol == "AAPL"
    assert [(str(p.report_date), p.ttm_eps, p.pe) for p in history.points] == [
        ("2024-02-01", 5.0, 10.0),
        ("2024-05-01", 7.0, 10.0),
    ]


def test_build_matches_a_prior_session_close_for_a_weekend_release():
    # A release dated on a Saturday takes the most recent trading day's close within lag.
    eps = _quarters(
        [("2023-05-01", 1.0), ("2023-08-01", 1.0), ("2023-11-01", 1.0), ("2024-02-03", 2.0)]
    )
    history = PeHistory.build("X", eps, {date(2024, 2, 1): 50.0})  # Thu before the Sat
    assert len(history.points) == 1
    assert history.points[0].price == 50.0


def test_build_skips_a_release_with_no_close_within_lag():
    eps = _quarters(
        [("2023-05-01", 1.0), ("2023-08-01", 1.0), ("2023-11-01", 1.0), ("2024-02-01", 2.0)]
    )
    # Only a close from two weeks earlier — beyond the 7-day lag, so no point.
    assert PeHistory.build("X", eps, {date(2024, 1, 15): 50.0}).points == ()


def test_build_drops_a_trailing_loss():
    eps = _quarters(
        [("2023-05-01", -1.0), ("2023-08-01", -1.0), ("2023-11-01", -1.0), ("2024-02-01", -1.0)]
    )
    assert PeHistory.build("X", eps, {date(2024, 2, 1): 50.0}).points == ()


def test_build_needs_a_full_trailing_year():
    eps = _quarters([("2023-05-01", 1.0), ("2023-08-01", 1.0), ("2023-11-01", 1.0)])
    assert PeHistory.build("X", eps, {date(2023, 11, 1): 30.0}).points == ()


# --- The entity: PeHistory.stats (valuation vs. its own history) -------------------------


def _history_from_pes(pes: list[float]) -> PeHistory:
    """A PeHistory carrying the given P/Es, oldest first (the last is 'current'). Only ``pe``
    feeds the stats, so the other point fields are placeholders."""
    points = tuple(
        PeHistoryPoint(report_date=date(2022, 1, 1), price=100.0, ttm_eps=5.0, pe=float(pe))
        for pe in pes
    )
    return PeHistory(symbol="X", points=points)


def test_stats_is_none_for_a_thin_sample():
    # One shy of the floor -> no verdict; exactly the floor -> a verdict.
    assert _history_from_pes([15.0] * (PeHistory.MIN_POINTS_FOR_STATS - 1)).stats is None
    assert _history_from_pes([15.0] * PeHistory.MIN_POINTS_FOR_STATS).stats is not None


def test_stats_flags_a_cheap_current_reading():
    # Current (last) is the lowest multiple the stock has traded at -> cheap vs history.
    stats = _history_from_pes([20, 22, 24, 26, 28, 30, 25, 15]).stats
    assert stats is not None
    assert stats.current_pe == 15.0
    assert stats.min_pe == 15.0
    assert stats.max_pe == 30.0
    assert stats.median_pe == 24.5
    assert stats.signal is ValuationSignal.CHEAP
    assert stats.current_percentile < 25
    assert stats.discount_to_median_percent < 0  # below its typical multiple
    assert stats.sample_size == 8


def test_stats_flags_an_expensive_current_reading():
    # Current (last) is the dearest multiple -> expensive vs history; quartiles interpolate.
    stats = _history_from_pes([15, 16, 17, 18, 19, 20, 21, 30]).stats
    assert stats is not None
    assert stats.signal is ValuationSignal.EXPENSIVE
    assert stats.current_percentile >= 75
    assert stats.discount_to_median_percent > 0
    assert stats.p25_pe == 16.75  # type-7 interpolation, same as the industry benchmark
    assert stats.p75_pe == 20.25


def test_stats_reads_a_mid_range_current_as_fair():
    stats = _history_from_pes([10, 15, 20, 25, 30, 35, 40, 25]).stats
    assert stats is not None
    assert stats.signal is ValuationSignal.FAIR
    assert 25 < stats.current_percentile < 75
    assert stats.median_pe == 25.0


# --- The use case: GetStockPeHistory ----------------------------------------------------


def _eps_5q() -> tuple[ReportedEps, ...]:
    return _quarters(
        [
            ("2023-05-01", 1.0),
            ("2023-08-01", 1.0),
            ("2023-11-01", 1.0),
            ("2024-02-01", 2.0),
            ("2024-05-01", 3.0),
        ]
    )


def test_execute_combines_both_legs():
    candles = _FakeCandles({date(2024, 2, 1): 50.0, date(2024, 5, 1): 70.0})
    use_case = GetStockPeHistory(candles, _FakeEpsHistory(_eps_5q()))

    history = use_case.execute("aapl")

    assert history.symbol == "AAPL"  # normalized
    assert [p.pe for p in history.points] == [10.0, 10.0]
    # The price window is anchored on the earliest reported quarter.
    (_symbol, timeframe, start, _end) = candles.calls[0]
    assert timeframe is Timeframe.DAY_1
    assert start.date() == date(2023, 5, 1)


def test_blocked_eps_degrades_to_empty_without_a_price_fetch():
    candles = _FakeCandles({date(2024, 2, 1): 50.0})
    eps = _FakeEpsHistory(error=StockDataUnavailable("AAPL", "yahoo blocked"))
    use_case = GetStockPeHistory(candles, eps)

    history = use_case.execute("AAPL")

    assert history.points == ()
    assert candles.calls == []  # best-effort EPS empty → no Alpaca call


def test_too_few_quarters_short_circuits_before_the_price_fetch():
    candles = _FakeCandles({date(2023, 11, 1): 30.0})
    eps = _FakeEpsHistory(_quarters([("2023-05-01", 1.0), ("2023-11-01", 1.0)]))
    use_case = GetStockPeHistory(candles, eps)

    assert use_case.execute("AAPL").points == ()
    assert candles.calls == []


def test_candle_failure_propagates():
    candles = _FakeCandles(error=StockDataUnavailable("AAPL", "alpaca down"))
    use_case = GetStockPeHistory(candles, _FakeEpsHistory(_eps_5q()))
    with pytest.raises(StockDataUnavailable):
        use_case.execute("AAPL")


def test_bad_symbol_is_a_value_error():
    use_case = GetStockPeHistory(_FakeCandles(), _FakeEpsHistory())
    with pytest.raises(ValueError):
        use_case.execute("!!")
