"""Interface Adapter: analyst estimates from Financial Modeling Prep.

Two FMP endpoints feed the earnings view's forward and revenue gaps, which the
Finnhub sources can't fill on the free tier:

* ``analyst-estimates`` (per quarter) — consensus EPS/revenue for several
  *future* quarters (the "multiple upcoming quarters" view) plus the consensus
  revenue for past quarters.
* ``income-statement`` (per quarter) — the *actual* revenue each past quarter
  reported.

Both key on the fiscal period-end date, so reported revenue lands as
estimate-vs-actual on the matching beat-history quarter. We prefer FMP's
"stable" API and fall back to the legacy ``/api/v3`` one, the same dual-endpoint
handling the profile adapter uses. Best-effort: any miss yields empty data.

Docs: https://site.financialmodelingprep.com/developer/docs/stable/financial-estimates
"""

from datetime import date

import httpx

from app.stocks.entities import EarningsEstimates, NextEarnings
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ports import EarningsEstimatesProvider

# Cap the forward view so the chart stays legible — the nearest few quarters.
_MAX_UPCOMING = 4


class FmpEstimatesProvider(EarningsEstimatesProvider):
    """Fetches analyst estimates + reported revenue from FMP (free API key)."""

    _DEFAULT_BASE_URL = "https://financialmodelingprep.com"

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)

    def get_estimates(self, symbol: str) -> EarningsEstimates:
        today = date.today()
        estimates = self._fetch(
            symbol,
            (
                ("/stable/analyst-estimates", {"symbol": symbol, "period": "quarter", "limit": 40}),
                (f"/api/v3/analyst-estimates/{symbol}", {"period": "quarter", "limit": 40}),
            ),
        )
        income = self._fetch(
            symbol,
            (
                ("/stable/income-statement", {"symbol": symbol, "period": "quarter", "limit": 16}),
                (f"/api/v3/income-statement/{symbol}", {"period": "quarter", "limit": 16}),
            ),
        )

        # Actual revenue per reported period, keyed by period-end date.
        actuals: dict[date, float | None] = {}
        for row in income:
            d = _parse_date(row.get("date"))
            if d is not None:
                actuals[d] = _first(row, "revenue")

        upcoming: list[NextEarnings] = []
        revenue_by_period: dict[date, tuple[float | None, float | None]] = {}
        for row in estimates:
            d = _parse_date(row.get("date"))
            if d is None:
                continue
            eps_est = _first(row, "epsAvg", "estimatedEpsAvg")
            rev_est = _first(row, "revenueAvg", "estimatedRevenueAvg")
            if d > today:
                upcoming.append(
                    NextEarnings(
                        report_date=d,
                        fiscal_year=None,
                        fiscal_quarter=None,
                        eps_estimate=eps_est,
                        revenue_estimate=rev_est,
                        session=None,
                    )
                )
            elif rev_est is not None or d in actuals:
                revenue_by_period[d] = (rev_est, actuals.get(d))

        upcoming.sort(key=lambda n: n.report_date or today)
        return EarningsEstimates(
            upcoming=tuple(upcoming[:_MAX_UPCOMING]),
            revenue_by_period=revenue_by_period,
        )

    def _fetch(self, symbol: str, routes: tuple) -> list[dict]:
        """Fetch a JSON list from the first route that answers, else raise.

        Prefers the stable endpoint, falls back to legacy ``/api/v3`` (some keys
        are scoped to one API). A non-list payload (e.g. an error object) maps to
        an empty list so a partially-covered symbol degrades gracefully."""
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
                payload = resp.json()
            except ValueError as exc:
                last_error = f"invalid JSON: {exc}"
                continue
            return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
        raise StockDataUnavailable(symbol, f"estimates request failed ({last_error})")


def _first(row: dict, *keys: str) -> float | None:
    """First present, non-null numeric value among candidate keys."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _parse_date(value) -> date | None:
    """Parse FMP's ``YYYY-MM-DD`` date (ignoring any time part); None if bad."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None
