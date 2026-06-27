"""Interface Adapter: the earnings calendar from Finnhub.

Finnhub's free ``/calendar/earnings`` endpoint lists scheduled earnings events
with the consensus EPS/revenue estimate going into each one — and, for events
already reported, the actuals. We read two slices: the *next* upcoming event
(the "when do they report next, and where do analysts expect it" forward view)
and recent revenue (estimate vs actual per quarter, to merge onto the EPS beat
history, which is revenue-blind). Kept separate from the earnings-surprise
adapter so each module owns a single Finnhub endpoint.

Docs: https://finnhub.io/docs/api/earnings-calendar
"""

from datetime import date, timedelta

import httpx

from app.stocks.entities import NextEarnings
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import EarningsCalendarProvider


class FinnhubEarningsCalendarProvider(EarningsCalendarProvider):
    """Fetches the earnings calendar from Finnhub (free key)."""

    _DEFAULT_BASE_URL = "https://finnhub.io/api/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        horizon_days: int = 120,
        lookback_days: int = 540,
    ) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)
        # How far ahead to look for the next report; a quarter-plus covers the
        # gap between cycles without dragging in the one after next.
        self._horizon_days = horizon_days
        # How far back to pull reported revenue — enough to cover the last
        # several quarters the beat history returns.
        self._lookback_days = lookback_days

    def _fetch_events(self, symbol: str, frm: date, to: date) -> list[dict]:
        """Fetch the calendar rows for a date window, or raise on failure."""
        try:
            resp = self._http.get(
                "/calendar/earnings",
                params={
                    "symbol": symbol,
                    "from": frm.isoformat(),
                    "to": to.isoformat(),
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
        # window with nothing scheduled yields an empty list.
        rows = payload.get("earningsCalendar") if isinstance(payload, dict) else None
        return [row for row in (rows or []) if isinstance(row, dict)]

    def get_next_earnings(self, symbol: str) -> NextEarnings | None:
        today = date.today()
        rows = self._fetch_events(
            symbol, today, today + timedelta(days=self._horizon_days)
        )
        dated = [
            (d, row)
            for row in rows
            if (d := _parse_date(row.get("date"))) is not None
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

    def get_recent_revenue(
        self, symbol: str
    ) -> dict[tuple[int, int], tuple[float | None, float | None]]:
        today = date.today()
        rows = self._fetch_events(
            symbol, today - timedelta(days=self._lookback_days), today
        )
        out: dict[tuple[int, int], tuple[float | None, float | None]] = {}
        for row in rows:
            year, quarter = row.get("year"), row.get("quarter")
            if year is None or quarter is None:
                continue
            estimate, actual = row.get("revenueEstimate"), row.get("revenueActual")
            if estimate is None and actual is None:
                continue  # nothing to carry for this quarter
            out[(year, quarter)] = (estimate, actual)
        return out


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
