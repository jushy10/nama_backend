"""Interface Adapter: analyst recommendation trends from Finnhub.

Finnhub's free ``/stock/recommendation`` endpoint returns the sell-side
buy/hold/sell split for a symbol, one row per month, newest first. We map those
rows into our ``RecommendationTrend`` entities (wrapped in an
``AnalystRecommendations``). This is the only module that knows Finnhub serves
this; swap it and nothing else changes.

Docs: https://finnhub.io/docs/api/recommendation-trends
"""

from datetime import date

import httpx

from app.stocks.entities import AnalystRecommendations, RecommendationTrend
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import RecommendationProvider


class FinnhubRecommendationProvider(RecommendationProvider):
    """Fetches analyst recommendation trends from Finnhub (free key required)."""

    _DEFAULT_BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        try:
            resp = self._http.get(
                "/stock/recommendation",
                params={"symbol": symbol, "token": self._api_key},
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code != 200:
            # Surface the upstream body so the failure is self-explaining.
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol,
                f"recommendation request failed (HTTP {resp.status_code}): {body}",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(symbol, f"invalid JSON payload: {exc}") from exc

        # Finnhub returns a bare list, newest first; an uncovered symbol yields an
        # empty list, which maps cleanly to "no analyst coverage" (empty trends).
        rows = payload if isinstance(payload, list) else []
        trends = [
            trend
            for row in rows
            if isinstance(row, dict)
            if (trend := _trend(row)) is not None
        ]
        # Defensive: keep newest-first regardless of how the upstream ordered it.
        trends.sort(key=lambda t: t.period, reverse=True)
        return AnalystRecommendations(symbol=symbol, trends=tuple(trends))


def _trend(row: dict) -> RecommendationTrend | None:
    """Map one Finnhub row to a ``RecommendationTrend``; ``None`` if it carries
    no period (the key we order and align on)."""
    period = _parse_date(row.get("period"))
    if period is None:
        return None
    return RecommendationTrend(
        period=period,
        strong_buy=_count(row.get("strongBuy")),
        buy=_count(row.get("buy")),
        hold=_count(row.get("hold")),
        sell=_count(row.get("sell")),
        strong_sell=_count(row.get("strongSell")),
    )


def _parse_date(value) -> date | None:
    """Parse Finnhub's ``YYYY-MM-DD`` period; ``None`` if absent or malformed."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _count(value) -> int:
    """Coerce an analyst count to int, treating missing/malformed as 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
