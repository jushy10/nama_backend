import logging
from typing import Callable, TypeVar

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.sentiment.entities import MarketSentiment
from app.stocks.sentiment.ports import FearGreedProvider, VixProvider

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# No single symbol backs a whole-market read; sentinel for the failure message.
_SENTIMENT = "*"


class GetMarketSentiment:
    def __init__(
        self,
        vix_provider: VixProvider,
        fear_greed_provider: FearGreedProvider,
    ) -> None:
        self._vix_provider = vix_provider
        self._fear_greed_provider = fear_greed_provider

    def execute(self) -> MarketSentiment:
        vix = self._best_effort("VIX", self._vix_provider.get_vix)
        fear_greed = self._best_effort(
            "Fear & Greed", self._fear_greed_provider.get_fear_greed
        )
        if vix is None and fear_greed is None:
            raise StockDataUnavailable(
                _SENTIMENT, "no market-sentiment sources were available"
            )
        return MarketSentiment(vix=vix, fear_greed=fear_greed)

    @staticmethod
    def _best_effort(label: str, fetch: Callable[[], _T]) -> _T | None:
        try:
            return fetch()
        except (StockNotFound, StockDataUnavailable) as exc:
            logger.info("market-sentiment %s source unavailable: %s", label, exc)
            return None
