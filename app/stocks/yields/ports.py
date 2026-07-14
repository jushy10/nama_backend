"""Application ports: the abstractions the Treasury-yields use cases depend on.

Two capabilities, deliberately split because they come from two keyless
sources: the whole par-yield curve in one shot (US Treasury) and the 2Y/10Y
history (FRED). Each is an ``ABC`` returning slice entities and raising the
shared domain exceptions.
"""

from abc import ABC, abstractmethod

from app.stocks.yields.entities import YieldCurve, YieldHistory


class YieldCurveProvider(ABC):
    """A gateway for the current US Treasury par-yield curve."""

    @abstractmethod
    def get_yield_curve(self) -> YieldCurve:
        """Return the most recent daily par-yield curve across all maturities.

        Raises:
            StockDataUnavailable: the upstream source failed or returned nothing.
        """
        raise NotImplementedError


class YieldHistoryProvider(ABC):
    """A gateway for the 2Y and 10Y Treasury yields over time."""

    @abstractmethod
    def get_yield_history(self, lookback_days: int) -> YieldHistory:
        """Return the 2Y and 10Y yield series over the trailing ``lookback_days``.

        Raises:
            StockDataUnavailable: the upstream source failed or returned nothing.
        """
        raise NotImplementedError
