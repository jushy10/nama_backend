from dataclasses import dataclass
from datetime import datetime

from app.domains.shared.entities import StockPerformance


@dataclass(frozen=True)
class SectorPerformance:
    sector: str
    symbol: str  # the proxy ETF ticker
    price: float  # latest trade price of the proxy ETF
    previous_close: float | None
    as_of: datetime | None
    # Trailing-window returns (1w/1m/3m/6m/ytd/1y) of the proxy ETF; best-effort
    # like the Stock entity's, so None when price history is unavailable.
    performance: StockPerformance | None = None

    @property
    def change(self) -> float | None:
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)


@dataclass(frozen=True)
class MarketIndexPerformance:
    name: str  # the index's plain name, e.g. "S&P 500"
    symbol: str  # the proxy ETF ticker, e.g. "SPY"
    price: float  # latest trade price of the proxy ETF
    previous_close: float | None
    as_of: datetime | None
    performance: StockPerformance | None = None

    @property
    def change(self) -> float | None:
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)
