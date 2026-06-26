"""Interface Adapter: a company's business description from FMP.

Market-data feeds (Alpaca) return a ticker's name and exchange but not a summary
of what the company does. Financial Modeling Prep's profile endpoint does, so the
description shown on the stock view comes from here. We read FMP's "stable"
endpoint first and fall back to the older ``/api/v3`` one (some keys are scoped to
the legacy API) — the same dual-endpoint handling the constituents sync uses.
This is the only module that knows FMP profiles exist; swap it and nothing else
changes.

Docs: https://site.financialmodelingprep.com/developer/docs (Company Profile)
"""

import httpx

from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import CompanyProfileProvider


class FmpProfileProvider(CompanyProfileProvider):
    """Fetches a company's business description from FMP (free API key required)."""

    _DEFAULT_BASE_URL = "https://financialmodelingprep.com"

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)

    def get_profile(self, symbol: str) -> CompanyProfile:
        payload = self._fetch_profile(symbol)
        # FMP returns a list of profiles; an unknown symbol yields an empty list,
        # which maps cleanly to "no description" (best-effort enrichment).
        first = payload[0] if isinstance(payload, list) and payload else {}
        description = first.get("description") if isinstance(first, dict) else None
        return CompanyProfile(description=_clean(description))

    def _fetch_profile(self, symbol: str):
        """Fetch the raw profile list, preferring the stable endpoint and falling
        back to legacy ``/api/v3`` (some keys are scoped to one API). Raises only
        when every endpoint fails the request, so the body is self-explaining."""
        routes = (
            ("/stable/profile", {"symbol": symbol}),
            (f"/api/v3/profile/{symbol}", {}),
        )
        last_error: object = "no attempt made"
        for path, params in routes:
            try:
                resp = self._http.get(path, params={**params, "apikey": self._api_key})
            except httpx.HTTPError as exc:
                last_error = str(exc)
                continue
            if resp.status_code != 200:
                body = resp.text[:200].strip() or "<empty body>"
                last_error = f"HTTP {resp.status_code}: {body}"
                continue
            try:
                return resp.json()
            except ValueError as exc:
                last_error = f"invalid JSON: {exc}"
        raise StockDataUnavailable(symbol, f"profile request failed ({last_error})")


def _clean(value: object) -> str | None:
    """Normalize FMP's description to a non-empty, trimmed string or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
