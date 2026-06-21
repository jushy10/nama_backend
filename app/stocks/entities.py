"""Enterprise Business Rules: the Stock entity.

Pure domain object — imports nothing from the rest of the app, the web
framework, or Alpaca. It only knows the concept of a "stock" and the
calculations intrinsic to it.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Logo:
    """A company's logo image plus its MIME type, ready to serve as-is."""

    content: bytes
    media_type: str


@dataclass(frozen=True)
class Stock:
    """A snapshot of a tradable stock at a point in time."""

    symbol: str
    name: str | None
    exchange: str | None
    price: float  # latest trade price
    open: float | None
    high: float | None
    low: float | None
    previous_close: float | None
    volume: int | None
    bid: float | None
    ask: float | None
    as_of: datetime | None

    @property
    def change(self) -> float | None:
        """Absolute price change since the previous close."""
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        """Percent price change since the previous close."""
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)

    @property
    def spread(self) -> float | None:
        """Current bid/ask spread, if a quote is available."""
        if self.bid is None or self.ask is None:
            return None
        return round(self.ask - self.bid, 4)
