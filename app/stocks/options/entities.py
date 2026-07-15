"""Entities: a stock's live options chain and the *flow* read derived from it.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than
reaching into the shared ``app/stocks/entities.py`` or the ticker slice's — the same
convention the earnings / news / recommendations sub-slices follow). Pure and
vendor-agnostic — stdlib only.

Where the ticker card's ``options_metrics`` distils the chain into four summary reads
for a buyer sizing an entry, this slice keeps the **contracts themselves** and the
aggregates an options-flow screen shows: per-side volume and open interest, the
put/call lean, the dollar premium flowing into each side, and the "unusual activity"
tell — contracts trading *more* today than were previously outstanding (volume above
open interest), which is fresh positioning rather than churn of an existing book.

The whole read is a **snapshot**, not a trade-by-trade tape: Yahoo publishes each
contract's *cumulative* day volume and prior-day open interest, not individual prints
with a bid/ask side. So this answers "where is the volume and the money going today"
(the keyless read), not "who swept the offer at 10:32" (an OPRA time-and-sales feed).
Every derived figure below is a fact about the contracts, computed on access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Sequence

# One options-contract lot is 100 shares, so the dollar premium that changed hands in a
# contract is its price × volume × this multiplier.
CONTRACT_MULTIPLIER = 100


class OptionType(str, Enum):
    """Which side of the chain a contract sits on — a right to buy (call) or sell (put).

    A ``str`` enum so the presenter serializes the value (``"call"`` / ``"put"``)
    directly."""

    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class OptionContract:
    """One listed option contract, as the market currently prices it.

    The vendor-agnostic row of an options chain. Prices / volume / open interest are
    all optional because thin contracts routinely trade (or sit) without a live quote;
    the derived figures below simply skip what isn't there. ``implied_volatility`` is a
    decimal fraction (``0.28`` = 28%), the vendor's native unit — the presenter renders
    it as a percent.
    """

    expiration: date
    strike: float
    option_type: OptionType
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    volume: int | None = None  # contracts traded today (cumulative)
    open_interest: int | None = None  # contracts outstanding (prior-day settle)
    implied_volatility: float | None = None  # decimal fraction (0.28 = 28%)
    in_the_money: bool | None = None  # vendor's ITM flag; None when unreported

    @property
    def is_call(self) -> bool:
        return self.option_type is OptionType.CALL

    @property
    def mid(self) -> float | None:
        """The contract's fair price: the bid/ask midpoint when both sides are live,
        else the last trade. ``None`` when neither exists — a price of 0 is a dead
        quote, not a price."""
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        if self.last_price is not None and self.last_price > 0:
            return self.last_price
        return None

    @property
    def premium(self) -> float | None:
        """The dollar premium that changed hands today: ``mid`` × volume × 100 (a lot is
        100 shares). This is the "how much money went into this strike" figure a flow
        screen ranks by — a $4M call print stands out where raw volume alone wouldn't.
        ``None`` when the contract has no price or no volume to weight it by."""
        price = self.mid
        if price is None or self.volume is None or self.volume <= 0:
            return None
        return price * self.volume * CONTRACT_MULTIPLIER

    @property
    def volume_oi_ratio(self) -> float | None:
        """Today's volume over the existing open interest — how much of the day's trading
        is *new* relative to the outstanding book. Above 1 means more contracts traded
        today than were open, the hallmark of fresh positioning. ``None`` when either leg
        is missing or open interest is 0 (the ratio is undefined, though ``is_unusual``
        still flags a 0-OI contract that traded — see below)."""
        if self.volume is None or self.open_interest is None or self.open_interest == 0:
            return None
        return self.volume / self.open_interest

    @property
    def is_unusual(self) -> bool:
        """The standard keyless "unusual activity" tell: today's volume exceeds the prior
        open interest, i.e. more contracts traded today than were outstanding — a fresh
        position going on rather than an existing one being worked. Requires real volume
        and a known open interest; a contract that traded against 0 open interest counts
        (brand-new interest). Deliberately a boolean tell, not a certainty — it flags
        *where to look*, since a snapshot can't prove intent the way a trade tape can."""
        if self.volume is None or self.volume <= 0 or self.open_interest is None:
            return False
        return self.volume > self.open_interest


@dataclass(frozen=True)
class OptionsFlowSummary:
    """The day's aggregate flow across a set of contracts — the top line of the screen.

    Per-side volume and open interest, the dollar premium into each side, and the
    put/call lean derived from them. Built by ``from_contracts`` over one expiry's chain
    (or, in a later revision, the whole board). Missing per-contract figures count as 0
    in the sums — an unreported volume is "no trades seen", not a gap that voids the
    total.
    """

    call_volume: int
    put_volume: int
    call_open_interest: int
    put_open_interest: int
    call_premium: float  # dollars into calls today
    put_premium: float  # dollars into puts today

    @classmethod
    def from_contracts(
        cls, contracts: Sequence[OptionContract]
    ) -> "OptionsFlowSummary":
        """Roll a chain into the per-side aggregates (pure, like the timelines'
        ``filled_from``). Volume / open interest / premium each sum what's present and
        treat the rest as 0."""
        call_vol = put_vol = call_oi = put_oi = 0
        call_prem = put_prem = 0.0
        for c in contracts:
            if c.is_call:
                call_vol += c.volume or 0
                call_oi += c.open_interest or 0
                call_prem += c.premium or 0.0
            else:
                put_vol += c.volume or 0
                put_oi += c.open_interest or 0
                put_prem += c.premium or 0.0
        return cls(
            call_volume=call_vol,
            put_volume=put_vol,
            call_open_interest=call_oi,
            put_open_interest=put_oi,
            call_premium=call_prem,
            put_premium=put_prem,
        )

    @property
    def total_volume(self) -> int:
        return self.call_volume + self.put_volume

    @property
    def put_call_volume_ratio(self) -> float | None:
        """Put volume over call volume — the day's directional lean. Above 1 the day's
        bets skew protective/bearish, below 1 optimistic. ``None`` when no calls traded
        (an undefined ratio)."""
        if self.call_volume <= 0:
            return None
        return self.put_volume / self.call_volume

    @property
    def put_call_oi_ratio(self) -> float | None:
        """Put open interest over call open interest — the standing positioning lean, the
        slower-moving cousin of the volume ratio. ``None`` when no call open interest."""
        if self.call_open_interest <= 0:
            return None
        return self.put_open_interest / self.call_open_interest

    @property
    def net_premium(self) -> float:
        """Call premium minus put premium — the day's dollar lean. Positive means more
        money went into calls (bullish tilt), negative into puts. A signed dollar figure,
        so it reads directly as "net $X into calls/puts today"."""
        return self.call_premium - self.put_premium


@dataclass(frozen=True)
class ExpiryChain:
    """One expiration's full chain plus the flow read over it.

    The contracts as they arrive from the vendor, the underlying's ``spot`` (best-effort
    context for the at-the-money row — ``None`` when the feed omits it), and the derived
    views a flow screen renders: the two sides sorted into a strike ladder, the aggregate
    ``summary``, and the ``unusual`` contracts ranked by the dollars behind them.
    """

    expiration: date
    spot: float | None
    contracts: tuple[OptionContract, ...]

    @property
    def calls(self) -> tuple[OptionContract, ...]:
        """The call side as a strike ladder (ascending)."""
        return tuple(sorted((c for c in self.contracts if c.is_call), key=lambda c: c.strike))

    @property
    def puts(self) -> tuple[OptionContract, ...]:
        """The put side as a strike ladder (ascending)."""
        return tuple(sorted((c for c in self.contracts if not c.is_call), key=lambda c: c.strike))

    @property
    def summary(self) -> OptionsFlowSummary:
        return OptionsFlowSummary.from_contracts(self.contracts)

    @property
    def unusual(self) -> tuple[OptionContract, ...]:
        """The unusual-activity contracts (``is_unusual``), most money first.

        Ranked by ``premium`` (dollars traded) so the biggest bets lead — the reading a
        flow screen leads with — with a contract that has volume but no priceable premium
        sorting to the back (treated as 0) rather than dropping out. Ties break on the
        volume/OI ratio (how *fresh* the interest is) then strike, so the order is stable
        for a given snapshot."""
        return tuple(
            sorted(
                (c for c in self.contracts if c.is_unusual),
                key=lambda c: (c.premium or 0.0, c.volume_oi_ratio or 0.0, c.strike),
                reverse=True,
            )
        )
