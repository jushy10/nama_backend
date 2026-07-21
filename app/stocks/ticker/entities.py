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
    symbol: str
    price: float  # the live price the multiples were taken at
    ttm_eps: float | None = None  # trailing 12m EPS, consensus basis (4 reported quarters)
    fcf_per_share: float | None = None  # trailing free cash flow per share (annual slice, anchor)
    ocf_per_share: float | None = None  # trailing operating cash flow per share (annual slice, anchor)
    book_value_per_share: float | None = None  # P/B input (fundamentals slice, anchor)
    sales_per_share: float | None = None  # P/S input (fundamentals slice, anchor)
    eps_growth_yoy: float | None = None  # trailing EPS growth %, consensus basis (for peg)
    ebitda: float | None = None  # trailing EBITDA, absolute (fundamentals slice, anchor)
    total_debt: float | None = None  # total debt, absolute (fundamentals slice, anchor)
    cash_and_equivalents: float | None = None  # cash + equivalents, absolute (anchor)
    shares_outstanding: float | None = None  # share count, for live enterprise value (anchor)

    @property
    def trailing_pe(self) -> float | None:
        if self.ttm_eps is None or self.ttm_eps <= 0 or self.price <= 0:
            return None
        return round(self.price / self.ttm_eps, 2)

    @property
    def price_to_fcf(self) -> float | None:
        if self.fcf_per_share is None or self.fcf_per_share <= 0 or self.price <= 0:
            return None
        return round(self.price / self.fcf_per_share, 2)

    @property
    def fcf_yield(self) -> float | None:
        if self.fcf_per_share is None or self.price <= 0:
            return None
        return round(self.fcf_per_share / self.price * 100, 2)

    @property
    def ocf_yield(self) -> float | None:
        if self.ocf_per_share is None or self.price <= 0:
            return None
        return round(self.ocf_per_share / self.price * 100, 2)

    @property
    def pb(self) -> float | None:
        if self.book_value_per_share is None or self.book_value_per_share <= 0 or self.price <= 0:
            return None
        return round(self.price / self.book_value_per_share, 2)

    @property
    def ps(self) -> float | None:
        if self.sales_per_share is None or self.sales_per_share <= 0 or self.price <= 0:
            return None
        return round(self.price / self.sales_per_share, 2)

    @property
    def peg(self) -> float | None:
        pe = self.trailing_pe
        if pe is None or self.eps_growth_yoy is None or self.eps_growth_yoy <= 0:
            return None
        return round(pe / self.eps_growth_yoy, 2)

    @property
    def enterprise_value(self) -> float | None:
        if self.shares_outstanding is None or self.shares_outstanding <= 0 or self.price <= 0:
            return None
        market_cap = self.price * self.shares_outstanding
        return market_cap + (self.total_debt or 0.0) - (self.cash_and_equivalents or 0.0)

    @property
    def ev_to_ebitda(self) -> float | None:
        ev = self.enterprise_value
        if ev is None or self.ebitda is None or self.ebitda <= 0:
            return None
        return round(ev / self.ebitda, 2)


@dataclass(frozen=True)
class OptionContract:
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
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        if self.last_price is not None and self.last_price > 0:
            return self.last_price
        return None


@dataclass(frozen=True)
class TickerOptionsMetrics:
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
    return min(side, key=lambda strike: abs(strike - price))


@dataclass(frozen=True)
class ReportedEps:
    report_date: date
    eps: float  # the reported (actual) diluted/consensus EPS for that quarter


@dataclass(frozen=True)
class PeHistoryPoint:
    report_date: date  # the announcement date the P/E is anchored on
    price: float  # the close on/near the announcement date
    ttm_eps: float  # sum of the trailing 4 reported quarters
    pe: float  # price / ttm_eps, rounded to 2


class ValuationSignal(str, Enum):
    CHEAP = "cheap"
    FAIR = "fair"
    EXPENSIVE = "expensive"
    NOT_MEANINGFUL = "not_meaningful"


@dataclass(frozen=True)
class PeHistoryStats:
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
    idx = bisect.bisect_right(trading_days, target)
    if idx == 0:
        return None
    day = trading_days[idx - 1]
    if (target - day).days > max_lag_days:
        return None
    return closes[day]


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
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
    n = len(sorted_values)
    below = sum(1 for v in sorted_values if v < value)
    equal = sum(1 for v in sorted_values if v == value)
    return round(100 * (below + 0.5 * equal) / n, 1)


def _signal_for(percentile: float) -> ValuationSignal:
    if percentile <= _CHEAP_PERCENTILE:
        return ValuationSignal.CHEAP
    if percentile >= _EXPENSIVE_PERCENTILE:
        return ValuationSignal.EXPENSIVE
    return ValuationSignal.FAIR


def _cyclical_thresholds(points: Sequence[PeHistoryPoint]) -> tuple[float, float]:
    median_ttm = _percentile(sorted(p.ttm_eps for p in points), 50) or 0.0
    pes = sorted(p.pe for p in points)
    q1 = _percentile(pes, 25) or 0.0
    q3 = _percentile(pes, 75) or 0.0
    return _TROUGH_EPS_FRACTION * median_ttm, q3 + _OUTLIER_IQR_MULT * (q3 - q1)


def _without_cyclical_spikes(
    points: tuple[PeHistoryPoint, ...]
) -> tuple[PeHistoryPoint, ...]:
    if len(points) < PeHistory.MIN_POINTS_FOR_STATS:
        return points
    trough_eps, pe_fence = _cyclical_thresholds(points[:-1])
    last = len(points) - 1
    return tuple(
        point
        for i, point in enumerate(points)
        if i == last or (point.ttm_eps >= trough_eps and point.pe <= pe_fence)
    )
