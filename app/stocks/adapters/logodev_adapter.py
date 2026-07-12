"""Interface Adapter: a fresh, ticker-keyed logo source.

Alpaca's logo endpoint is paywalled, so logos come from a separate vendor.
Logo.dev serves company logos by ticker at a public CDN URL and resolves to the
*current* logo through mergers, rebrands, and symbol changes — so the image
stays up to date instead of going stale (the reason we moved off the previous
source). This is the only module that knows that source exists; swap it and
nothing else changes.

Needs a free *publishable* token (https://logo.dev — 500k requests/month). The
token is publishable by design — it rides in the request URL, not a header — so
it isn't a secret the way the Alpaca keys are, but it's still injected from the
environment rather than hard-coded.

Two request params pin the behaviour:
  * ``format=png`` — the endpoint defaults to ``jpg``; we want PNG bytes.
  * ``fallback=404`` — by default an unknown ticker yields a monogram placeholder
    at HTTP 200; forcing 404 lets us raise StockNotFound, keeping the endpoint
    contract identical to the old source (missing logo -> HTTP 404).
"""

import httpx

from app.stocks.logo.entities import Logo
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.logo.ports import LogoProvider


class LogoDevProvider(LogoProvider):
    """Fetches company logos from Logo.dev (free tier, publishable token)."""

    _DEFAULT_BASE_URL = "https://img.logo.dev"

    def __init__(self, token: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._token = token
        self._http = httpx.Client(
            base_url=base_url,
            timeout=10.0,
            follow_redirects=True,  # the source 3xx-es to a CDN-hosted image
        )

    def get_logo(self, symbol: str) -> Logo:
        try:
            resp = self._http.get(
                f"/ticker/{symbol}",
                params={"token": self._token, "format": "png", "fallback": "404"},
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code == 404:
            raise StockNotFound(symbol)
        if resp.status_code != 200:
            # Surface the upstream body so the failure is self-explaining. The
            # token rides in the request URL, not the response body, so it does
            # not leak here.
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol, f"logo request failed (HTTP {resp.status_code}): {body}"
            )
        media_type = resp.headers.get("content-type", "image/png")
        return Logo(content=resp.content, media_type=media_type)
