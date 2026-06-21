"""Interface Adapter: a free, ticker-keyed logo source.

Alpaca's logo endpoint is gated behind a paid plan, so logos come from a
separate vendor. Financial Modeling Prep serves company logos as PNGs at a
public, no-auth URL keyed by ticker (e.g. .../image-stock/AAPL.png). This is
the only module that knows that source exists; swap it and nothing else changes.
"""

import httpx

from app.stocks.entities import Logo
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import LogoProvider


class FmpLogoProvider(LogoProvider):
    """Fetches company logos from Financial Modeling Prep (free, no API key)."""

    _DEFAULT_BASE_URL = "https://financialmodelingprep.com/image-stock"

    def __init__(self, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            timeout=10.0,
            follow_redirects=True,  # the source may 3xx to a CDN-hosted image
        )

    def get_logo(self, symbol: str) -> Logo:
        try:
            resp = self._http.get(f"/{symbol}.png")
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code == 404:
            raise StockNotFound(symbol)
        if resp.status_code != 200:
            # Surface the upstream body so the failure is self-explaining.
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol, f"logo request failed (HTTP {resp.status_code}): {body}"
            )
        media_type = resp.headers.get("content-type", "image/png")
        return Logo(content=resp.content, media_type=media_type)
