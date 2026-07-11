"""HTTP response DTOs for the insider-transactions endpoint.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. The open-market buy/sell
flags and the derived ``value`` / ``code_label`` / ``role`` (all computed on the entity) are
surfaced so a client doesn't have to re-derive the "big buy / big sell" signal.
"""

from datetime import date

from pydantic import BaseModel


class InsiderTransactionResponse(BaseModel):
    """One insider transaction (a Form 4 line).

    ``transaction_code`` is the raw Form 4 code and ``code_label`` its human rendering;
    ``is_open_market_buy`` / ``is_open_market_sale`` flag the ``P`` / ``S`` conviction trades
    (the "big buy / big sell"), with ``is_open_market`` their union. ``value`` is the trade's
    dollar size (``shares * price_per_share``), ``null`` when either leg is missing (e.g. a
    footnote-only price)."""

    filing_date: date
    transaction_date: date | None
    insider_name: str
    role: str
    security_title: str | None
    transaction_code: str
    code_label: str
    acquired_disposed: str | None
    is_open_market: bool
    is_open_market_buy: bool
    is_open_market_sale: bool
    shares: float | None
    price_per_share: float | None
    value: float | None
    shares_owned_following: float | None


class InsiderSummaryResponse(BaseModel):
    """A net buy-vs-sell rollup of the open-market (``P``/``S``) trades — counts and summed
    dollar value of purchases vs. sales, and the net (``buy - sell``, positive = net buying).
    Always reflects the full open-market set, independent of the ``open_market_only`` filter."""

    open_market_buy_count: int
    open_market_sell_count: int
    open_market_buy_value: float
    open_market_sell_value: float
    net_value: float


class InsiderActivityResponse(BaseModel):
    """A stock's recent insider transactions, newest first, plus the net buy-vs-sell ``summary``.

    ``count`` is the number of transactions in ``transactions`` (which honours the
    ``open_market_only`` query filter); an empty list means no recent insider activity on file.
    ``summary`` always reflects the full open-market rollup regardless of the filter."""

    symbol: str
    count: int
    summary: InsiderSummaryResponse
    transactions: list[InsiderTransactionResponse]
