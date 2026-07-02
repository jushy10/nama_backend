"""Entities: a stock's per-quarter earnings timeline.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than
reaching into the shared ``app/stocks/entities.py``). Pure and vendor-agnostic — they
import stdlib only and model both halves of the timeline in one shape:

- **Reported** quarters carry the actual EPS, the consensus estimate that preceded it,
  and the surprise (``eps_actual`` is set).
- **Upcoming** quarters carry the forward consensus (``eps_actual`` is ``None`` — not yet
  reported) and, for the nearest quarters, a forward revenue estimate.

``eps_actual is None`` is the single discriminator between the two, mirroring how the
stocks slice's ``EarningsSurprise`` already unions an actual with the estimate that
preceded it. Any field beyond the fiscal identity may be ``None`` when the source didn't
cover it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class QuarterlyEarnings:
    """One fiscal quarter: the estimate going in and, once reported, the actual.

    ``fiscal_year`` / ``fiscal_quarter`` are the quarter's identity (and the row's
    unique key). ``eps_actual`` is ``None`` until the quarter is reported, so it also
    tells reported quarters apart from upcoming ones. ``revenue_actual`` is the reported
    revenue for a past quarter and ``revenue_estimate`` the forward consensus for an
    upcoming one, so the two are naturally exclusive: a reported quarter carries the
    actual, an upcoming one the estimate (populated only for the nearest quarters Yahoo
    publishes). All revenue figures are raw (e.g. USD).
    """

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

    @property
    def is_reported(self) -> bool:
        """Whether the quarter has been reported (``eps_actual`` is known)."""
        return self.eps_actual is not None

    @property
    def beat(self) -> bool | None:
        """Whether the quarter met or beat its estimate (``actual >= estimate``).

        Meeting counts as a beat. ``None`` when either side is missing (e.g. an
        upcoming quarter), so an unknowable quarter stays distinct from a real miss.
        """
        if self.eps_actual is None or self.eps_estimate is None:
            return None
        return self.eps_actual >= self.eps_estimate


@dataclass(frozen=True)
class QuarterlyEarningsTimeline:
    """A stock's recent reported quarters plus its upcoming ones.

    ``quarters`` runs in chronological order — ascending by ``(fiscal_year,
    fiscal_quarter)``, so the oldest reported quarter leads through to the furthest
    upcoming one. The ``past`` / ``future`` views split it on ``is_reported`` while
    preserving that order (past = oldest→newest reported, future = soonest→furthest
    upcoming). Best-effort: an uncovered symbol yields an empty (``is_empty``) timeline
    rather than an error, the same contract the annual slice uses.
    """

    symbol: str
    quarters: tuple[QuarterlyEarnings, ...]

    @property
    def is_empty(self) -> bool:
        """True when no quarter — reported or upcoming — is carried."""
        return not self.quarters

    @property
    def past(self) -> tuple[QuarterlyEarnings, ...]:
        """The reported quarters, oldest first."""
        return tuple(q for q in self.quarters if q.is_reported)

    @property
    def future(self) -> tuple[QuarterlyEarnings, ...]:
        """The upcoming (not-yet-reported) quarters, soonest first."""
        return tuple(q for q in self.quarters if not q.is_reported)

    def filled_from(
        self, stored: "QuarterlyEarningsTimeline | None"
    ) -> "QuarterlyEarningsTimeline":
        """This (freshly fetched) timeline with its holes filled from a stored one.

        The refresh guard: Yahoo intermittently blocks parts of a fetch from
        data-centre IPs (the income-statement revenue hardest), and a refresh
        rewrites a stock's whole window — so without this, a degraded fetch would
        overwrite good stored figures with ``None``. Reported figures never change
        once published, so carrying the stored value forward is always correct:

        - a fresh quarter's missing figures are taken from the stored quarter with
          the same fiscal key (a reported quarter's estimate fields excluded —
          ``revenue_estimate`` stays an upcoming-quarter concept);
        - a stored *reported* quarter is never downgraded — it wins outright over a
          fresh not-yet-reported row for the same key;
        - stored *reported* quarters absent from the fresh window are retained,
          capped to the newest ``max(fresh, stored)`` reported counts so outage
          protection never grows the served window run over run (stored *upcoming*
          quarters are not retained — consensus legitimately rolls off).
        """
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


def _merged_quarter(
    fresh: QuarterlyEarnings, stored: QuarterlyEarnings | None
) -> QuarterlyEarnings:
    """One fiscal quarter merged for a refresh: fresh values win, stored values fill
    the holes. A stored reported quarter beats a fresh not-yet-reported one outright
    (a published actual never un-reports)."""
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
    )
