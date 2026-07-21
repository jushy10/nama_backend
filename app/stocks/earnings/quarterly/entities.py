from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from enum import Enum


class EarningsSession(str, Enum):
    BMO = "bmo"  # before market open (< 9:30 ET)
    AMC = "amc"  # after market close (>= 16:00 ET)
    DURING = "during"  # intraday, between the open and close (rare)
    UNKNOWN = "unknown"  # no usable announcement time

    @classmethod
    def from_local_time(cls, when: time | None) -> "EarningsSession":
        if when is None or when == time(0, 0):
            return cls.UNKNOWN
        if when < time(9, 30):
            return cls.BMO
        if when >= time(16, 0):
            return cls.AMC
        return cls.DURING


@dataclass(frozen=True)
class QuarterlyEarnings:
    fiscal_year: int
    fiscal_quarter: int  # 1–4
    period_end: date | None  # fiscal period end
    report_date: date | None  # earnings announcement date (past = actual, future = expected)
    eps_actual: float | None  # reported EPS; None ⇒ not yet reported (an upcoming quarter)
    eps_estimate: float | None  # consensus EPS estimate
    eps_surprise: float | None  # actual - estimate (EPS); reported quarters only
    eps_surprise_percent: float | None  # surprise as a percent of the estimate
    revenue_estimate: float | None  # forward consensus revenue (raw), nearest quarters only
    revenue_actual: float | None = None  # reported revenue (raw), reported quarters only
    # Market timing of the announcement (before open / after close), from its time-of-day;
    # UNKNOWN when Yahoo publishes no usable time. Applies to reported and upcoming alike.
    report_session: EarningsSession = EarningsSession.UNKNOWN

    @property
    def is_reported(self) -> bool:
        return self.eps_actual is not None

    @property
    def beat(self) -> bool | None:
        if self.eps_actual is None or self.eps_estimate is None:
            return None
        return self.eps_actual >= self.eps_estimate


@dataclass(frozen=True)
class QuarterlyEarningsTimeline:
    symbol: str
    quarters: tuple[QuarterlyEarnings, ...]

    @property
    def is_empty(self) -> bool:
        return not self.quarters

    @property
    def past(self) -> tuple[QuarterlyEarnings, ...]:
        return tuple(q for q in self.quarters if q.is_reported)

    @property
    def future(self) -> tuple[QuarterlyEarnings, ...]:
        return tuple(q for q in self.quarters if not q.is_reported)

    @property
    def ttm_eps(self) -> float | None:
        reported = self.past
        if len(reported) < 4:
            return None
        return sum(q.eps_actual for q in reported[-4:])

    def filled_from(
        self, stored: "QuarterlyEarningsTimeline | None"
    ) -> "QuarterlyEarningsTimeline":
        if stored is None or stored.is_empty:
            return self
        stored_by_key = {(q.fiscal_year, q.fiscal_quarter): q for q in stored.quarters}
        fresh_keys = {(q.fiscal_year, q.fiscal_quarter) for q in self.quarters}
        merged = [
            _merged_quarter(q, stored_by_key.get((q.fiscal_year, q.fiscal_quarter)))
            for q in self.quarters
        ]
        retained = [
            q
            for q in stored.past
            if (q.fiscal_year, q.fiscal_quarter) not in fresh_keys
        ]
        combined = merged + retained
        reported = sorted(
            (q for q in combined if q.is_reported),
            key=lambda q: (q.fiscal_year, q.fiscal_quarter),
        )
        cap = max(len(self.past), len(stored.past))
        reported = reported[-cap:] if cap else []
        upcoming = [q for q in combined if not q.is_reported]
        quarters = sorted(
            reported + upcoming, key=lambda q: (q.fiscal_year, q.fiscal_quarter)
        )
        return QuarterlyEarningsTimeline(symbol=self.symbol, quarters=tuple(quarters))


def _session_or(
    fresh: EarningsSession, stored: EarningsSession
) -> EarningsSession:
    return fresh if fresh is not EarningsSession.UNKNOWN else stored


def _merged_quarter(
    fresh: QuarterlyEarnings, stored: QuarterlyEarnings | None
) -> QuarterlyEarnings:
    if stored is None:
        return fresh
    if stored.is_reported and not fresh.is_reported:
        return stored
    if fresh.is_reported:
        return QuarterlyEarnings(
            fiscal_year=fresh.fiscal_year,
            fiscal_quarter=fresh.fiscal_quarter,
            period_end=fresh.period_end or stored.period_end,
            report_date=fresh.report_date or stored.report_date,
            eps_actual=fresh.eps_actual,
            eps_estimate=(
                fresh.eps_estimate
                if fresh.eps_estimate is not None
                else stored.eps_estimate
            ),
            eps_surprise=(
                fresh.eps_surprise
                if fresh.eps_surprise is not None
                else stored.eps_surprise
            ),
            eps_surprise_percent=(
                fresh.eps_surprise_percent
                if fresh.eps_surprise_percent is not None
                else stored.eps_surprise_percent
            ),
            revenue_estimate=fresh.revenue_estimate,
            revenue_actual=(
                fresh.revenue_actual
                if fresh.revenue_actual is not None
                else stored.revenue_actual
            ),
            report_session=_session_or(fresh.report_session, stored.report_session),
        )
    # Both upcoming: fill the consensus holes.
    return QuarterlyEarnings(
        fiscal_year=fresh.fiscal_year,
        fiscal_quarter=fresh.fiscal_quarter,
        period_end=fresh.period_end or stored.period_end,
        report_date=fresh.report_date or stored.report_date,
        eps_actual=None,
        eps_estimate=(
            fresh.eps_estimate
            if fresh.eps_estimate is not None
            else stored.eps_estimate
        ),
        eps_surprise=None,
        eps_surprise_percent=None,
        revenue_estimate=(
            fresh.revenue_estimate
            if fresh.revenue_estimate is not None
            else stored.revenue_estimate
        ),
        revenue_actual=None,
        report_session=_session_or(fresh.report_session, stored.report_session),
    )
