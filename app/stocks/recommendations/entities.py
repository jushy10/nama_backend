"""Entities: a stock's analyst recommendation trends.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than
reaching into the shared ``app/stocks/entities.py``, the same convention as the
earnings sub-slices). Pure and vendor-agnostic — stdlib only. They model the
sell-side buy/hold/sell split as a monthly time series: each
``RecommendationTrend`` is one month's snapshot, and ``AnalystRecommendations``
is the run of snapshots for a symbol, newest first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RecommendationTrend:
    """Analysts' buy/hold/sell split for one monthly snapshot.

    The five buckets are how many sell-side analysts held each stance that
    period (``strong_buy`` … ``strong_sell``). The derived ``score`` collapses
    them to a single consensus mean on the classic 1 (Strong Buy) … 5 (Strong
    Sell) scale — lower is more bullish — and ``consensus`` maps that mean to a
    five-step label, the same vocabulary the RSI verdict uses so the two reads
    line up.
    """

    period: date  # first day of the month this snapshot covers
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int

    @property
    def total(self) -> int:
        """How many analysts contributed a rating this period."""
        return self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell

    @property
    def score(self) -> float | None:
        """Consensus mean on the 1 (Strong Buy) … 5 (Strong Sell) scale.

        A single-number read of the split, each bucket weighted by its stance.
        ``None`` when no analyst covers the period — an empty snapshot has no
        consensus to take.
        """
        if self.total == 0:
            return None
        weighted = (
            self.strong_buy * 1
            + self.buy * 2
            + self.hold * 3
            + self.sell * 4
            + self.strong_sell * 5
        )
        return round(weighted / self.total, 2)

    @property
    def consensus(self) -> str | None:
        """The mean mapped to a five-step label (``Strong Buy`` … ``Strong Sell``).

        Half-point bands around each integer: ``<= 1.5`` Strong Buy, ``<= 2.5``
        Buy, ``<= 3.5`` Hold, ``<= 4.5`` Sell, else Strong Sell. ``None`` when
        there's no score (no coverage).
        """
        score = self.score
        if score is None:
            return None
        if score <= 1.5:
            return "Strong Buy"
        if score <= 2.5:
            return "Buy"
        if score <= 3.5:
            return "Hold"
        if score <= 4.5:
            return "Sell"
        return "Strong Sell"


@dataclass(frozen=True)
class AnalystRecommendations:
    """A run of analyst recommendation snapshots for one symbol, newest first.

    Each ``RecommendationTrend`` is a month's buy/hold/sell split, ordered newest
    first like the earnings history. ``latest`` is the current consensus and
    ``direction`` reads how it shifted from the prior month — the forward-looking
    part, since an upgrade trend tends to lead price. Best-effort: a symbol no
    analyst covers yields an empty (``is_empty``) run, not an error.
    """

    symbol: str
    trends: tuple[RecommendationTrend, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no snapshot is carried (no analyst coverage)."""
        return not self.trends

    @property
    def latest(self) -> RecommendationTrend | None:
        """The most recent snapshot, or ``None`` when there's no coverage."""
        return self.trends[0] if self.trends else None

    @property
    def direction(self) -> str | None:
        """How the consensus moved from the prior snapshot to the latest.

        ``"upgraded"`` when the latest mean is more bullish (lower) than the one
        before it, ``"downgraded"`` when less, ``"unchanged"`` when level.
        ``None`` until there are two snapshots with a score to compare — the
        signal is the *shift*, so a lone month doesn't have one yet.
        """
        if len(self.trends) < 2:
            return None
        latest = self.trends[0].score
        prior = self.trends[1].score
        if latest is None or prior is None:
            return None
        if latest < prior:
            return "upgraded"
        if latest > prior:
            return "downgraded"
        return "unchanged"
