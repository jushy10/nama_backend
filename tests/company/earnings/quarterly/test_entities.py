from datetime import time

import pytest

from app.stocks.company.earnings.quarterly.entities import (
    EarningsSession,
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)


@pytest.mark.parametrize(
    "when, expected",
    [
        (time(6, 0), EarningsSession.BMO),  # early morning, before the open
        (time(9, 29), EarningsSession.BMO),  # one minute before the open
        (time(9, 30), EarningsSession.DURING),  # the open itself is in-session
        (time(12, 0), EarningsSession.DURING),  # midday
        (time(15, 59), EarningsSession.DURING),  # one minute before the close
        (time(16, 0), EarningsSession.AMC),  # the close itself is after-close
        (time(20, 0), EarningsSession.AMC),  # evening
        (time(0, 0), EarningsSession.UNKNOWN),  # midnight = Yahoo's "no time" placeholder
        (None, EarningsSession.UNKNOWN),  # no time at all
    ],
)
def test_from_local_time_classifies_the_session(when, expected):
    assert EarningsSession.from_local_time(when) is expected


def _quarter(fy, q, *, eps_actual=None, session=EarningsSession.UNKNOWN):
    return QuarterlyEarnings(
        fiscal_year=fy,
        fiscal_quarter=q,
        period_end=None,
        report_date=None,
        eps_actual=eps_actual,
        eps_estimate=1.0,
        eps_surprise=None,
        eps_surprise_percent=None,
        revenue_estimate=None,
        report_session=session,
    )


def test_merge_keeps_a_known_session_when_the_fresh_fetch_lost_it():
    # A refresh that came back without a usable time (UNKNOWN) must not wipe the stored
    # session — the announcement's timing doesn't change once known.
    stored = QuarterlyEarningsTimeline(
        symbol="AAPL",
        quarters=(_quarter(2026, 1, eps_actual=3.0, session=EarningsSession.AMC),),
    )
    fresh = QuarterlyEarningsTimeline(
        symbol="AAPL",
        quarters=(_quarter(2026, 1, eps_actual=3.0, session=EarningsSession.UNKNOWN),),
    )
    merged = fresh.filled_from(stored)
    assert merged.quarters[0].report_session is EarningsSession.AMC


def test_merge_prefers_a_fresh_known_session():
    stored = QuarterlyEarningsTimeline(
        symbol="AAPL",
        quarters=(_quarter(2026, 1, session=EarningsSession.UNKNOWN),),
    )
    fresh = QuarterlyEarningsTimeline(
        symbol="AAPL",
        quarters=(_quarter(2026, 1, session=EarningsSession.BMO),),
    )
    merged = fresh.filled_from(stored)
    assert merged.quarters[0].report_session is EarningsSession.BMO
