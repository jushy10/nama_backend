"""Interface Adapter: quarterly earnings surprises from Finnhub.

Finnhub's free ``/stock/earnings`` endpoint returns the last reported quarters
with the consensus EPS estimate that preceded each one — the actual-vs-estimate
("beat or miss") history. Kept separate from the fundamentals adapter so each
module owns a single Finnhub endpoint; swap this and nothing else changes.

Docs: https://finnhub.io/docs/api/company-earnings
"""

from datetime import date

import httpx

from app.stocks.entities import EarningsHistory, EarningsSurprise
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import EarningsHistoryProvider


class FinnhubEarningsProvider(EarningsHistoryProvider):
    """Fetches recent quarterly earnings surprises from Finnhub (free key)."""

    _DEFAULT_BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)

    def get_earnings_history(self, symbol: str, *, limit: int) -> EarningsHistory:
        try:
            resp = self._http.get(
                "/stock/earnings",
                params={"symbol": symbol, "limit": limit, "token": self._api_key},
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code != 200:
            # Surface the upstream body so the failure is self-explaining.
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol,
                f"earnings request failed (HTTP {resp.status_code}): {body}",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(symbol, f"invalid JSON payload: {exc}") from exc

        # Finnhub returns a JSON array, newest quarter first; an unknown symbol
        # comes back as an empty array -> no earnings data for that ticker.
        rows = payload if isinstance(payload, list) else []
        quarters = tuple(_surprise(row) for row in rows if isinstance(row, dict))
        if not quarters:
            raise StockNotFound(symbol)
        return EarningsHistory(symbol=symbol, quarters=quarters)


def _surprise(row: dict) -> EarningsSurprise:
    return EarningsSurprise(
        period=_parse_date(row.get("period")),
        fiscal_year=row.get("year"),
        fiscal_quarter=row.get("quarter"),
        actual=row.get("actual"),
        estimate=row.get("estimate"),
        surprise=row.get("surprise"),
        surprise_percent=row.get("surprisePercent"),
    )


def _parse_date(value) -> date | None:
    """Parse Finnhub's ``YYYY-MM-DD`` period; ``None`` if absent or malformed."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None
