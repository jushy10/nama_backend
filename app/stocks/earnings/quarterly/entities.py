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
