import pandas as pd
import pytest

from app.stocks.adapters.yfinance.rating_changes_adapter import (
    YfinanceRatingChangeProvider,
)
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.company.recommendations.entities import AnalystRatingChanges


class _FakeTicker:
    def __init__(self, frame=None, error=None) -> None:
        self._frame = frame
        self._error = error

    @property
    def upgrades_downgrades(self):
        if self._error is not None:
            raise self._error
        return self._frame


def provider_with(frame=None, error=None) -> YfinanceRatingChangeProvider:
    return YfinanceRatingChangeProvider(
        ticker_factory=lambda symbol: _FakeTicker(frame, error)
    )


def _dated_frame(rows: list[tuple[str, dict]]) -> pd.DataFrame:
    index = pd.to_datetime([when for when, _ in rows])
    index.name = "GradeDate"
    return pd.DataFrame([cols for _, cols in rows], index=index)


def test_maps_rows_and_orders_newest_first():
    frame = _dated_frame(
        [
            (
                "2026-05-01",
                {"Firm": "Old Firm", "ToGrade": "Hold", "FromGrade": "Buy", "Action": "down"},
            ),
            (
                "2026-06-09",
                {
                    "Firm": "TD Cowen",
                    "ToGrade": "Buy",
                    "FromGrade": "Buy",
                    "Action": "main",
                    "currentPriceTarget": 350.0,
                    "priorPriceTarget": 335.0,
                },
            ),
        ]
    )
    changes = provider_with(frame).get_rating_changes("AAPL")
    assert isinstance(changes, AnalystRatingChanges)
    assert [c.firm for c in changes.changes] == ["TD Cowen", "Old Firm"]  # newest first
    latest = changes.latest
    assert latest.to_grade == "Buy" and latest.from_grade == "Buy"
    assert latest.is_upgrade is False and latest.action == "main"
    assert (latest.target_current, latest.target_prior) == (350.0, 335.0)
    assert changes.changes[1].is_downgrade  # Old Firm's "down" action


def test_reads_the_date_from_a_gradedate_column_when_not_indexed():
    frame = pd.DataFrame(
        [{"Firm": "A Firm", "ToGrade": "Buy", "GradeDate": "2026-06-01", "Action": "up"}]
    )
    changes = provider_with(frame).get_rating_changes("AAPL")
    assert len(changes.changes) == 1
    assert changes.latest.is_upgrade


def test_empty_and_missing_frames_are_no_coverage_not_errors():
    assert provider_with(pd.DataFrame()).get_rating_changes("ZZZZ").is_empty
    assert provider_with(None).get_rating_changes("ZZZZ").is_empty


def test_rows_without_a_firm_are_dropped():
    frame = _dated_frame(
        [
            ("2026-06-01", {"Firm": "Real Firm", "ToGrade": "Buy"}),
            ("2026-06-02", {"Firm": "", "ToGrade": "Buy"}),  # blank firm — no identity
            ("2026-06-03", {"Firm": None, "ToGrade": "Hold"}),
        ]
    )
    changes = provider_with(frame).get_rating_changes("AAPL")
    assert [c.firm for c in changes.changes] == ["Real Firm"]


def test_duplicate_firm_and_date_keeps_the_first():
    frame = _dated_frame(
        [
            ("2026-06-01", {"Firm": "Dup", "ToGrade": "Buy"}),
            ("2026-06-01", {"Firm": "Dup", "ToGrade": "Hold"}),  # same firm+date
        ]
    )
    changes = provider_with(frame).get_rating_changes("AAPL")
    assert len(changes.changes) == 1
    assert changes.latest.to_grade == "Buy"  # first row kept


def test_junk_targets_and_grades_become_none():
    frame = _dated_frame(
        [
            (
                "2026-06-01",
                {
                    "Firm": "A Firm",
                    "ToGrade": "Buy",
                    "FromGrade": "",  # initiation — no prior grade
                    "currentPriceTarget": 350.0,
                    "priorPriceTarget": 0.0,  # Yahoo's "no target"
                },
            )
        ]
    )
    change = provider_with(frame).get_rating_changes("AAPL").latest
    assert change.from_grade is None
    assert change.target_current == 350.0
    assert change.target_prior is None


def test_caps_the_stored_window_at_the_most_recent():
    # 60 consecutive daily events — only the newest _MAX_CHANGES (50) are kept, newest first.
    dates = pd.date_range("2026-01-01", periods=60, freq="D")  # ends 2026-03-01
    rows = [(d, {"Firm": f"Firm {i}", "ToGrade": "Buy"}) for i, d in enumerate(dates)]
    changes = provider_with(_dated_frame(rows)).get_rating_changes("AAPL")
    assert len(changes.changes) == YfinanceRatingChangeProvider._MAX_CHANGES
    # The newest kept is the last date; the oldest 10 fall off the cap.
    assert changes.latest.published_at.isoformat() == "2026-03-01"


def test_vendor_failure_raises_unavailable():
    with pytest.raises(StockDataUnavailable):
        provider_with(error=RuntimeError("rate limited")).get_rating_changes("AAPL")


def test_ticker_construction_failure_raises_unavailable():
    def _boom(symbol):
        raise RuntimeError("no network")

    provider = YfinanceRatingChangeProvider(ticker_factory=_boom)
    with pytest.raises(StockDataUnavailable):
        provider.get_rating_changes("AAPL")
