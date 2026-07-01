"""Interface Adapter: forward analyst estimates from FMP.

A stock's *forward* consensus — what sell-side analysts expect EPS and revenue to
be over the next fiscal years — comes from an estimates vendor, not the price feed
(Alpaca) or company filings (SEC EDGAR, which carry only reported actuals).
Financial Modeling Prep's ``analyst-estimates`` endpoint returns one row per fiscal
period; we read the **annual** cadence (available on FMP's free tier — the
quarterly cadence is gated), and each row carries the mean/low/high EPS and revenue
estimate plus how many analysts contributed. We keep the nearest forward fiscal
year (FY1) plus the one after (FY2) — what a forward P/E and a next-twelve-months
blend need.

We read FMP's "stable" endpoint first and fall back to the older ``/api/v3`` one
(some keys are scoped to the legacy API) — the same dual-endpoint handling the
profile adapter and the constituents sync use. The two APIs name the fields
differently (``epsAvg`` vs ``estimatedEpsAvg``), so the parser accepts either. This
is the only module that knows FMP estimates exist; swap it and nothing else changes.

Docs: https://site.financialmodelingprep.com/developer/docs (Analyst Estimates)
"""

from datetime import date

import httpx

from app.stocks.entities import AnalystEstimates, ForwardEstimate
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.estimates.estimates_ports import AnalystEstimatesProvider

# An uncovered symbol (or one with no forward year) yields this rather than an
# error — best-effort, like the profile adapter returning an all-None profile.
_EMPTY = AnalystEstimates(
    fiscal_year=None,
    period_end=None,
    eps_avg=None,
    eps_low=None,
    eps_high=None,
    revenue_avg=None,
    num_analysts_eps=None,
    num_analysts_revenue=None,
)


class FmpEstimatesProvider(AnalystEstimatesProvider):
    """Fetches forward annual analyst estimates from FMP (the free API key works)."""

    _DEFAULT_BASE_URL = "https://financialmodelingprep.com"

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        today: date | None = None,
    ) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=10.0)
        # Injectable "now" so the forward/past split is deterministic in tests;
        # defaults to the real clock in production.
        self._today = today

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        rows = self._fetch_estimates(symbol)
        today = self._today or date.today()
        # FMP returns past *and* future fiscal years in one payload; keep only the
        # forward ones (period end today or later) and order them soonest-first, so
        # the first is FY1 (the nearest forward year) and the second FY2.
        forward = sorted(
            (
                parsed
                for row in rows
                if (parsed := _parse_row(row)) is not None and parsed[0] >= today
            ),
            key=lambda parsed: parsed[0],
        )
        if not forward:
            return _EMPTY
        period_end, eps_avg, eps_low, eps_high, revenue_avg, n_eps, n_rev = forward[0]
        fy2 = forward[1] if len(forward) > 1 else None
        # The full forward series (every estimated year) backs the one-year
        # forward growth (FY1→FY2); row = (period_end, epsAvg, low, high, revAvg, ...).
        forward_years = tuple(
            ForwardEstimate(
                fiscal_year=row[0].year,
                period_end=row[0],
                eps_avg=row[1],
                revenue_avg=row[4],
            )
            for row in forward
        )
        return AnalystEstimates(
            fiscal_year=period_end.year,
            period_end=period_end,
            eps_avg=eps_avg,
            eps_low=eps_low,
            eps_high=eps_high,
            revenue_avg=revenue_avg,
            num_analysts_eps=n_eps,
            num_analysts_revenue=n_rev,
            eps_avg_fy2=fy2[1] if fy2 else None,
            fiscal_year_fy2=fy2[0].year if fy2 else None,
            forward_years=forward_years,
        )

    def _fetch_estimates(self, symbol: str) -> list:
        """Fetch the raw estimates list, preferring the stable endpoint and falling
        back to legacy ``/api/v3`` (some keys are scoped to one API). Raises only
        when every endpoint fails the request, so the body is self-explaining."""
        routes = (
            ("/stable/analyst-estimates", {"symbol": symbol, "period": "annual"}),
            (f"/api/v3/analyst-estimates/{symbol}", {"period": "annual"}),
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
                payload = resp.json()
            except ValueError as exc:
                last_error = f"invalid JSON: {exc}"
                continue
            # FMP returns a JSON array of per-year estimate objects; an unknown
            # symbol yields an empty list, which maps cleanly to "no estimates".
            if isinstance(payload, list):
                return [row for row in payload if isinstance(row, dict)]
            return []
        raise StockDataUnavailable(symbol, f"estimates request failed ({last_error})")


def _parse_row(row: dict):
    """Parse one FMP estimate row, or ``None`` when it has no usable period date.

    Accepts both the stable (``epsAvg``) and legacy v3 (``estimatedEpsAvg``) field
    names. Returns ``(period_end, eps_avg, eps_low, eps_high, revenue_avg,
    num_eps, num_revenue)``.
    """
    period_end = _parse_date(row.get("date"))
    if period_end is None:
        return None
    return (
        period_end,
        _num(row, "epsAvg", "estimatedEpsAvg"),
        _num(row, "epsLow", "estimatedEpsLow"),
        _num(row, "epsHigh", "estimatedEpsHigh"),
        _num(row, "revenueAvg", "estimatedRevenueAvg"),
        _int(row, "numAnalystsEps", "numberAnalystsEstimatedEps"),
        _int(row, "numAnalystsRevenue", "numberAnalystEstimatedRevenue"),
    )


def _num(row: dict, *keys: str) -> float | None:
    """First present numeric value among candidate keys, as a float (bools ignored)."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _int(row: dict, *keys: str) -> int | None:
    """First present integral value among candidate keys, as an int."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


def _parse_date(value) -> date | None:
    """Parse FMP's ``YYYY-MM-DD`` period date; ``None`` if absent or malformed."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None
