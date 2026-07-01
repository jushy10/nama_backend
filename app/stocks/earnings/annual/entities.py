"""Entities: a stock's per-year (annual) earnings timeline.

Slice-local domain objects — like the quarterly slice, this sub-slice keeps its own
``entities`` rather than reaching into the shared ``app/stocks/entities.py``. Pure and
vendor-agnostic (stdlib only), modeling both halves of the timeline in one shape:

- **Reported** years carry the actual EPS, the reported revenue, and net income
  (``eps_actual`` is set).
- **Upcoming** years carry the forward consensus EPS and revenue (``eps_actual`` is
  ``None`` — not yet reported).

``eps_actual is None`` is the single discriminator between the two, mirroring the
quarterly slice. The deliberate divergence from quarterly: there is **no per-year
surprise or beat**. Yahoo's estimate-vs-actual history is per-quarter, so there is no
historical *annual* estimate to compare a reported year against — a reported year carries
an actual with no estimate. Any field beyond the fiscal identity may be ``None`` when the
source didn't cover it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class AnnualEarnings:
    """One fiscal year: the estimate for an upcoming year, or the actuals for a reported one.

    ``fiscal_year`` is the year's identity (and the row's unique key). ``eps_actual`` is
    ``None`` until the year is reported, so it also tells reported years apart from
    upcoming ones. ``revenue_actual`` / ``net_income`` are the reported figures for a past
    year and ``revenue_estimate`` the forward consensus for an upcoming one, so the actual
    and estimate sides are naturally exclusive. All revenue and income figures are raw
    (e.g. USD).
    """

    fiscal_year: int
    period_end: date | None  # fiscal year end
    eps_actual: float | None  # reported diluted EPS; None ⇒ not yet reported (upcoming year)
    eps_estimate: float | None  # forward consensus EPS (upcoming years)
    revenue_actual: float | None  # reported revenue (raw), reported years only
    revenue_estimate: float | None  # forward consensus revenue (raw), upcoming years only
    net_income: float | None = None  # reported net income (raw), reported years only

    @property
    def is_reported(self) -> bool:
        """Whether the year has been reported (``eps_actual`` is known)."""
        return self.eps_actual is not None


@dataclass(frozen=True)
class AnnualEarningsTimeline:
    """A stock's recent reported fiscal years plus its upcoming (estimated) ones.

    ``years`` runs in chronological order — ascending by ``fiscal_year``, so the oldest
    reported year leads through to the furthest upcoming one. The ``past`` / ``future``
    views split it on ``is_reported`` while preserving that order (past = oldest→newest
    reported, future = soonest→furthest upcoming). Best-effort: an uncovered symbol yields
    an empty (``is_empty``) timeline rather than an error, the same contract the estimates
    and quarterly slices use.
    """

    symbol: str
    years: tuple[AnnualEarnings, ...]

    @property
    def is_empty(self) -> bool:
        """True when no year — reported or upcoming — is carried."""
        return not self.years

    @property
    def past(self) -> tuple[AnnualEarnings, ...]:
        """The reported years, oldest first."""
        return tuple(y for y in self.years if y.is_reported)

    @property
    def future(self) -> tuple[AnnualEarnings, ...]:
        """The upcoming (not-yet-reported) years, soonest first."""
        return tuple(y for y in self.years if not y.is_reported)
