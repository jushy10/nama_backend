"""Interface Adapter: the next scheduled earnings report from Finnhub.

Finnhub's free ``/calendar/earnings`` endpoint lists scheduled earnings events
with the consensus EPS/revenue estimate going into each one. We read the *next*
upcoming event for a symbol — the "when do they report next, and where do
analysts expect it" forward view that complements the reported beat history
(which is past-only). Kept separate from the earnings-surprise adapter so each
module owns a single Finnhub endpoint; swap this and nothing else changes.

Docs: https://finnhub.io/docs/api/earnings-calendar
"""

from datetime import date, timedelta

import httpx

from app.stocks.entities import NextEarnings
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import EarningsCalendarProvider


class FinnhubEarningsCalendarProvider(EarningsCalendarProvider):
    """Fetches the next scheduled earnings report from Finnhub (free key)."""

    _DEFAULT_BASE_URL = "https://finnhub.io/api/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        horizon_days: int = 120,
    ) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)
        # How far ahead to look for the next report; a quarter-plus covers the
        # gap between cycles without dragging in the one after next.
        self._horizon_days = horizon_days

    def get_next_earnings(self, symbol: str) -> NextEarnings | None:
        today = date.today()
        try:
            resp = self._http.get(
                "/calendar/earnings",
                params={
                    "symbol": symbol,
                    "from": today.isoformat(),
                    "to": (today + timedelta(days=self._horizon_days)).isoformat(),
                    "token": self._api_key,
                },
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code != 200:
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol,
                f"earnings calendar request failed (HTTP {resp.status_code}): {body}",
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(symbol, f"invalid JSON payload: {exc}") from exc

        # Finnhub returns {"earningsCalendar": [...]} — an unknown symbol or a
        # name with nothing scheduled in the window yields an empty list.
        rows = payload.get("earningsCalendar") if isinstance(payload, dict) else None
        dated = [
            (d, row)
            for row in (rows or [])
            if isinstance(row, dict) and (d := _parse_date(row.get("date"))) is not None
        ]
        if not dated:
            return None
        # The soonest scheduled event is the "next report".
        _, row = min(dated, key=lambda pair: pair[0])
        return NextEarnings(
            report_date=_parse_date(row.get("date")),
            fiscal_year=row.get("year"),
            fiscal_quarter=row.get("quarter"),
            eps_estimate=row.get("epsEstimate"),
            revenue_estimate=row.get("revenueEstimate"),
            session=_session(row.get("hour")),
        )


def _parse_date(value) -> date | None:
    """Parse Finnhub's ``YYYY-MM-DD`` date; ``None`` if absent or malformed."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _session(value) -> str | None:
    """Normalize Finnhub's ``hour`` field to a known session code or None."""
    if isinstance(value, str) and value in {"bmo", "amc", "dmh"}:
        return value
    return None
