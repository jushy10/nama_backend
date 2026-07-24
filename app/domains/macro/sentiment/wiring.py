"""The sentiment slice's composition root — framework-free. Both sources are keyless
(FRED + CNN), so the providers are always-constructable process singletons and there
is no missing-key gate."""

from functools import lru_cache

from app.adapters.cnn.fear_greed_adapter_impl import FearGreedAdapterImpl
from app.adapters.fred.vix_adapter_impl import VixAdapterImpl
from app.domains.macro.sentiment.interfaces import FearGreedAdapter, VixAdapter
from app.domains.macro.sentiment.use_cases import GetMarketSentiment


@lru_cache(maxsize=1)
def get_vix_provider() -> VixAdapter:
    # Keyless (FRED), so no 503 gate — unlike the Alpaca price feed.
    return VixAdapterImpl()


@lru_cache(maxsize=1)
def get_fear_greed_provider() -> FearGreedAdapter:
    # Keyless (CNN), so no 503 gate.
    return FearGreedAdapterImpl()


def build_get_market_sentiment() -> GetMarketSentiment:
    return GetMarketSentiment(get_vix_provider(), get_fear_greed_provider())
