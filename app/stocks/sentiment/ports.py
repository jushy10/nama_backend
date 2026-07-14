"""Application ports: the abstractions the market-sentiment use case depends on.

Two capabilities, deliberately split because they come from two independent
keyless sources: the VIX (FRED) and the CNN Fear & Greed score (CNN's dataviz
endpoint). Each is an ``ABC`` returning a slice entity and raising the shared
domain exceptions; the use case gathers both and degrades either to ``None``, so
one source failing never sinks the other.
"""

from abc import ABC, abstractmethod

from app.stocks.sentiment.entities import FearGreedSnapshot, VixSnapshot


class VixProvider(ABC):
    """A gateway for the current CBOE Volatility Index (VIX) close."""

    @abstractmethod
    def get_vix(self) -> VixSnapshot:
        """Return the latest VIX close and the immediately preceding close.

        Raises:
            StockDataUnavailable: the upstream source failed or returned nothing.
        """
        raise NotImplementedError


class FearGreedProvider(ABC):
    """A gateway for the current CNN Fear & Greed Index score."""

    @abstractmethod
    def get_fear_greed(self) -> FearGreedSnapshot:
        """Return the current Fear & Greed score with its trailing comparisons.

        Raises:
            StockDataUnavailable: the upstream source failed or returned nothing.
        """
        raise NotImplementedError
