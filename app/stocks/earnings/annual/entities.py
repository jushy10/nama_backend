"""Entities: a stock's per-year (annual) earnings timeline.

Slice-local domain objects — like the quarterly slice, this sub-slice keeps its own
``entities`` rather than reaching into the shared ``app/stocks/entities.py``. Pure and
vendor-agnostic (stdlib only), modeling both halves of the timeline in one shape:

- **Reported** years carry the actual EPS, the reported revenue, and net income
  (``eps_actual`` is set).
- **Upcoming** years carry the forward consensus EPS and revenue (``eps_actual`` is
  ``None`` — not yet reported).

Reported years may additionally carry ``eps_actual_consensus`` — the year's actual EPS on
the analyst-consensus (adjusted) basis, i.e. the same basis the upcoming years'
``eps_estimate`` is quoted on. ``eps_actual`` is GAAP diluted EPS (from the income
statement), which for high-SBC companies sits well below the adjusted figure analysts
estimate against; a client walking from a reported year's actual to an upcoming year's
estimate needs both ends on one basis, and ``eps_actual_consensus`` is that anchor.
Best-effort (``None`` when the quarterly history couldn't be assembled).

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
    # Reported actual EPS on the analyst-consensus (adjusted) basis — the sum of the fiscal
    # year's four quarterly "Reported EPS" figures, comparable with eps_estimate (which is
    # quoted on that basis, unlike the GAAP-diluted eps_actual). Reported years only.
    eps_actual_consensus: float | None = None

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
    an empty (``is_empty``) timeline rather than an error, the same contract the
    quarterly slice uses.
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

    @property
    def latest_revenue_growth_yoy(self) -> float | None:
        """Trailing YoY revenue growth (percent): the newest reported year's revenue
        over the prior reported year's.

        A *trailing* indicator — both legs are already-reported actuals, so it says
        what growth the business has shown, not what's expected (the forward analogue
        is ``AnalystEstimates.forward_revenue_growth``). ``revenue_actual`` is a single
        basis, so no basis caveat applies. ``None`` with fewer than two reported years,
        a missing figure, or a non-positive prior (growth off a non-positive base is
        meaningless)."""
        reported = self.past
        if len(reported) < 2:
            return None
        return _growth_percent(reported[-1].revenue_actual, reported[-2].revenue_actual)

    @property
    def latest_eps_growth_yoy(self) -> float | None:
        """Trailing YoY EPS growth (percent) on the analyst-consensus (adjusted) basis:
        the newest reported year's ``eps_actual_consensus`` over the prior reported
        year's.

        Deliberately the *consensus* basis on both legs — not the GAAP-diluted
        ``eps_actual`` — so the number is real growth, not a GAAP-vs-adjusted artifact
        (the same reason ``eps_actual_consensus`` exists). Trailing, like the revenue
        counterpart. ``None`` with fewer than two reported years, a missing
        consensus figure (best-effort — often unfilled), or a non-positive prior."""
        reported = self.past
        if len(reported) < 2:
            return None
        return _growth_percent(
            reported[-1].eps_actual_consensus, reported[-2].eps_actual_consensus
        )

    def filled_from(
        self, stored: "AnnualEarningsTimeline | None"
    ) -> "AnnualEarningsTimeline":
        """This (freshly fetched) timeline with its holes filled from a stored one.

        The refresh guard: the reported half comes from Yahoo's income-statement
        endpoint, which it intermittently blocks from data-centre IPs — a blocked
        fetch yields a forward-only timeline, and a refresh rewrites a stock's
        whole window, so without this it would erase the stored reported history.
        Reported figures never change once published, so carrying the stored value
        forward is always correct:

        - a fresh year's missing figures are taken from the stored year with the
          same ``fiscal_year`` (estimate fields only between upcoming years — a
          reported year keeps carrying no estimate, the slice's contract);
        - a stored *reported* year is never downgraded — it wins outright over a
          fresh not-yet-reported row for the same year;
        - stored *reported* years absent from the fresh window are retained, capped
          to the newest ``max(fresh, stored)`` reported counts so outage protection
          never grows the served window run over run (stored *upcoming* years are
          not retained — consensus legitimately rolls off).
        """
        if stored is None or stored.is_empty:
            return self
        stored_by_year = {y.fiscal_year: y for y in stored.years}
        fresh_years = {y.fiscal_year for y in self.years}
        merged = [_merged_year(y, stored_by_year.get(y.fiscal_year)) for y in self.years]
        retained = [y for y in stored.past if y.fiscal_year not in fresh_years]
        combined = merged + retained
        reported = sorted(
            (y for y in combined if y.is_reported), key=lambda y: y.fiscal_year
        )
        cap = max(len(self.past), len(stored.past))
        reported = reported[-cap:] if cap else []
        upcoming = [y for y in combined if not y.is_reported]
        years = sorted(reported + upcoming, key=lambda y: y.fiscal_year)
        return AnnualEarningsTimeline(symbol=self.symbol, years=tuple(years))


def _growth_percent(current: float | None, prior: float | None) -> float | None:
    """One-year point-to-point growth (percent): the change from ``prior`` to
    ``current``. A plain percentage gain/loss, not a compounded rate. ``None`` unless
    both legs are present and ``prior`` is positive — a non-positive or missing base
    makes the ratio meaningless. Mirrors the shared ``_forward_one_year_growth`` guard."""
    if current is None or prior is None or prior <= 0:
        return None
    return round((current - prior) / prior * 100, 2)


def _merged_year(fresh: AnnualEarnings, stored: AnnualEarnings | None) -> AnnualEarnings:
    """One fiscal year merged for a refresh: fresh values win, stored values fill the
    holes. A stored reported year beats a fresh not-yet-reported one outright (a
    published actual never un-reports)."""
    if stored is None:
        return fresh
    if stored.is_reported and not fresh.is_reported:
        return stored
    if fresh.is_reported:
        return AnnualEarnings(
            fiscal_year=fresh.fiscal_year,
            period_end=fresh.period_end or stored.period_end,
            eps_actual=fresh.eps_actual,
            eps_estimate=fresh.eps_estimate,
            revenue_actual=(
                fresh.revenue_actual
                if fresh.revenue_actual is not None
                else stored.revenue_actual
            ),
            revenue_estimate=fresh.revenue_estimate,
            net_income=(
                fresh.net_income if fresh.net_income is not None else stored.net_income
            ),
            eps_actual_consensus=(
                fresh.eps_actual_consensus
                if fresh.eps_actual_consensus is not None
                else stored.eps_actual_consensus
            ),
        )
    # Both upcoming: fill the consensus holes.
    return AnnualEarnings(
        fiscal_year=fresh.fiscal_year,
        period_end=fresh.period_end or stored.period_end,
        eps_actual=None,
        eps_estimate=(
            fresh.eps_estimate
            if fresh.eps_estimate is not None
            else stored.eps_estimate
        ),
        revenue_actual=None,
        revenue_estimate=(
            fresh.revenue_estimate
            if fresh.revenue_estimate is not None
            else stored.revenue_estimate
        ),
        net_income=None,
    )
