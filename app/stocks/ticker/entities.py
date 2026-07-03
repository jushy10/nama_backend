"""Entities: a stock's valuation read (trailing + forward) and its options-market read.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than
reaching into the shared ``app/stocks/entities.py``, the same convention as the
earnings and recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

``TickerValuation`` models the card's valuation multiples on one EPS basis — the
analyst-consensus (adjusted) one. The trailing P/E is today's price over the sum
of the 4 newest reported quarters' consensus-basis EPS (not a vendor's GAAP TTM
read, so it's directly comparable with the forward legs). The forward PEG is the
forward analogue of the trailing PEG: the forward P/E (today's price against next
fiscal year's consensus EPS) divided by the EPS growth analysts expect the year
after that (FY1 → FY2). Where the trailing PEG divides by growth *already
reported* — which a cyclical rebound can inflate into the hundreds of percent,
pinning the ratio near zero — this one divides by growth analysts still
*expect*, so it answers "is today's price justified by what's supposed to come"
rather than "by what already happened".

``OptionContract`` + ``TickerOptionsMetrics`` model what the options market says
about the stock: how nervous it is (at-the-money implied volatility), how big a
swing is priced in (the ATM straddle as a percent of spot), what downside
protection costs (an ATM put a quarter out), and which way the day's bets lean
(put/call volume). All are *reads on the underlying stock* for a buyer sizing up
an entry — not a chain browser for options traders — which is why the card serves
these four derived figures and not the contracts themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence


@dataclass(frozen=True)
class TickerValuation:
    """One symbol's valuation legs at today's price.

    The legs arrive precomputed (the use case derives them from the live quote,
    the stored consensus estimates, and the quarterly-earnings timeline); the
    entity owns the rules that combine them. The legs are optional: estimates
    are consensus coverage and the TTM sum needs four cached quarters, so a
    symbol missing either simply carries ``None``s around a live price.

    ``ttm_eps`` is deliberately on the *consensus (adjusted)* basis — the sum of
    the 4 newest reported quarters' "Reported EPS" — not GAAP diluted: the
    forward legs are quoted on the consensus basis, so anchoring the trailing
    multiple on the same basis is what makes ``trailing_pe`` and ``forward_pe``
    a comparable pair (a GAAP trailing leg would make the walk between them a
    basis artifact, not a story about growth).
    """

    symbol: str
    price: float  # the live price the multiple was taken at
    forward_pe: float | None  # price / FY1 consensus EPS
    forward_eps_growth: float | None  # FY1 -> FY2 consensus EPS growth (percent)
    ttm_eps: float | None = None  # trailing 12m EPS, consensus basis (4 reported quarters)

    @property
    def trailing_pe(self) -> float | None:
        """Trailing P/E on the consensus basis: price over ``ttm_eps``.

        The trailing counterpart of ``forward_pe``, on the same (adjusted) EPS
        basis so the two multiples read as one walk. Same guard as the other
        ratios here: ``None`` unless both legs are positive — a loss-making
        trailing year (or a broken quote) makes the multiple meaningless.
        """
        if self.ttm_eps is None or self.ttm_eps <= 0 or self.price <= 0:
            return None
        return round(self.price / self.ttm_eps, 2)

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
