"""HTTP response DTOs for the institutional-ownership endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the serialization
shape lives here so the domain stays framework-agnostic. The derived buy/sell flags and the
``share_change`` / ``value_change`` / ``flow`` (all computed on the entities) are surfaced so a
client doesn't have to re-derive the "big buy / big sell" signal.
"""

from datetime import date

from pydantic import BaseModel


class InstitutionalHolderResponse(BaseModel):
    """One institutional/mutual-fund holder's stake as of a reported 13F quarter.

    ``holder_type`` is ``institution`` / ``mutual_fund``; ``pct_held`` / ``pct_change`` are percent.
    ``is_buyer`` / ``is_seller`` flag the direction of the quarter-over-quarter position change (the
    "big buy / big sell"), and ``share_change`` / ``value_change`` are its size (positive = added),
    ``null`` when the inputs are missing."""

    holder: str
    holder_type: str
    date_reported: date
    shares: float | None
    value: float | None
    pct_held: float | None
    pct_change: float | None
    is_buyer: bool
    is_seller: bool
    share_change: float | None
    value_change: float | None


class OwnershipBreakdownResponse(BaseModel):
    """The headline ownership summary — what fraction (percent) of the company institutions and
    insiders hold, and how many institutions hold it."""

    institutions_pct_held: float | None
    insiders_pct_held: float | None
    institutions_float_pct_held: float | None
    institutions_count: int | None


class HolderFlowResponse(BaseModel):
    """A net buy-vs-sell rollup of the latest reported snapshot — counts of holders that added vs.
    trimmed, the summed shares/value bought vs. sold (magnitudes), and the nets (positive = net
    buying)."""

    buyers_count: int
    sellers_count: int
    shares_bought: float
    shares_sold: float
    value_bought: float
    value_sold: float
    net_share_change: float
    net_value_change: float


class InstitutionalOwnershipResponse(BaseModel):
    """A stock's institutional ownership: the accumulated holders feed, the current breakdown, and
    the latest-snapshot buy-vs-sell ``flow``.

    ``count`` is the number of holder rows in ``holders`` (newest reported quarter first);
    ``latest_report_date`` is the most recent reported quarter. An empty ``holders`` means the
    source carries no institutional coverage for the symbol (a 200, not a 404)."""

    symbol: str
    count: int
    latest_report_date: date | None = None
    breakdown: OwnershipBreakdownResponse | None = None
    flow: HolderFlowResponse
    holders: list[InstitutionalHolderResponse]
