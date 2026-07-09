"""Unit tests for the yfinance EPS-history adapter.

No network: a fake Ticker returns the ``get_earnings_dates`` frame yfinance would, so
this checks the mapping — the *reported* quarters only (future/unreported rows carry a
NaN ``Reported EPS`` and drop out), oldest first, deduped by date — plus an uncovered
symbol degrading to an empty tuple and any vendor failure becoming a domain error.
"""

from datetime import date

import pandas as pd
import pytest

from app.stocks.adapters.yfinance_eps_history_adapter import (
    YfinanceEpsHistoryProvider,
)
from app.stocks.exceptions import StockDataUnavailable

_NAN = float("nan")


def _earnings_dates(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """A date-indexed frame like ``Ticker.get_earnings_dates``: rows of
    ``(announce_date, Reported EPS)``; a NaN Reported EPS is a future/unreported date."""
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows])
    return pd.DataFrame({"Reported EPS": [eps for _, eps in rows]}, index=index)


class FakeTicker:
    """Stands in for ``yfinance.Ticker``; serves a canned frame, or raises."""

    def __init__(self, *, earnings_dates=None, error=None) -> None:
        self._earnings_dates = earnings_dates
        self._error = error
        self.requested_limit: int | None = None

    def get_earnings_dates(self, limit: int = 12):
        self.requested_limit = limit
        if self._error is not None:
            raise self._error
        return self._earnings_dates


def _provider(fake: FakeTicker, **kwargs) -> YfinanceEpsHistoryProvider:
    return YfinanceEpsHistoryProvider(ticker_factory=lambda _symbol: fake, **kwargs)


def test_parses_reported_quarters_oldest_first():
    # Rows out of order, with two future (NaN) quarters that must drop out.
    fake = FakeTicker(
        earnings_dates=_earnings_dates(
            [
                ("2025-10-30", _NAN),  # future — no reported EPS yet
                ("2024-11-01", 1.29),
                ("2025-02-01", 2.40),
                ("2024-08-01", 1.40),
                ("2025-08-01", _NAN),  # future
                ("2025-05-01", 1.65),
            ]
        )
    )
    history = _provider(fake).get_eps_history("AAPL")

    assert [(str(p.report_date), p.eps) for p in history] == [
        ("2024-08-01", 1.40),
        ("2024-11-01", 1.29),
        ("2025-02-01", 2.40),
        ("2025-05-01", 1.65),
    ]


def test_dedupes_by_date_keeping_a_reported_value():
    # Yahoo can list a boundary quarter twice; a single reported figure survives per date.
    fake = FakeTicker(
        earnings_dates=_earnings_dates([("2025-02-01", 2.40), ("2025-02-01", 2.41)])
    )
    history = _provider(fake).get_eps_history("AAPL")
    assert len(history) == 1
    assert history[0].report_date == date(2025, 2, 1)


def test_empty_frame_is_no_coverage_not_an_error():
    assert _provider(FakeTicker(earnings_dates=pd.DataFrame())).get_eps_history("X") == ()


def test_none_frame_is_no_coverage():
    assert _provider(FakeTicker(earnings_dates=None)).get_eps_history("X") == ()


def test_all_future_rows_yield_empty():
    fake = FakeTicker(
        earnings_dates=_earnings_dates([("2025-10-30", _NAN), ("2026-02-01", _NAN)])
    )
    assert _provider(fake).get_eps_history("X") == ()


def test_vendor_failure_becomes_domain_error():
    fake = FakeTicker(error=RuntimeError("yahoo blocked the data-centre IP"))
    with pytest.raises(StockDataUnavailable):
        _provider(fake).get_eps_history("AAPL")


def test_requests_the_configured_depth():
    fake = FakeTicker(earnings_dates=_earnings_dates([("2025-02-01", 2.40)]))
    _provider(fake, limit=40).get_eps_history("AAPL")
    assert fake.requested_limit == 40
