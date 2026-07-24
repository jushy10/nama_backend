from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.domains.pricing.options.entities import (
    OptionContract,
    OptionsFlow,
    OptionsFlowSummary,
)

# The unusual-activity list is a "look here first" highlight, not the whole chain, so it's
# capped — the full picture is already in `calls`/`puts`. Most-money-first (the entity's
# ordering), so the cap keeps the biggest bets.
_MAX_UNUSUAL = 25


def _round2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


class OptionContractResponse(BaseModel):
    expiration: date
    strike: float
    type: str  # "call" | "put"
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    mid: float | None = None
    volume: int | None = None  # contracts traded today
    open_interest: int | None = None  # contracts outstanding (prior-day)
    implied_volatility: float | None = None  # percent
    in_the_money: bool | None = None
    premium: float | None = None  # dollars traded today (mid × volume × 100)
    volume_oi_ratio: float | None = None  # volume / open interest
    unusual: bool = False  # volume > open interest

    @classmethod
    def from_contract(cls, c: OptionContract) -> "OptionContractResponse":
        # Display figures rounded here at the edge — the chain arithmetic (mid, premium, the
        # ratio) carries float noise — and IV rendered as a percent (the entity keeps the
        # vendor's decimal fraction).
        iv = None if c.implied_volatility is None else round(c.implied_volatility * 100, 2)
        return cls(
            expiration=c.expiration,
            strike=c.strike,
            type=c.option_type.value,
            bid=c.bid,
            ask=c.ask,
            last_price=c.last_price,
            mid=_round2(c.mid),
            volume=c.volume,
            open_interest=c.open_interest,
            implied_volatility=iv,
            in_the_money=c.in_the_money,
            premium=_round2(c.premium),
            volume_oi_ratio=_round2(c.volume_oi_ratio),
            unusual=c.is_unusual,
        )


class OptionsFlowSummaryResponse(BaseModel):
    call_volume: int
    put_volume: int
    total_volume: int
    call_open_interest: int
    put_open_interest: int
    put_call_volume_ratio: float | None = None
    put_call_oi_ratio: float | None = None
    call_premium: float  # dollars into calls
    put_premium: float  # dollars into puts
    net_premium: float  # call_premium - put_premium (signed)

    @classmethod
    def from_summary(cls, s: OptionsFlowSummary) -> "OptionsFlowSummaryResponse":
        return cls(
            call_volume=s.call_volume,
            put_volume=s.put_volume,
            total_volume=s.total_volume,
            call_open_interest=s.call_open_interest,
            put_open_interest=s.put_open_interest,
            put_call_volume_ratio=_round2(s.put_call_volume_ratio),
            put_call_oi_ratio=_round2(s.put_call_oi_ratio),
            call_premium=_round2(s.call_premium),
            put_premium=_round2(s.put_premium),
            net_premium=_round2(s.net_premium),
        )


class OptionsFlowResponse(BaseModel):
    ticker: str
    spot: float | None = None
    expiration: date | None = None  # null only when the symbol lists no options
    expirations: list[date] = []
    summary: OptionsFlowSummaryResponse | None = None  # null only when no options listed
    calls: list[OptionContractResponse] = []
    puts: list[OptionContractResponse] = []
    unusual: list[OptionContractResponse] = []  # capped, most money first

    @classmethod
    def from_flow(cls, flow: OptionsFlow) -> "OptionsFlowResponse":
        chain = flow.chain
        if chain is None:
            return cls(
                ticker=flow.symbol,
                expirations=list(flow.expirations),
            )
        return cls(
            ticker=flow.symbol,
            spot=_round2(chain.spot),
            expiration=chain.expiration,
            expirations=list(flow.expirations),
            summary=OptionsFlowSummaryResponse.from_summary(chain.summary),
            calls=[OptionContractResponse.from_contract(c) for c in chain.calls],
            puts=[OptionContractResponse.from_contract(c) for c in chain.puts],
            unusual=[
                OptionContractResponse.from_contract(c) for c in chain.unusual[:_MAX_UNUSUAL]
            ],
        )
