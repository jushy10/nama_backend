"""Tests for the quarterly-earnings entities — the intrinsic rules.

Pure, no I/O: the beat rule (meeting counts, unknowable stays None), the reported/upcoming
split on ``eps_actual``, and the timeline's ``past`` / ``future`` / ``is_empty`` views.
"""

from datetime import date

from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)


def _quarter(fy: int, fq: int, *, actual=None, estimate=None) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=fy,
        fiscal_quarter=fq,
        period_end=date(fy, fq * 3, 28),
        report_date=None,
        eps_actual=actual,
        eps_estimate=estimate,
        eps_surprise=None,
        eps_surprise_percent=None,
        revenue_estimate=None,
    )


def test_is_reported_tracks_the_actual():
    assert _quarter(2025, 1, actual=2.0, estimate=1.9).is_reported is True
    assert _quarter(2026, 1, estimate=2.5).is_reported is False  # upcoming


def test_beat_counts_meeting_as_a_beat():
    assert _quarter(2025, 1, actual=2.2, estimate=2.0).beat is True
    assert _quarter(2025, 1, actual=2.0, estimate=2.0).beat is True  # meeting counts
    assert _quarter(2025, 1, actual=1.8, estimate=2.0).beat is False


def test_beat_is_none_when_unknowable():
    assert _quarter(2026, 1, estimate=2.0).beat is None  # no actual yet
    assert _quarter(2025, 1, actual=2.0).beat is None  # no estimate


def test_timeline_splits_past_and_future_preserving_order():
    reported_new = _quarter(2025, 4, actual=3.0, estimate=2.8)
    reported_old = _quarter(2025, 3, actual=2.5, estimate=2.6)
    upcoming_soon = _quarter(2026, 1, estimate=3.1)
    upcoming_later = _quarter(2026, 2, estimate=3.3)
    timeline = QuarterlyEarningsTimeline(
        symbol="AAPL",
        quarters=(reported_new, reported_old, upcoming_soon, upcoming_later),
    )

    assert timeline.past == (reported_new, reported_old)
    assert timeline.future == (upcoming_soon, upcoming_later)
    assert timeline.is_empty is False


def test_empty_timeline():
    timeline = QuarterlyEarningsTimeline(symbol="ZZZZ", quarters=())
    assert timeline.is_empty is True
    assert timeline.past == () and timeline.future == ()
