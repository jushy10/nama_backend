"""Interface Adapter: upcoming estimates + reported revenue from FMP.

FMP's free ``/stable/earnings`` endpoint lists a symbol's earnings events — past
and future — each with EPS and revenue, both the consensus going in
(``epsEstimated``/``revenueEstimated``) and, once reported, the actual
(``epsActual``/``revenueActual``). One free call covers the two gaps Finnhub's
free tier leaves: the next several *upcoming* quarters' consensus, and
reported-quarter revenue (estimate vs actual) to merge onto the EPS beat
history. (FMP's analyst-estimates endpoint carries the same numbers but is
paywalled on the free plan; the legacy ``/api/v3`` endpoints are retired.)

Docs: https://site.financialmodelingprep.com/developer/docs/stable/earnings-company
"""

from datetime import date

import httpx

from app.stocks.entities import EarningsEstimates, NextEarnings
from app.stocks.exceptions import StockDataUnavailable

from app.stocks.ports import EarningsEstimatesProvider

# Cap the forward view so the chart stays legible — the nearest few quarters.
_MAX_UPCOMING = 4


class FmpEstimatesProvider(EarningsEstimatesProvider):
    """Fetches earnings estimates + reported revenue from FMP (free API key)."""

    _DEFAULT_BASE_URL = "https://financialmodelingprep.com"

    def __init__(
        self, api_key: str, base_url: str = _DEFAULT_BASE_URL, *, limit: int = 24
    ) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)
        self._limit = limit  # rows back; covers several past quarters + upcoming

    def get_estimates(self, symbol: str) -> EarningsEstimates:
        today = date.today()
        upcoming: list[NextEarnings] = []
        reported: list[tuple[date, float | None, float | None]] = []
        for row in self._fetch(symbol):
            d = _parse_date(row.get("date"))
            if d is None:
                continue
            rev_est = _num(row.get("revenueEstimated"))
            rev_act = _num(row.get("revenueActual"))
            if d > today:
                upcoming.append(
                    NextEarnings(
                        report_date=d,
                        fiscal_year=None,
                        fiscal_quarter=None,
                        eps_estimate=_num(row.get("epsEstimated")),
                        revenue_estimate=rev_est,
                        session=None,
                    )
                )
            elif rev_est is not None or rev_act is not None:
                reported.append((d, rev_est, rev_act))
        upcoming.sort(key=lambda n: n.report_date or today)
        return EarningsEstimates(
            upcoming=tuple(upcoming[:_MAX_UPCOMING]),
            reported_revenue=tuple(reported),
        )

    def _fetch(self, symbol: str) -> list[dict]:
        try:
            resp = self._http.get(
                "/stable/earnings",
                params={"symbol": symbol, "limit": self._limit, "apikey": self._api_key},
            )
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code != 200:
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol, f"earnings request failed (HTTP {resp.status_code}): {body}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(symbol, f"invalid JSON payload: {exc}") from exc
        # A non-list payload (e.g. an error object) maps to empty — best-effort.
        return (
            [row for row in payload if isinstance(row, dict)]
            if isinstance(payload, list)
            else []
        )


def _num(value) -> float | None:
    """A finite number or None — FMP leaves unreported figures null."""
    return float(value) if isinstance(value, (int, float)) else None


def _parse_date(value) -> date | None:
    """Parse FMP's ``YYYY-MM-DD`` date (ignoring any time part); None if bad."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None
