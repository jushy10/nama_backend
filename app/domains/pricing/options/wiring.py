"""The options slice's composition root — framework-free. The chain source is the
keyless yfinance provider, so it's an always-constructable process singleton and
there is no missing-key gate — a blocked Yahoo call surfaces as a 502 at read time
(the chain is primary here), not a boot-time failure."""

from functools import lru_cache

from app.adapters.yfinance.options_chain_adapter_impl import OptionsChainAdapterImpl
from app.domains.pricing.options.interfaces import OptionsChainAdapter
from app.domains.pricing.options.use_cases import GetOptionsFlow


@lru_cache(maxsize=1)
def get_options_chain_provider() -> OptionsChainAdapter:
    # Keyless (Yahoo via yfinance, like the ticker card's options provider), so no 503 gate.
    return OptionsChainAdapterImpl()


def build_get_options_flow() -> GetOptionsFlow:
    return GetOptionsFlow(get_options_chain_provider())
