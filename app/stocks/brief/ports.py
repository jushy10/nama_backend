"""Abstract live-source port for the brief slice.

The one interface the generate use case depends on to turn a gathered market snapshot into
a written brief — Dependency Inversion for the AI. The use case is handed a
``MarketBriefProvider`` and never knows whether it's Claude on Bedrock, another model, or a
hand-written fake (tests); it just calls ``generate``. The concrete Bedrock implementation
lives in ``app/stocks/adapters/bedrock/market_brief_adapter.py``.
"""

from abc import ABC, abstractmethod
from datetime import date

from app.stocks.brief.entities import MarketBrief, MarketBriefContext


class MarketBriefProvider(ABC):
    """Writes a plain-language ``MarketBrief`` from a gathered market snapshot."""

    @abstractmethod
    def generate(self, context: MarketBriefContext, brief_date: date) -> MarketBrief:
        """Return a brief dated ``brief_date``, written from ``context``'s true quotes.

        The model contributes only prose (the tone, summary and section text); every number
        it's shown comes from the context, so a brief never carries a figure the model
        authored. Stamps the entity's ``generated_at`` and ``model``.

        Raises:
            StockDataUnavailable: the model call failed or returned no usable result.
        """
        raise NotImplementedError
