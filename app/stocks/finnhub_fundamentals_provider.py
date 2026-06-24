"""Interface Adapter: company fundamentals from Finnhub.

Market data feeds (Alpaca) don't expose market cap or dividends, so those come
from a fundamentals vendor. Finnhub's free ``/stock/metric`` endpoint returns a
broad metrics object keyed by ticker; we pick out market cap and dividend. This
is the only module that knows Finnhub exists; swap it and nothing else changes.

Docs: https://finnhub.io/docs/api/company-basic-financials
"""

import httpx

from app.stocks.entities import StockFundamentals
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import StockFundamentalsProvider


class FinnhubFundamentalsProvider(StockFundamentalsProvider):
    """Fetches market cap + dividend from Finnhub (free API key required)."""

    _DEFAULT_BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)

    def get_fundamentals(self, symbol: str) -> StockFundamentals:
        try:
            resp = self._http.get(
                "/stock/metric",
                params={"symbol": symbol, "metric": "all", "token": self._api_key},
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code != 200:
            # Surface the upstream body so the failure is self-explaining.
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol,
                f"fundamentals request failed (HTTP {resp.status_code}): {body}",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(symbol, f"invalid JSON payload: {exc}") from exc

        # Unknown/uncovered symbols return 200 with an empty "metric" object,
        # which maps cleanly to all-None fundamentals (best-effort).
        metric = (payload.get("metric") if isinstance(payload, dict) else None) or {}
        return StockFundamentals(
            market_cap=self._market_cap(metric),
            dividend_per_share=_first(
                metric, "dividendPerShareAnnual", "dividendPerShareTTM"
            ),
            dividend_yield=_first(
                metric, "dividendYieldIndicatedAnnual", "currentDividendYieldTTM"
            ),
        )

    @staticmethod
    def _market_cap(metric: dict) -> float | None:
        # Finnhub reports market cap in millions of USD; normalize to raw USD.
        millions = metric.get("marketCapitalization")
        return millions * 1_000_000 if millions is not None else None


def _first(metric: dict, *keys: str) -> float | None:
    """First present, non-null value among candidate metric keys."""
    for key in keys:
        value = metric.get(key)
        if value is not None:
            return value
    return None
