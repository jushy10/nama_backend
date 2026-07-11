"""Entities: a stock's insider buys and sells — SEC Form 4 non-derivative transactions.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching into
the shared ``app/stocks/entities.py``, the same convention as the earnings / recommendations /
news / revenue-segments sub-slices). Pure and vendor-agnostic — stdlib only.

A **Form 4** is the filing an insider — an officer, a director, or a 10%+ owner — must file with
the SEC within two business days of trading their own company's stock. Each carries one or more
transactions, each stamped with a one-letter *transaction code* that says what it was: ``P`` an
open-market purchase, ``S`` an open-market sale — the conviction "big buy / big sell" — versus
the compensation and mechanics a Form 4 also reports (``A`` a grant, ``M`` an option exercise,
``F`` shares withheld for tax, ``G`` a gift, …).

Two facts about the domain shape everything here:

- **The signal is the open-market P/S trades.** Every non-derivative transaction is stored (the
  primitive fact), but ``is_open_market`` flags the two codes that mean an insider *chose* to
  buy or sell on the market with their own money; the rest is compensation plumbing. The
  ``summary`` rolls the P/S trades into a net buy-vs-sell read — the "are insiders net buying or
  selling" signal.
- **A filed transaction is a frozen fact.** Once reported it never changes, so the store
  accumulates history and the cache upsert is insert-only (like the rating-changes / news
  slices), keyed on the filing's accession number + the transaction's line within it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# One-letter Form 4 transaction codes -> human labels. ``P`` / ``S`` are the open-market
# conviction trades; the rest is the compensation / mechanics a Form 4 also reports. An
# unrecognised code falls back to the raw letter (see ``code_label``).
_CODE_LABELS = {
    "P": "Open-market purchase",
    "S": "Open-market sale",
    "A": "Grant or award",
    "M": "Option exercise",
    "F": "Tax withholding",
    "G": "Gift",
    "D": "Sale to issuer",
    "X": "Option exercise",
    "C": "Derivative conversion",
    "W": "Acquired/disposed by will",
    "J": "Other acquisition/disposition",
}

# The two codes that mark an open-market conviction trade (a real "big buy / big sell"), as
# opposed to the compensation / mechanics codes.
_OPEN_MARKET_CODES = frozenset({"P", "S"})


@dataclass(frozen=True)
class InsiderTransaction:
    """One insider's one reported transaction in the company's stock (a Form 4 line).

    ``transaction_code`` is the raw Form 4 code and ``acquired_disposed`` is ``"A"`` (acquired)
    or ``"D"`` (disposed). The open-market buy/sell signal is the pair ``is_open_market_buy``
    (``P``) / ``is_open_market_sale`` (``S``); the broader A/D axis captures every
    acquisition/disposition including the compensation ones. ``shares`` and ``price_per_share``
    can each be ``None`` — a Form 4 sometimes reports a price only in a footnote (an option
    exercise, say), so ``value`` is best-effort. ``accession_number`` + ``line_index`` are the
    filing's identity plus the transaction's ordinal within it — the store's unique key.
    """

    filing_date: date
    transaction_date: date | None
    insider_name: str
    officer_title: str | None
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    security_title: str | None
    transaction_code: str
    acquired_disposed: str | None  # "A" (acquired) / "D" (disposed)
    shares: float | None
    price_per_share: float | None
    shares_owned_following: float | None
    accession_number: str
    line_index: int

    @property
    def value(self) -> float | None:
        """The transaction's dollar value (``shares * price_per_share``), or ``None`` when either
        leg is missing — computed on access, not stored."""
        if self.shares is None or self.price_per_share is None:
            return None
        return self.shares * self.price_per_share

    @property
    def is_open_market_buy(self) -> bool:
        """An open-market purchase (code ``P``) — the insider bought on the market: the "big buy"."""
        return self.transaction_code == "P"

    @property
    def is_open_market_sale(self) -> bool:
        """An open-market sale (code ``S``) — the insider sold on the market: the "big sell"."""
        return self.transaction_code == "S"

    @property
    def is_open_market(self) -> bool:
        """Either open-market conviction trade (``P`` or ``S``), as opposed to the
        compensation / mechanics codes (grants, option exercises, tax withholding, gifts)."""
        return self.transaction_code in _OPEN_MARKET_CODES

    @property
    def code_label(self) -> str:
        """A human label for the raw ``transaction_code`` ("Open-market purchase"), falling back
        to the raw code for one we don't recognise."""
        return _CODE_LABELS.get(self.transaction_code, self.transaction_code)

    @property
    def role(self) -> str:
        """A compact label for the insider's relationship to the company — the officer title
        when present (the most specific), else "Officer" / "Director" / "10% Owner", else
        "Insider"."""
        if self.officer_title:
            return self.officer_title
        if self.is_officer:
            return "Officer"
        if self.is_director:
            return "Director"
        if self.is_ten_percent_owner:
            return "10% Owner"
        return "Insider"


@dataclass(frozen=True)
class InsiderSummary:
    """A net buy-vs-sell rollup of the open-market (``P``/``S``) trades in an ``InsiderActivity``.

    Counts and summed dollar value of the open-market purchases vs. sales, and the net
    (``buy_value - sell_value``) — the "are insiders net buying or selling" read. Only the P/S
    conviction trades count; grants / exercises / tax / gifts are excluded. The value legs sum
    only the transactions whose value is known (both shares *and* price present)."""

    open_market_buy_count: int
    open_market_sell_count: int
    open_market_buy_value: float
    open_market_sell_value: float

    @property
    def net_value(self) -> float:
        """Net open-market dollar flow: buy value minus sell value (positive = net buying)."""
        return self.open_market_buy_value - self.open_market_sell_value


@dataclass(frozen=True)
class InsiderActivity:
    """A stock's recent insider transactions — every stored Form 4 non-derivative trade, newest
    first.

    Best-effort: a stock its insiders haven't traded recently (or one SEC doesn't cover as a
    domestic filer) yields an empty (``is_empty``) activity, not an error — the same contract the
    other best-effort slices use. ``open_market`` narrows to the P/S conviction trades;
    ``summary`` rolls those into a net buy-vs-sell read.
    """

    symbol: str
    transactions: tuple[InsiderTransaction, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no transaction is carried (no recent insider activity on file)."""
        return not self.transactions

    @property
    def open_market(self) -> tuple[InsiderTransaction, ...]:
        """Only the open-market buys and sells (``P``/``S``) — the conviction trades, in the same
        order as ``transactions``."""
        return tuple(t for t in self.transactions if t.is_open_market)

    @property
    def summary(self) -> InsiderSummary:
        """A net buy-vs-sell rollup of the open-market trades (see ``InsiderSummary``). Computed
        over the stored (already-recent, bounded) set on access, not stored."""
        buy_count = sell_count = 0
        buy_value = sell_value = 0.0
        for txn in self.transactions:
            if txn.is_open_market_buy:
                buy_count += 1
                if txn.value is not None:
                    buy_value += txn.value
            elif txn.is_open_market_sale:
                sell_count += 1
                if txn.value is not None:
                    sell_value += txn.value
        return InsiderSummary(
            open_market_buy_count=buy_count,
            open_market_sell_count=sell_count,
            open_market_buy_value=buy_value,
            open_market_sell_value=sell_value,
        )
