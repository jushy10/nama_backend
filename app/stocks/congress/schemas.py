"""HTTP response DTOs for the Congressional-trades endpoints.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the serialization
shape lives here so the domain stays framework-agnostic. The buy/sell flags and the derived
``amount_midpoint`` (all computed on the entity) are surfaced so a client doesn't re-derive them.

The item shape follows the slice's API contract exactly (``member`` / ``chamber`` / ``party`` /
``ticker`` / ``name`` / ``tx_type`` / ``amount_range`` / ``transaction_date`` / ``disclosure_date``
/ ``is_buy`` / ``is_sell``), plus a few additive best-effort fields (``owner`` / ``amount_midpoint``
/ ``source_url``). Both the per-ticker read and the market board reuse the same item DTO.
"""

from datetime import date

from pydantic import BaseModel


class CongressTradeResponse(BaseModel):
    """One member's one disclosed trade.

    ``name`` is the company name (from the shared anchor); ``tx_type`` is the normalized action
    (``Purchase`` / ``Sale`` / ``Exchange`` / ``Other``) with ``is_buy`` / ``is_sell`` its derived
    buy/sell flags. ``amount_range`` is the disclosed dollar band verbatim (Congress never reports
    an exact figure) and ``amount_midpoint`` a best-effort estimate of the trade's size (the middle
    of the band). ``transaction_date`` is when the trade happened; ``disclosure_date`` when it was
    reported (the two can be weeks apart)."""

    member: str
    chamber: str
    party: str | None
    ticker: str
    name: str | None
    tx_type: str
    amount_range: str | None
    amount_midpoint: float | None
    transaction_date: date | None
    disclosure_date: date | None
    owner: str | None
    source_url: str | None
    is_buy: bool
    is_sell: bool


class CongressSummaryResponse(BaseModel):
    """A net buy-vs-sell rollup of a set of trades — counts and *estimated* dollar flow (summed
    band midpoints, since Congress discloses only ranges), and the net (``buy - sell``, positive =
    net buying)."""

    buy_count: int
    sell_count: int
    buy_value: float
    sell_value: float
    net_value: float


class CongressActivityResponse(BaseModel):
    """A single stock's recent Congressional trades, newest first, plus the net buy-vs-sell
    ``summary``.

    ``total`` is the full number of stored trades for the stock; ``count`` the number returned in
    ``items`` this page; ``limit`` / ``offset`` echo the window the page was cut with. An empty
    ``items`` means no Congressional activity on file. ``summary`` always reflects the *full* stored
    set regardless of the page.
    """

    symbol: str
    total: int
    limit: int
    offset: int
    count: int
    summary: CongressSummaryResponse
    items: list[CongressTradeResponse]


class CongressMarketActivityResponse(BaseModel):
    """A window of the whole market's recent Congressional trades, newest first, with the
    pagination envelope.

    ``window`` echoes the requested window token (``"30d"``); ``total`` is the full match count in
    the window before the page was cut; ``count`` the number in ``items`` this page; ``limit`` /
    ``offset`` the window it was cut with. ``summary`` rolls up the page's trades.
    """

    window: str
    total: int
    limit: int
    offset: int
    count: int
    summary: CongressSummaryResponse
    items: list[CongressTradeResponse]
