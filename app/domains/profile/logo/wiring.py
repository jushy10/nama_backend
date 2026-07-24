"""The logo slice's composition root — framework-free. The endpoint's Depends shim
resolves the env-gated token (its 503 gate stays at the HTTP edge) and passes it in."""

from functools import lru_cache

from app.adapters.logodev.logo_adapter_impl import LogoAdapterImpl
from app.domains.profile.logo.interfaces import LogoAdapter
from app.domains.profile.logo.use_cases import GetStockLogo


@lru_cache(maxsize=4)
def get_logo_provider(token: str, base_url: str | None = None) -> LogoAdapter:
    # Logo.dev keeps logos current through rebrands/symbol changes. One process
    # singleton per config — the impl holds an httpx connection pool worth sharing.
    # base_url lets tests point elsewhere without a code change.
    return LogoAdapterImpl(token, base_url) if base_url else LogoAdapterImpl(token)


def build_get_stock_logo(provider: LogoAdapter) -> GetStockLogo:
    return GetStockLogo(provider)
