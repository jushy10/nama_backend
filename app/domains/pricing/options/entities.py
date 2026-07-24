from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Sequence

# One options-contract lot is 100 shares, so the dollar premium that changed hands in a
# contract is its price × volume × this multiplier.
CONTRACT_MULTIPLIER = 100


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class OptionContract:
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
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        if self.last_price is not None and self.last_price > 0:
            return self.last_price
        return None

    @property
    def premium(self) -> float | None:
        price = self.mid
        if price is None or self.volume is None or self.volume <= 0:
            return None
        return price * self.volume * CONTRACT_MULTIPLIER

    @property
    def volume_oi_ratio(self) -> float | None:
        if self.volume is None or self.open_interest is None or self.open_interest == 0:
            return None
        return self.volume / self.open_interest

    @property
    def is_unusual(self) -> bool:
        if self.volume is None or self.volume <= 0 or self.open_interest is None:
            return False
        return self.volume > self.open_interest


@dataclass(frozen=True)
class OptionsFlowSummary:
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
        if self.call_volume <= 0:
            return None
        return self.put_volume / self.call_volume

    @property
    def put_call_oi_ratio(self) -> float | None:
        if self.call_open_interest <= 0:
            return None
        return self.put_open_interest / self.call_open_interest

    @property
    def net_premium(self) -> float:
        return self.call_premium - self.put_premium


@dataclass(frozen=True)
class ExpiryChain:
    expiration: date
    spot: float | None
    contracts: tuple[OptionContract, ...]

    @property
    def calls(self) -> tuple[OptionContract, ...]:
        return tuple(sorted((c for c in self.contracts if c.is_call), key=lambda c: c.strike))

    @property
    def puts(self) -> tuple[OptionContract, ...]:
        return tuple(sorted((c for c in self.contracts if not c.is_call), key=lambda c: c.strike))

    @property
    def summary(self) -> OptionsFlowSummary:
        return OptionsFlowSummary.from_contracts(self.contracts)

    @property
    def unusual(self) -> tuple[OptionContract, ...]:
        return tuple(
            sorted(
                (c for c in self.contracts if c.is_unusual),
                key=lambda c: (c.premium or 0.0, c.volume_oi_ratio or 0.0, c.strike),
                reverse=True,
            )
        )


@dataclass(frozen=True)
class OptionsFlow:
    symbol: str
    expirations: tuple[date, ...]
    chain: ExpiryChain | None  # None only when the symbol lists no options
