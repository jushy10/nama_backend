"""Entities: a stock's analyst coverage — recommendation trends, price targets, rating changes.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than
reaching into the shared ``app/stocks/entities.py``, the same convention as the
earnings sub-slices). Pure and vendor-agnostic — stdlib only. Three related reads of
what the sell-side thinks:

- the buy/hold/sell split as a monthly time series — each ``RecommendationTrend`` is
  one month's snapshot and ``AnalystRecommendations`` the run for a symbol, newest
  first, now also carrying the current ``AnalystPriceTargets`` consensus;
- ``AnalystPriceTargets`` — the consensus 12-month price target (mean/high/low/median),
  a single current snapshot; and
- ``RatingChange`` / ``AnalystRatingChanges`` — the discrete upgrade/downgrade *events*
  behind the trend, one per firm action, newest first.
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
class AnalystPriceTargets:
    """The sell-side's consensus 12-month price target for a stock.

    A single *current* snapshot — Yahoo publishes no history — of where analysts see
    the price a year out: the ``mean`` and ``median`` consensus, and the ``high``/``low``
    range across the estimates. Every field is optional and ``None`` when absent (never
    a fabricated zero); a stock no analyst targets carries an empty (``is_empty``) block.
    Rides on ``AnalystRecommendations`` as best-effort enrichment beside the trend run.
    """

    mean: float | None = None
    high: float | None = None
    low: float | None = None
    median: float | None = None

    @property
    def is_empty(self) -> bool:
        """True when no target figure is carried (no price-target coverage)."""
        return (
            self.mean is None
            and self.high is None
            and self.low is None
            and self.median is None
        )

    def upside_percent(self, price: float | None) -> float | None:
        """How far the ``mean`` target sits above (or below) ``price``, in percent.

        ``(mean - price) / price * 100`` — the headline read of a price target, pairing
        the consensus with a live quote the way ``AnalystEstimates.forward_pe`` pairs the
        forward EPS with one. ``None`` without a mean target or a positive price to
        anchor on (the caller supplies the quote; this entity holds no price of its own).
        """
        if self.mean is None or price is None or price <= 0:
            return None
        return round((self.mean - price) / price * 100, 2)


@dataclass(frozen=True)
class AnalystRecommendations:
    """A run of analyst recommendation snapshots for one symbol, newest first.

    Each ``RecommendationTrend`` is a month's buy/hold/sell split, ordered newest
    first like the earnings history. ``latest`` is the current consensus and
    ``direction`` reads how it shifted from the prior month — the forward-looking
    part, since an upgrade trend tends to lead price. ``price_targets`` is the current
    consensus target block (``None`` when Yahoo serves none), best-effort enrichment
    that rides alongside the run. Best-effort overall: a symbol no analyst covers yields
    an empty (``is_empty``) run, not an error.
    """

    symbol: str
    trends: tuple[RecommendationTrend, ...] = ()
    price_targets: AnalystPriceTargets | None = None

    @property
    def is_empty(self) -> bool:
        """True when no monthly snapshot is carried (no analyst coverage).

        Keyed on the trend run — the primary series the store hangs on — not on
        ``price_targets``, which is optional enrichment with nowhere to be stored when
        there's no monthly row to attach it to.
        """
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


@dataclass(frozen=True)
class RatingChange:
    """One sell-side rating action on a stock — the discrete event behind the trend.

    A single firm's published change on ``published_at``: its ``action`` (Yahoo's grade
    action — ``up`` upgrade, ``down`` downgrade, ``init`` initiation, ``main`` maintain,
    ``reit`` reiterate), the ``from_grade`` → ``to_grade`` move, and the price target it
    set (``target_current``) versus the one it replaced (``target_prior``). Grades and
    targets are optional — an initiation has no prior grade, a rating-only note no target.
    Where a ``RecommendationTrend`` is the monthly *aggregate*, this is one analyst's
    individual action; many can land in a single month.
    """

    firm: str
    published_at: date
    action: str | None = None
    from_grade: str | None = None
    to_grade: str | None = None
    target_current: float | None = None
    target_prior: float | None = None

    @property
    def is_upgrade(self) -> bool:
        """True when the firm raised its rating (Yahoo's ``up`` action)."""
        return (self.action or "").strip().lower() == "up"

    @property
    def is_downgrade(self) -> bool:
        """True when the firm cut its rating (Yahoo's ``down`` action)."""
        return (self.action or "").strip().lower() == "down"


@dataclass(frozen=True)
class AnalystRatingChanges:
    """A run of a stock's individual rating actions, newest first.

    The upgrade/downgrade feed — the events that, aggregated by month, become the
    ``RecommendationTrend`` series. Best-effort like the rest of the slice: a symbol with
    no published actions yields an empty (``is_empty``) run, not an error.
    """

    symbol: str
    changes: tuple[RatingChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no rating action is carried (no coverage / none published)."""
        return not self.changes

    @property
    def latest(self) -> RatingChange | None:
        """The most recent action, or ``None`` when there are none."""
        return self.changes[0] if self.changes else None
