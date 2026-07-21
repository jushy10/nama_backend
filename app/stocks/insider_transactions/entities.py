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
        if self.shares is None or self.price_per_share is None:
            return None
        return self.shares * self.price_per_share

    @property
    def is_open_market_buy(self) -> bool:
        return self.transaction_code == "P"

    @property
    def is_open_market_sale(self) -> bool:
        return self.transaction_code == "S"

    @property
    def is_open_market(self) -> bool:
        return self.transaction_code in _OPEN_MARKET_CODES

    @property
    def code_label(self) -> str:
        return _CODE_LABELS.get(self.transaction_code, self.transaction_code)

    @property
    def role(self) -> str:
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
    open_market_buy_count: int
    open_market_sell_count: int
    open_market_buy_value: float
    open_market_sell_value: float

    @property
    def net_value(self) -> float:
        return self.open_market_buy_value - self.open_market_sell_value


@dataclass(frozen=True)
class InsiderActivity:
    symbol: str
    transactions: tuple[InsiderTransaction, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.transactions

    @property
    def open_market(self) -> tuple[InsiderTransaction, ...]:
        return tuple(t for t in self.transactions if t.is_open_market)

    @property
    def summary(self) -> InsiderSummary:
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
