from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class AnnualEarnings:
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
    # The reported year's free-cash-flow and operating-cash-flow *per share* (the year's
    # total from the cash-flow statement over that year's diluted average shares, on the
    # trading currency). Reported years only — cash flow is a reported fact, so an upcoming
    # year carries neither. Best-effort enrichment (like net_income): a blocked cash-flow
    # fetch leaves them None without sinking the year. They power the ticker card's live
    # price/FCF, FCF-yield and OCF-yield reads, priced on the card's live quote.
    fcf_per_share: float | None = None  # reported free cash flow per share (trading currency)
    ocf_per_share: float | None = None  # reported operating cash flow per share (trading currency)

    @property
    def is_reported(self) -> bool:
        return self.eps_actual is not None


@dataclass(frozen=True)
class AnnualEarningsTimeline:
    symbol: str
    years: tuple[AnnualEarnings, ...]

    @property
    def is_empty(self) -> bool:
        return not self.years

    @property
    def past(self) -> tuple[AnnualEarnings, ...]:
        return tuple(y for y in self.years if y.is_reported)

    @property
    def future(self) -> tuple[AnnualEarnings, ...]:
        return tuple(y for y in self.years if not y.is_reported)

    @property
    def latest_revenue_growth_yoy(self) -> float | None:
        reported = self.past
        if len(reported) < 2:
            return None
        return _growth_percent(reported[-1].revenue_actual, reported[-2].revenue_actual)

    @property
    def latest_eps_growth_yoy(self) -> float | None:
        reported = self.past
        if len(reported) < 2:
            return None
        return _growth_percent(
            reported[-1].eps_actual_consensus, reported[-2].eps_actual_consensus
        )

    @property
    def latest_fcf_per_share(self) -> float | None:
        reported = self.past
        return reported[-1].fcf_per_share if reported else None

    @property
    def latest_ocf_per_share(self) -> float | None:
        reported = self.past
        return reported[-1].ocf_per_share if reported else None

    @property
    def latest_fcf_growth_yoy(self) -> float | None:
        reported = self.past
        if len(reported) < 2:
            return None
        return _growth_percent(reported[-1].fcf_per_share, reported[-2].fcf_per_share)

    @property
    def forward_revenue_growth_yoy(self) -> float | None:
        upcoming = self.future
        if len(upcoming) < 2:
            return None
        return _growth_percent(upcoming[1].revenue_estimate, upcoming[0].revenue_estimate)

    @property
    def forward_eps_growth_yoy(self) -> float | None:
        upcoming = self.future
        if len(upcoming) < 2:
            return None
        return _growth_percent(upcoming[1].eps_estimate, upcoming[0].eps_estimate)

    def filled_from(
        self, stored: "AnnualEarningsTimeline | None"
    ) -> "AnnualEarningsTimeline":
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
    if current is None or prior is None or prior <= 0:
        return None
    return round((current - prior) / prior * 100, 2)


def _merged_year(fresh: AnnualEarnings, stored: AnnualEarnings | None) -> AnnualEarnings:
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
            # Cash-flow per share rides Yahoo's hardest-gated endpoint (like net income),
            # so a degraded refresh keeps the stored reported figure — it never changes.
            fcf_per_share=(
                fresh.fcf_per_share
                if fresh.fcf_per_share is not None
                else stored.fcf_per_share
            ),
            ocf_per_share=(
                fresh.ocf_per_share
                if fresh.ocf_per_share is not None
                else stored.ocf_per_share
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
