"""HTTP response DTOs for the options-flow endpoint.

Pydantic models kept at the edge, deliberately separate from ``entities`` — the JSON
shape lives here so the domain stays framework-agnostic. Two shape choices are made here
rather than on the entity: implied volatility is rendered as a **percent** (the entity
keeps the vendor's decimal fraction), and the domain's ``symbol`` is renamed ``ticker``,
this endpoint's JSON vocabulary.
"""

from datetime import date

from pydantic import BaseModel


class OptionContractResponse(BaseModel):
    """One contract's row on the chain.

    ``type`` is ``"call"`` / ``"put"``. ``mid`` is the fair price (bid/ask midpoint, else
    last), and ``premium`` the dollars that changed hands today (``mid`` × volume × 100) —
    the figure a flow screen ranks by. ``implied_volatility`` is a **percent** (the entity's
    decimal fraction × 100). ``volume_oi_ratio`` is today's volume over prior open interest,
    and ``unusual`` flags a contract whose volume exceeded that open interest (fresh
    positioning). Any field is ``null`` when the vendor didn't quote it — an unpriced or
    untraded contract, not a zero."""

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


class OptionsFlowSummaryResponse(BaseModel):
    """The day's aggregate flow across the shown expiry.

    Per-side volume / open interest, the dollar premium into each side, the put/call lean
    (volume and open-interest ratios), and ``net_premium`` — call premium minus put
    premium, a signed dollar figure (positive = money leaning into calls). The two ratios
    are ``null`` when their call denominator is 0."""

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


class OptionsFlowResponse(BaseModel):
    """A stock's options-flow read for one expiration.

    ``spot`` is the underlying's price for at-the-money context (``null`` when the feed
    omits it). ``expiration`` is the expiry these ``calls`` / ``puts`` are for, and
    ``expirations`` the full list of listed expiries so the client can switch without a
    second call. ``summary`` is the aggregate flow, and ``unusual`` the standout contracts
    (volume above open interest), most-money-first. A symbol with no listed options is a
    200 with ``expiration``/``summary`` ``null`` and empty lists — not a 404."""

    ticker: str
    spot: float | None = None
    expiration: date | None = None  # null only when the symbol lists no options
    expirations: list[date] = []
    summary: OptionsFlowSummaryResponse | None = None  # null only when no options listed
    calls: list[OptionContractResponse] = []
    puts: list[OptionContractResponse] = []
    unusual: list[OptionContractResponse] = []  # capped, most money first
