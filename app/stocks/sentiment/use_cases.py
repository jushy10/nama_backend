"""Application Business Rules: the market-sentiment use case.

One whole-market read with no input: the VIX and the CNN Fear & Greed score,
gathered together for the home page. Each leg is **best-effort** — the two come
from separate keyless sources, so one being down (e.g. CNN blocking us) degrades
that leg to ``None`` rather than failing the whole read. Only when *both* legs
are unavailable is the read a real outage (``StockDataUnavailable``). Depends
solely on its two ports; the adapters behind them are keyless and
live-per-request (no table, no cron), like the yields slice.
"""

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
    """Use case: the combined VIX + Fear & Greed home-page read.

    Takes no input. Reads both sources independently and assembles a
    ``MarketSentiment``; a source that raises a domain error is dropped to
    ``None`` so its sibling still surfaces. If neither source is available the
    read raises, translating to a 502 at the edge.
    """

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
        """Run a source read, swallowing a domain failure into ``None``.

        Both legs are enrichment for a combined read — a failure is logged and
        dropped so the other leg still surfaces, never propagated.
        """
        try:
            return fetch()
        except (StockNotFound, StockDataUnavailable) as exc:
            logger.info("market-sentiment %s source unavailable: %s", label, exc)
            return None
