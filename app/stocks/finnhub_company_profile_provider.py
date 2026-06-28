"""Interface Adapter: a company's clean display name from Finnhub.

Finnhub's free ``/stock/profile2`` endpoint returns a tidy company name
("Apple Inc.") — the display name the stock view prefers over the price feed's
full legal title ("Apple Inc. Common Stock"). It carries no business
description, so this fills only the *name* half of ``CompanyProfile``; the
description comes from a different vendor and the two are merged behind the port
by ``CompositeCompanyProfileProvider``. This is the only module that knows
Finnhub profiles exist.

Docs: https://finnhub.io/docs/api/company-profile2
"""

import httpx

from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import CompanyProfileProvider


class FinnhubCompanyProfileProvider(CompanyProfileProvider):
    """Fetches a company's clean display name from Finnhub (free key)."""

    _DEFAULT_BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)

    def get_profile(self, symbol: str) -> CompanyProfile:
        try:
            resp = self._http.get(
                "/stock/profile2", params={"symbol": symbol, "token": self._api_key}
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code != 200:
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol, f"profile request failed (HTTP {resp.status_code}): {body}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(symbol, f"invalid JSON payload: {exc}") from exc
        # Finnhub returns a JSON object; an unknown symbol comes back as ``{}``,
        # which maps cleanly to "no name" (best-effort enrichment).
        data = payload if isinstance(payload, dict) else {}
        return CompanyProfile(name=_clean(data.get("name")), description=None)


def _clean(value: object) -> str | None:
    """Normalize a Finnhub profile string to a non-empty, trimmed value or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
