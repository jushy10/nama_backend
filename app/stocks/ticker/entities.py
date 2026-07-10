"""Entities: a stock's valuation read (trailing + forward) and its options-market read.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than
reaching into the shared ``app/stocks/entities.py``, the same convention as the
earnings and recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

``TickerValuation`` models the card's trailing P/E on the analyst-consensus
(adjusted) EPS basis — today's price over the sum of the 4 newest reported
quarters' consensus-basis EPS (not a vendor's GAAP TTM read, so it lines up with
the forward consensus figures the AI analysis context is built on).

``OptionContract`` + ``TickerOptionsMetrics`` model what the options market says
about the stock: how nervous it is (at-the-money implied volatility), how big a
swing is priced in (the ATM straddle as a percent of spot), what downside
protection costs (an ATM put a quarter out), and which way the day's bets lean
(put/call volume). All are *reads on the underlying stock* for a buyer sizing up
an entry — not a chain browser for options traders — which is why the card serves
these four derived figures and not the contracts themselves.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Mapping, Sequence

# A trailing-twelve-month EPS is the sum of this many reported quarters — the window the
# P/E-history walk rolls over the reported-EPS run (and the warm-up before its first point).
TTM_QUARTERS = 4

# How stale a close may be to price an earnings release: a release can land on a weekend or
# holiday, so the P/E point takes the most recent session's close within this many days.
_MAX_PRICE_LAG_DAYS = 7

# Percentile thresholds that bucket the current multiple against its own history. At or below
# the 25th percentile the stock has rarely been cheaper (a "cheap vs history" read); at or above
# the 75th it has rarely been dearer. The middle half is "fair" — no signal. They bound the
# interquartile band the FE shades, so the buckets line up with ``p25_pe`` / ``p75_pe``.
_CHEAP_PERCENTILE = 25.0
_EXPENSIVE_PERCENTILE = 75.0

# A cyclical stock's earnings can collapse to a near-zero (but still positive) trailing sum at a
# trough — Seagate/STX is the type case — and a tiny denominator balloons the trailing P/E into a
# spike that says nothing about how the market values the business. Two self-scaling screens keep
# those spikes from cluttering the chart or distorting the signal; both are *relative to the
# stock's own history*, deliberately not an absolute P/E cutoff (which would punish a genuine
# high-growth multiple):
#   * trough-earnings — a release whose trailing-twelve-month EPS is below this fraction of the
#     series' median TTM EPS is a trough. Its point is dropped from the chart, and when the
#     *latest* release is a trough the valuation signal is suppressed (the multiple is not
#     meaningful, not "expensive").
_TROUGH_EPS_FRACTION = 0.3
#   * far-outlier — a P/E past the Tukey far-outlier fence (Q3 + this × IQR) is dropped from the
#     chart too, catching a spike from any cause (a near-trough the fraction screen just misses,
#     or a price bubble). Chart-only: a high *current* multiple on healthy earnings still reads
#     "expensive", so this fence never drives the signal.
_OUTLIER_IQR_MULT = 3.0


@dataclass(frozen=True)
class TickerValuation:
    """One symbol's trailing valuation multiples at today's price.

    The per-share legs arrive precomputed (the use case derives ``ttm_eps`` from
    the quarterly-earnings timeline and takes ``fcf_per_share`` off the
    fundamentals call); the entity owns the rules that turn them into multiples.
    Each leg is optional — the TTM sum needs four cached quarters, and the FCF
    figure needs a covering fundamentals vendor — so a symbol missing one simply
    carries ``None`` around a live price.

    ``ttm_eps`` is deliberately on the *consensus (adjusted)* basis — the sum of
    the 4 newest reported quarters' "Reported EPS" — not GAAP diluted, so the
    trailing multiple sits on the same basis as the forward consensus figures the
    AI analysis context is built on (a GAAP trailing leg would make any walk
    between them a basis artifact rather than a story about growth).

    ``fcf_per_share`` is the vendor's trailing free-cash-flow per share (the same
    ``KeyMetrics.fcf_per_share`` the snapshot carries). Pricing it here — rather
    than trusting the vendor's own P/FCF snapshot — keeps the FCF multiples on the
    card's *live* quote, exactly as ``trailing_pe`` prices the consensus EPS.
    """

    symbol: str
    price: float  # the live price the multiples were taken at
    ttm_eps: float | None = None  # trailing 12m EPS, consensus basis (4 reported quarters)
    fcf_per_share: float | None = None  # trailing free cash flow per share (fundamentals vendor)

    @property
    def trailing_pe(self) -> float | None:
        """Trailing P/E on the consensus basis: price over ``ttm_eps``.

        ``None`` unless both legs are positive — a loss-making trailing year (or
        a broken quote) makes the multiple meaningless.
        """
        if self.ttm_eps is None or self.ttm_eps <= 0 or self.price <= 0:
            return None
        return round(self.price / self.ttm_eps, 2)

    @property
    def price_to_fcf(self) -> float | None:
        """Price-to-free-cash-flow: price over ``fcf_per_share`` — the cash-flow
        analogue of ``trailing_pe``.

        ``None`` unless both legs are *positive*. A non-positive FCF (a company
        burning cash) makes the multiple meaningless in exactly the way a
        loss-making year does for P/E — "24× cash flow" has no reading when the
        cash flow is negative — so the same positivity guard applies. The signed
        version of that same read is ``fcf_yield``, which stays informative for a
        cash-burner.
        """
        if self.fcf_per_share is None or self.fcf_per_share <= 0 or self.price <= 0:
            return None
        return round(self.price / self.fcf_per_share, 2)

    @property
    def fcf_yield(self) -> float | None:
        """Free-cash-flow yield (percent): ``fcf_per_share`` over price — the
        reciprocal of ``price_to_fcf``, and the "how much cash am I buying per
        dollar of price" read that sits beside the dividend yield.

        Unlike ``price_to_fcf`` this keeps its *sign*: a negative yield (a company
        with negative free cash flow) is a real, informative reading, not a
        meaningless one — so the only guard is a live price. When FCF is negative
        the two figures deliberately diverge (a null P/FCF beside a negative
        yield), which is the standard treatment.
        """
        if self.fcf_per_share is None or self.price <= 0:
            return None
        return round(self.fcf_per_share / self.price * 100, 2)


@dataclass(frozen=True)
class OptionContract:
    """One listed option contract, as the market currently prices it.

    The vendor-agnostic row of an options chain: a right to buy (call) or sell
    (put) the stock at ``strike`` until ``expiration``. Prices/volume are optional
    because thin contracts routinely trade without a live quote; the metrics
    below simply skip what isn't there.
    """

    expiration: date
    strike: float
    is_call: bool  # False -> a put
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    volume: int | None = None  # contracts traded today
    open_interest: int | None = None  # contracts outstanding
    implied_volatility: float | None = None  # decimal fraction (0.28 = 28%)

    @property
    def mid(self) -> float | None:
        """The contract's fair price: the bid/ask midpoint when both sides are
        live, else the last trade. ``None`` when neither exists — a price of 0
        is a dead quote, not a price."""
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        if self.last_price is not None and self.last_price > 0:
            return self.last_price
        return None


@dataclass(frozen=True)
class TickerOptionsMetrics:
    """The four options-market reads the ticker card serves, at today's price.

    Each field is independently optional — a thin chain fills what it can:

    - ``implied_volatility``: at-the-money IV (percent) at the ~1-month expiry —
      the market's forward-looking "how nervous" gauge.
    - ``expected_move_percent``: the ATM straddle (call + put) as a percent of
      spot — the swing the market has priced in by ``expected_move_by``.
    - ``insurance_cost_percent``: an ATM protective put at the ~3-month expiry
      as a percent of spot — the market's literal price for downside protection
      until ``insurance_expires``.
    - ``put_call_ratio``: today's put volume over call volume across the sampled
      expiries — above 1 the day's bets lean protective, below 1 optimistic.
    """

    implied_volatility: float | None  # percent, ATM at the near expiry
    expected_move_percent: float | None  # ATM straddle / spot, percent
    expected_move_by: date | None  # the near (~1-month) expiry sampled
    insurance_cost_percent: float | None  # ATM put / spot, percent
    insurance_expires: date | None  # the far (~3-month) expiry sampled
    put_call_ratio: float | None  # today's put volume / call volume

    @classmethod
    def from_chains(
        cls,
        price: float,
        near: Sequence[OptionContract],
        insurance: Sequence[OptionContract],
    ) -> TickerOptionsMetrics:
        """Derive the four reads from two sampled chains (pure merge logic, like
        the timelines' ``filled_from``).

        ``near`` is the ~1-month expiry's contracts (IV + expected move),
        ``insurance`` the ~3-month expiry's (the protective put). Either may be
        empty, and they may be the *same* expiry when the listed dates are sparse —
        the volume pool dedupes on expiration so a shared chain isn't counted
        twice. A non-positive spot yields an all-``None`` read: every figure here
        is a ratio to it.
        """
        if price <= 0:
            return cls(None, None, None, None, None, None)
        calls = {c.strike: c for c in near if c.is_call}
        puts = {c.strike: c for c in near if not c.is_call}

        # ATM IV: average the call/put nearest the money (each side independently
        # — IV is quoted per contract and the two sides' ATM IVs sit together).
        ivs = [
            side[_nearest_strike(side, price)].implied_volatility
            for side in (calls, puts)
            if side
        ]
        ivs = [iv for iv in ivs if iv is not None and iv > 0]
        implied_volatility = (sum(ivs) / len(ivs)) * 100 if ivs else None

        # Expected move: the straddle needs a call AND a put at one strike — the
        # common strike nearest the money, both legs priced.
        expected_move = None
        common = calls.keys() & puts.keys()
        if common:
            strike = min(common, key=lambda s: abs(s - price))
            call_mid, put_mid = calls[strike].mid, puts[strike].mid
            if call_mid is not None and put_mid is not None:
                expected_move = (call_mid + put_mid) / price * 100

        # Insurance: the ATM put at the far expiry, as a percent of spot.
        insurance_cost = None
        far_puts = {c.strike: c for c in insurance if not c.is_call}
        if far_puts:
            put_mid = far_puts[_nearest_strike(far_puts, price)].mid
            if put_mid is not None:
                insurance_cost = put_mid / price * 100

        # Put/call ratio over everything sampled, deduped when both windows
        # landed on the same expiry.
        pool = list(near)
        if {c.expiration for c in insurance} - {c.expiration for c in near}:
            pool += list(insurance)
        call_volume = sum(c.volume or 0 for c in pool if c.is_call)
        put_volume = sum(c.volume or 0 for c in pool if not c.is_call)
        put_call_ratio = put_volume / call_volume if call_volume > 0 else None

        return cls(
            implied_volatility=implied_volatility,
            expected_move_percent=expected_move,
            expected_move_by=next(iter(near)).expiration if near else None,
            insurance_cost_percent=insurance_cost,
            insurance_expires=(
                next(iter(insurance)).expiration if insurance else None
            ),
            put_call_ratio=put_call_ratio,
        )


def _nearest_strike(side: dict[float, OptionContract], price: float) -> float:
    """The strike closest to the money on one side of a chain."""
    return min(side, key=lambda strike: abs(strike - price))


@dataclass(frozen=True)
class ReportedEps:
    """One quarter's reported (actual) EPS, keyed by its announcement date.

    The raw material of a trailing-P/E walk: a chronological run of these sums into a
    rolling trailing-twelve-month EPS. Announcement-dated (not fiscal-period-dated) on
    purpose — the market re-prices the multiple the day the number is released, so that's
    the date each P/E point is anchored to.
    """

    report_date: date
    eps: float  # the reported (actual) diluted/consensus EPS for that quarter


@dataclass(frozen=True)
class PeHistoryPoint:
    """The trailing P/E at one earnings release.

    The close on the announcement date over the trailing-twelve-month EPS the market
    knew then (the just-reported quarter plus the three before it). One dot on the P/E
    line the FE draws.
    """

    report_date: date  # the announcement date the P/E is anchored on
    price: float  # the close on/near the announcement date
    ttm_eps: float  # sum of the trailing 4 reported quarters
    pe: float  # price / ttm_eps, rounded to 2


class ValuationSignal(str, Enum):
    """Where the current trailing P/E sits within the stock's own history — the one-word read.

    ``CHEAP`` / ``EXPENSIVE`` when the current multiple is in the bottom / top quartile of its
    own history (it has rarely been cheaper / dearer), ``FAIR`` in the middle half. Deliberately
    a *relative* verdict — "cheap for this stock", not "cheap" outright: a structurally re-rated
    business (slowing growth, a faded moat) can read CHEAP the whole way down, so the signal
    anchors a judgement rather than making it. A ``str`` enum so the presenter serializes the
    value directly.

    ``NOT_MEANINGFUL`` is the escape hatch for a cyclical trough: when the *latest* release sits
    on a near-zero trailing EPS (see ``_TROUGH_EPS_FRACTION``), the P/E balloons on a collapsing
    denominator, so a percentile read would call a mid-cycle-cheap stock "expensive". Rather than
    emit that false verdict, the signal reports no read — the FE shows "trailing P/E not
    meaningful (trough earnings)" and the historical band is still there to eyeball.
    """

    CHEAP = "cheap"
    FAIR = "fair"
    EXPENSIVE = "expensive"
    NOT_MEANINGFUL = "not_meaningful"


@dataclass(frozen=True)
class PeHistoryStats:
    """Where the *current* trailing P/E sits within the stock's own history.

    The read that turns the raw P/E line into a valuation signal. The distribution of the
    historical multiples — ``median_pe`` with the ``p25_pe``/``p75_pe`` interquartile band and
    the ``min_pe``/``max_pe`` envelope — and where the latest reading falls in it:
    ``current_percentile`` (0–100, the share of history at or below it) bucketed into ``signal``.
    ``discount_to_median_percent`` is the current multiple's gap to its median (negative = below
    its typical multiple, i.e. cheaper than usual). ``sample_size`` is how many releases the
    distribution rests on — the confidence behind the verdict.

    ``current_pe`` is the *latest sampled point* — the P/E at the most recent earnings release,
    not a live tick (the card's ``metrics.pe`` is the to-the-second figure; this series is
    fundamentals-sampled, so "current" moves only when a new quarter reports). Same "relative to
    itself" caveat as ``ValuationSignal``: this says where the multiple is versus its own past,
    which anchors "is it a good buy" without settling it alone.

    When the latest release sits on a cyclical earnings trough, ``signal`` is
    ``NOT_MEANINGFUL`` and the distribution fields (``median_pe`` … ``max_pe``) are drawn from
    the rest of the history — the trough spike would otherwise blow out the envelope. ``current_pe``
    still carries the real (distorted) multiple, so the FE can show it beside the "not meaningful"
    note rather than hiding it.
    """

    current_pe: float
    median_pe: float
    p25_pe: float
    p75_pe: float
    min_pe: float
    max_pe: float
    current_percentile: float  # 0–100: share of history at or below the current multiple
    discount_to_median_percent: float  # (current - median) / median * 100; negative = cheaper
    signal: ValuationSignal
    sample_size: int  # number of historical points the distribution rests on


@dataclass(frozen=True)
class PeHistory:
    """A symbol's trailing P/E sampled at each earnings release — one point per reported
    quarter, oldest first.

    Pure derivation (``build``), the same stance as ``TickerOptionsMetrics.from_chains``:
    the use case fetches the two legs (the reported-EPS run and the daily closes) and the
    entity owns the rule that combines them. A quarter yields a point only when it has a
    full trailing year of EPS behind it (the first ``TTM_QUARTERS - 1`` are warm-up), a
    *positive* trailing sum (a trailing loss makes the multiple meaningless — the same
    guard as ``TickerValuation.trailing_pe``), and a close on/near its announcement date
    (early quarters outside the price feed's range are dropped). So the series can be
    shorter than the EPS run — a 200 with an empty ``points`` is a valid "no coverage".
    """

    # The fewest historical points a valuation signal may rest on. The series samples one
    # multiple per earnings release (~4 a year), so this is ~2 years — enough for a percentile
    # to mean "versus how it has traded" rather than versus two or three readings. Below it
    # ``stats`` is None (no verdict), the same "thin sample → no benchmark" stance the universe
    # slice's ``IndustryValuation`` takes with ``MIN_REPRESENTATIVE_PEERS``. The EPS adapter
    # fetches ~7 years, so a mature stock clears this comfortably; only fresh listings fall short.
    MIN_POINTS_FOR_STATS = 8

    symbol: str
    points: tuple[PeHistoryPoint, ...]

    @classmethod
    def build(
        cls,
        symbol: str,
        eps: Sequence[ReportedEps],
        closes: Mapping[date, float],
        *,
        max_price_lag_days: int = _MAX_PRICE_LAG_DAYS,
    ) -> "PeHistory":
        """Roll the reported-EPS run into a trailing-twelve-month series and divide each
        release's close by it. ``eps`` in any order (sorted here); ``closes`` maps a
        trading day to that day's close.

        The raw series is then passed through ``_without_cyclical_spikes`` — a cyclical
        trough (a near-zero trailing EPS ballooning the multiple) is dropped so it doesn't
        clutter the chart, the same reason ``stats`` suppresses the trough signal."""
        ordered = sorted(eps, key=lambda e: e.report_date)
        trading_days = sorted(closes)
        points: list[PeHistoryPoint] = []
        for i in range(TTM_QUARTERS - 1, len(ordered)):
            window = ordered[i - TTM_QUARTERS + 1 : i + 1]
            ttm = sum(q.eps for q in window)
            if ttm <= 0:
                continue  # a trailing loss has no meaningful P/E
            report_date = ordered[i].report_date
            price = _close_asof(trading_days, closes, report_date, max_price_lag_days)
            if price is None or price <= 0:
                continue  # no price near this release (feed range or a data gap)
            points.append(
                PeHistoryPoint(
                    report_date=report_date,
                    price=price,
                    ttm_eps=ttm,
                    pe=round(price / ttm, 2),
                )
            )
        return cls(symbol=symbol, points=_without_cyclical_spikes(tuple(points)))

    @property
    def stats(self) -> PeHistoryStats | None:
        """Summarize where the latest multiple sits in the series, or ``None`` for a thin
        sample (fewer than ``MIN_POINTS_FOR_STATS`` points) where a percentile would be noise
        rather than a signal.

        Pure over ``points`` — every P/E in them is already positive (``build`` drops trailing
        losses), so the distribution is well-formed and the median is a safe divisor. "Current"
        is the newest point (``points`` is oldest-first), ranked against the whole series.

        One special case: when the latest release sits on a cyclical earnings trough (its TTM
        EPS below ``_TROUGH_EPS_FRACTION`` of the history's median), the multiple is distorted
        by a collapsing denominator, so the signal is ``NOT_MEANINGFUL`` and the distribution is
        measured from the rest of the history rather than let the spike blow out the envelope."""
        if len(self.points) < self.MIN_POINTS_FOR_STATS:
            return None
        current = self.points[-1]
        trough_eps, _fence = _cyclical_thresholds(self.points[:-1])
        current_is_trough = current.ttm_eps < trough_eps
        # A trough denominator makes the current multiple meaningless: rank it and shape the band
        # from history alone (the spike would distort both), and hand back no cheap/fair/expensive.
        reference = self.points[:-1] if current_is_trough else self.points
        pes = sorted(point.pe for point in reference)
        median = _percentile(pes, 50)
        if median is None or median <= 0:
            return None  # defensive: real P/Es are positive, so a non-positive median never occurs
        percentile = _percentile_rank(pes, current.pe)
        return PeHistoryStats(
            current_pe=current.pe,
            median_pe=median,
            p25_pe=_percentile(pes, 25),
            p75_pe=_percentile(pes, 75),
            min_pe=pes[0],
            max_pe=pes[-1],
            current_percentile=percentile,
            discount_to_median_percent=round((current.pe - median) / median * 100, 1),
            signal=(
                ValuationSignal.NOT_MEANINGFUL
                if current_is_trough
                else _signal_for(percentile)
            ),
            sample_size=len(pes),
        )


def _close_asof(
    trading_days: Sequence[date],
    closes: Mapping[date, float],
    target: date,
    max_lag_days: int,
) -> float | None:
    """The close on ``target`` or the most recent trading day before it, within
    ``max_lag_days`` — a release can land on a weekend/holiday, and the prior session's
    close is the price the market carried into it. ``None`` when nothing is near enough
    (``trading_days`` must be sorted ascending)."""
    idx = bisect.bisect_right(trading_days, target)
    if idx == 0:
        return None
    day = trading_days[idx - 1]
    if (target - day).days > max_lag_days:
        return None
    return closes[day]


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
    """The ``q``-th percentile (0–100) of an already-sorted sequence, by linear interpolation
    between the two nearest ranks (the "type 7" definition numpy defaults to). ``None`` for an
    empty sample; rounded to 2 dp, the precision the P/E points carry.

    Deliberately the same definition as the universe slice's industry benchmark
    (``IndustryValuation._percentile``), so "P/E percentile" means one thing across the app —
    reimplemented here rather than imported to keep the entity layer stdlib-only (an entity
    never reaches into another slice)."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return round(sorted_values[0], 2)
    rank = (q / 100) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return round(sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo]), 2)


def _percentile_rank(sorted_values: Sequence[float], value: float) -> float:
    """The rank of ``value`` within ``sorted_values`` as a 0–100 percentile — the inverse of
    ``_percentile`` (value → rank, not rank → value).

    The mid-rank ("mean") convention: ties split half-below, half-above, so a value equal to
    the whole sample lands at 50 and the measure is symmetric between the minimum and the
    maximum. ``sorted_values`` must be non-empty; the P/Es are 2-dp rounded, so equality is
    exact. Rounded to 1 dp."""
    n = len(sorted_values)
    below = sum(1 for v in sorted_values if v < value)
    equal = sum(1 for v in sorted_values if v == value)
    return round(100 * (below + 0.5 * equal) / n, 1)


def _signal_for(percentile: float) -> ValuationSignal:
    """Bucket a 0–100 percentile into the valuation signal: bottom quartile → cheap, top
    quartile → expensive, the middle half → fair."""
    if percentile <= _CHEAP_PERCENTILE:
        return ValuationSignal.CHEAP
    if percentile >= _EXPENSIVE_PERCENTILE:
        return ValuationSignal.EXPENSIVE
    return ValuationSignal.FAIR


def _cyclical_thresholds(points: Sequence[PeHistoryPoint]) -> tuple[float, float]:
    """The two bounds that mark a trailing-P/E point as a cyclical distortion, derived from the
    given reference points (the caller passes the history *excluding* the current point, and
    guarantees it is non-empty): the *trough-earnings* EPS floor (``_TROUGH_EPS_FRACTION`` × the
    median TTM EPS) and the *far-outlier* P/E fence (``Q3 + _OUTLIER_IQR_MULT`` × IQR). Both rest
    on the median / quartiles, so a handful of spike points in the reference don't move them."""
    median_ttm = _percentile(sorted(p.ttm_eps for p in points), 50) or 0.0
    pes = sorted(p.pe for p in points)
    q1 = _percentile(pes, 25) or 0.0
    q3 = _percentile(pes, 75) or 0.0
    return _TROUGH_EPS_FRACTION * median_ttm, q3 + _OUTLIER_IQR_MULT * (q3 - q1)


def _without_cyclical_spikes(
    points: tuple[PeHistoryPoint, ...]
) -> tuple[PeHistoryPoint, ...]:
    """Drop the history's cyclical-trough / far-outlier P/E spikes (see ``_cyclical_thresholds``)
    so they don't clutter the chart or distort the valuation band. The most recent point is kept
    regardless — it's the current reading, distorted or not, and ``PeHistory.stats`` flags it
    (``NOT_MEANINGFUL``) rather than hiding it. A no-op below ``MIN_POINTS_FOR_STATS`` points,
    where the distribution is too thin to judge an outlier from — a fresh listing keeps its raw
    series, and ``stats`` returns ``None`` there anyway.

    Thresholds come from the history alone (the latest point excluded), so a current spike can't
    define the fence that would then wave it through."""
    if len(points) < PeHistory.MIN_POINTS_FOR_STATS:
        return points
    trough_eps, pe_fence = _cyclical_thresholds(points[:-1])
    last = len(points) - 1
    return tuple(
        point
        for i, point in enumerate(points)
        if i == last or (point.ttm_eps >= trough_eps and point.pe <= pe_fence)
    )
