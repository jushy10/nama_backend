"""Enterprise Business Rules: the market slice's own entities.

The market-wide boards: each sector's move on the day (proxied by its SPDR
Select Sector ETF) and the headline US indices' performance (proxied by
SPY/QQQ). Pure domain objects — they import only the shared kernel's
``StockPerformance`` and nothing from the outer layers.
"""

from dataclasses import dataclass
from datetime import datetime

from app.stocks.entities import StockPerformance


@dataclass(frozen=True)
class SectorPerformance:
    """One market sector's move on the day, proxied by its sector ETF.

    Sector indices aren't directly tradable, so each sector is represented by
    the SPDR Select Sector ETF that tracks it (e.g. XLK -> Technology). The
    day's move is the proxy's latest price versus its previous close — the same
    rule the Stock entity uses for its own daily change.
    """

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


@dataclass(frozen=True)
class MarketIndexPerformance:
    """One headline US index's move on the day, proxied by a tradable ETF.

    Broad-market indices (the S&P 500, the Nasdaq) aren't directly tradable, so
    each is read through the exchange-traded fund that tracks it — SPY for the
    S&P 500, QQQ for the Nasdaq. The day's move is the proxy's latest price versus
    its previous close (the same rule the ``Stock`` and ``SectorPerformance``
    entities use); ``performance`` carries the trailing-window returns
    (1w/1m/…/1y), best-effort like the others (``None`` when price history is
    unavailable).
    """

    name: str  # the index's plain name, e.g. "S&P 500"
    symbol: str  # the proxy ETF ticker, e.g. "SPY"
    price: float  # latest trade price of the proxy ETF
    previous_close: float | None
    as_of: datetime | None
    performance: StockPerformance | None = None

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
