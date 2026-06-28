"""Interface Adapter: recently-reported quarterly revenue from SEC EDGAR.

SEC EDGAR's XBRL REST APIs expose what every US filer reported in its 10-Q/10-K
filings — for free, with no API key. We read the candidate revenue concepts and
reduce them to a per-quarter map of actuals to overlay onto the EPS beat history,
which is revenue-blind. This is the only module that knows EDGAR exists; swap it
and nothing else changes.

Two wrinkles this adapter handles:

* EDGAR is keyed by CIK, not ticker, so we resolve the ticker through SEC's
  ``company_tickers.json`` map (fetched once and held for the process).
* A 10-K reports the *full year*, not a standalone Q4. Where only an annual
  figure exists for a fiscal year, Q4 is derived as ``annual - (Q1+Q2+Q3)``.

EDGAR requires a descriptive ``User-Agent`` (generic agents get a 403) and rate
limits to ~10 req/s per IP; the caching decorator in front keeps us well under.

Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""

from __future__ import annotations

import threading
from datetime import date

import httpx

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import RevenueHistoryProvider

# us-gaap revenue concepts. Filers tag revenue differently and change tags over
# time — across the ASC 606 transition (older filings used SalesRevenueNet), and
# between the assessed-tax presentations (Lumentum moved from the Excluding to the
# Including variant in FY2026). We fetch every candidate and merge the facts (see
# _fetch_revenue_rows), so a tag going stale never hides the newer quarters.
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)

# Duration bands (days) that classify an XBRL period as a single quarter vs a
# full year. Quarters land near 91 days, fiscal years near 365; the wide bands
# absorb 52/53-week ("4-4-5") fiscal calendars. Year-to-date facts (≈182/273
# days) fall between the bands and are ignored, which is what we want.
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100
_ANNUAL_MIN_DAYS = 350
_ANNUAL_MAX_DAYS = 380
# A quarter "belongs" to the fiscal year ending at ``year_end`` when its own end
# falls within this many days before it — wide enough to catch the year's first
# quarter (~273 days back), tight enough to exclude the prior year's (~364).
_FISCAL_YEAR_LOOKBACK_DAYS = 330


class SecEdgarRevenueProvider(RevenueHistoryProvider):
    """Fetches reported quarterly revenue from SEC EDGAR (free, no key)."""

    _TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    _CONCEPT_URL = (
        "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
    )
    # SEC asks callers to identify themselves and rejects generic agents; the
    # wiring overrides this from the env so ops can set a real contact.
    _DEFAULT_USER_AGENT = "nama_backend/1.0 (https://namainsights.com)"

    def __init__(
        self,
        user_agent: str = _DEFAULT_USER_AGENT,
        *,
        tickers_url: str | None = None,
        concept_url: str | None = None,
    ) -> None:
        self._http = httpx.Client(timeout=10.0, headers={"User-Agent": user_agent})
        self._tickers_url = tickers_url or self._TICKERS_URL
        self._concept_url = concept_url or self._CONCEPT_URL
        # The ticker -> CIK map is one shared fetch for every symbol; build it
        # lazily and hold it for the process (it changes rarely).
        self._lock = threading.Lock()
        self._cik_by_symbol: dict[str, int] | None = None

    def get_quarterly_revenue(self, symbol: str) -> dict[date, float]:
        cik = self._resolve_cik(symbol)
        rows = self._fetch_revenue_rows(symbol, cik)
        return _quarterly_revenue(rows)

    # -- CIK resolution -----------------------------------------------------

    def _resolve_cik(self, symbol: str) -> int:
        cik = self._cik_map().get(symbol.upper())
        if cik is None:
            # Not an EDGAR filer we can resolve (e.g. an ADR or ETF) — treat it
            # as "no data", like the other best-effort enrichment providers.
            raise StockNotFound(symbol)
        return cik

    def _cik_map(self) -> dict[str, int]:
        with self._lock:
            if self._cik_by_symbol is not None:
                return self._cik_by_symbol
        # Fetch outside the lock; a rare concurrent double-fetch is harmless.
        payload = self._get_json("company_tickers", self._tickers_url)
        mapping = _parse_cik_map(payload)
        with self._lock:
            self._cik_by_symbol = mapping
        return mapping

    # -- revenue fetch ------------------------------------------------------

    def _fetch_revenue_rows(self, symbol: str, cik: int) -> list[dict]:
        """Return the USD revenue facts merged across every candidate tag.

        A filer can change which us-gaap concept it reports revenue under over
        time — Alphabet moved from ``RevenueFromContractWithCustomerExcludingAssessedTax``
        to ``Revenues`` — so we union all of them rather than trusting the first
        tag that has data: a tag that went stale would otherwise mask the newer
        quarters. Overlapping periods are de-duplicated downstream (the latest
        filing wins per period end).
        """
        merged: list[dict] = []
        for tag in _REVENUE_TAGS:
            url = self._concept_url.format(cik=cik, tag=tag)
            try:
                resp = self._http.get(url)
            except httpx.HTTPError as exc:
                raise StockDataUnavailable(symbol, str(exc)) from exc
            if resp.status_code == 404:
                continue  # the filer doesn't use this tag
            if resp.status_code != 200:
                body = resp.text[:200].strip() or "<empty body>"
                raise StockDataUnavailable(
                    symbol,
                    f"EDGAR concept request failed (HTTP {resp.status_code}): {body}",
                )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise StockDataUnavailable(
                    symbol, f"invalid JSON payload: {exc}"
                ) from exc
            units = payload.get("units") if isinstance(payload, dict) else None
            rows = units.get("USD") if isinstance(units, dict) else None
            if rows:
                merged.extend(r for r in rows if isinstance(r, dict))
        return merged  # empty when no revenue concept is covered (best-effort)

    def _get_json(self, symbol: str, url: str):
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code != 200:
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol, f"EDGAR request failed (HTTP {resp.status_code}): {body}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(symbol, f"invalid JSON payload: {exc}") from exc


def _parse_cik_map(payload) -> dict[str, int]:
    """Build a TICKER -> CIK map from SEC's ``company_tickers.json``.

    The file is an object of rows like ``{"cik_str": 320193, "ticker": "AAPL"}``.
    """
    out: dict[str, int] = {}
    rows = payload.values() if isinstance(payload, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        cik = row.get("cik_str")
        if isinstance(ticker, str) and isinstance(cik, int):
            out[ticker.upper()] = cik
    return out


def _quarterly_revenue(rows: list[dict]) -> dict[date, float]:
    """Reduce raw XBRL USD facts to a per-quarter map of revenue actuals.

    Keeps the genuine ~3-month facts keyed by period end, then fills in each
    fiscal year's missing Q4 as ``annual - (Q1+Q2+Q3)`` where a 12-month fact
    exists but no standalone Q4 one does.
    """
    quarterly: dict[date, float] = {}
    annual: dict[date, float] = {}
    # Later filings (re-statements) should win over earlier ones for the same
    # period, so iterate oldest-filed first and let the last write stand.
    for row in sorted(rows, key=lambda r: str(r.get("filed", ""))):
        start = _parse_date(row.get("start"))
        end = _parse_date(row.get("end"))
        val = row.get("val")
        if start is None or end is None or not isinstance(val, (int, float)):
            continue
        span = (end - start).days
        if _QUARTER_MIN_DAYS <= span <= _QUARTER_MAX_DAYS:
            quarterly[end] = float(val)
        elif _ANNUAL_MIN_DAYS <= span <= _ANNUAL_MAX_DAYS:
            annual[end] = float(val)

    for year_end, year_val in annual.items():
        if year_end in quarterly:
            continue  # a standalone Q4 fact already exists for this year end
        priors = sorted(
            q
            for q in quarterly
            if 0 < (year_end - q).days <= _FISCAL_YEAR_LOOKBACK_DAYS
        )
        if len(priors) == 3:  # the year's Q1–Q3; Q4 is the remainder
            quarterly[year_end] = year_val - sum(quarterly[q] for q in priors)
    return quarterly


def _parse_date(value) -> date | None:
    """Parse EDGAR's ``YYYY-MM-DD`` date; ``None`` if absent or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None
