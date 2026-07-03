"""Entities: a stock's forward-PEG read.

Slice-local domain object (this sub-slice keeps its own ``entities`` rather than
reaching into the shared ``app/stocks/entities.py``, the same convention as the
earnings and recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

It models the forward analogue of the trailing PEG: the forward P/E (today's price
against next fiscal year's consensus EPS) divided by the EPS growth analysts expect
the year after that (FY1 → FY2). Where the trailing PEG divides by growth *already
reported* — which a cyclical rebound can inflate into the hundreds of percent,
pinning the ratio near zero — this one divides by growth analysts still *expect*,
so it answers "is today's price justified by what's supposed to come" rather than
"by what already happened".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TickerValuation:
    """One symbol's forward-PEG inputs at today's price.

    The two legs arrive precomputed (the use case derives them from the live quote
    and the stored consensus estimates); the entity owns the rule that combines
    them. The legs are optional: estimates are consensus coverage, and a symbol
    without stored forward years simply carries ``None``s around a live price.
    """

    symbol: str
    price: float  # the live price the multiple was taken at
    forward_pe: float | None  # price / FY1 consensus EPS
    forward_eps_growth: float | None  # FY1 -> FY2 consensus EPS growth (percent)

    @property
    def forward_peg(self) -> float | None:
        """Forward PEG: forward P/E divided by expected EPS growth (percent).

        The forward cousin of ``KeyMetrics.peg`` with the same reading (near 1.0
        means the price roughly matches growth) and the same guard: ``None``
        unless both legs are present and positive — a non-positive multiple or
        expected shrinkage makes the ratio meaningless. The denominator is a
        single FY1→FY2 leg (Yahoo's forward ceiling), not the classic five-year
        rate, so one boom-year estimate can still flatter it.
        """
        if self.forward_pe is None or self.forward_eps_growth is None:
            return None
        if self.forward_pe <= 0 or self.forward_eps_growth <= 0:
            return None
        return round(self.forward_pe / self.forward_eps_growth, 2)
