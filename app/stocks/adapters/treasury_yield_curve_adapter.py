"""Interface Adapter: the US Treasury par-yield curve from Treasury.gov.

The US Treasury publishes the **Daily Treasury Par Yield Curve Rates** as a
per-year CSV — one row per business day, one column per maturity (1 Mo … 30 Yr).
We fetch the current year's file and read the most recent row into a
``YieldCurve`` across every quoted maturity. It's the only module that knows
Treasury.gov backs the curve; swap it for another ``YieldCurveProvider`` and
only this file changes.

**Keyless**, and — like SEC EDGAR — the Treasury welcomes programmatic reads
from data-centre IPs (it works from Fargate where the Yahoo endpoints block us),
so there's no IP-block retry machinery; we send a descriptive ``User-Agent`` as
a courtesy. One CSV call returns the whole curve, which is why the curve is read
live per request with no table or cron.

At the very start of a calendar year the current-year file can be briefly empty
(no business day has printed yet), so a blank current year falls back to the
prior year's file. ``_http`` is the fake seam the offline tests swap; ``_today``
is injectable so the tests pin the year without touching the clock.
"""

from __future__ import annotations

import csv
import datetime
import logging
from io import StringIO

import httpx

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.yields.entities import YieldCurve, YieldTenor
from app.stocks.yields.ports import YieldCurveProvider

logger = logging.getLogger(__name__)

_BASE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
)
_QUERY = "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"

# Treasury asks automated clients to identify themselves.
_USER_AGENT = "nama-backend/1.0 (treasury yield curve; +https://namainsights.com)"

# The curve has no single symbol; use a sentinel so a source-wide failure reads
# sensibly ("'*' is unavailable: …"), matching the other whole-market adapters.
_CURVE = "*"

# Map each CSV column header to (display label, tenor in months). Anything not
# listed here (a stray column) is ignored; a listed column that's blank on the
# latest row (a maturity Treasury didn't quote that day) is simply skipped.
_COLUMNS: dict[str, tuple[str, float]] = {
    "1 Mo": ("1M", 1.0),
    "1.5 Month": ("1.5M", 1.5),
    "2 Mo": ("2M", 2.0),
    "3 Mo": ("3M", 3.0),
    "4 Mo": ("4M", 4.0),
    "6 Mo": ("6M", 6.0),
    "1 Yr": ("1Y", 12.0),
    "2 Yr": ("2Y", 24.0),
    "3 Yr": ("3Y", 36.0),
    "5 Yr": ("5Y", 60.0),
    "7 Yr": ("7Y", 84.0),
    "10 Yr": ("10Y", 120.0),
    "20 Yr": ("20Y", 240.0),
    "30 Yr": ("30Y", 360.0),
}


class TreasuryYieldCurveProvider(YieldCurveProvider):
    """Reads the current US Treasury par-yield curve from Treasury.gov (keyless)."""

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )
        # Injectable clock so tests pin the year deterministically.
        self._today = datetime.date.today

    def get_yield_curve(self) -> YieldCurve:
        year = self._today().year
        curve = self._fetch_year(year)
        if curve is None:
            # A fresh calendar year may not have printed a business day yet.
            curve = self._fetch_year(year - 1)
        if curve is None:
            raise StockDataUnavailable(
                _CURVE, "no Treasury par-yield curve rows were returned"
            )
        return curve

    def _fetch_year(self, year: int) -> YieldCurve | None:
        """Fetch and parse one year's CSV, returning the latest row's curve.

        Returns ``None`` when the file has no data rows (so the caller can fall
        back to the prior year); raises ``StockDataUnavailable`` on a transport
        or non-200 failure, which is a real outage, not an empty year.
        """
        url = (_BASE_URL + _QUERY).format(year=year)
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(
                _CURVE, f"Treasury request failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise StockDataUnavailable(
                _CURVE, f"Treasury returned HTTP {resp.status_code}"
            )
        return _parse_latest_curve(resp.text)


def _parse_latest_curve(text: str) -> YieldCurve | None:
    """Parse a Treasury year CSV and return the most recent day's curve.

    Pure function (the tested seam): the rows can arrive newest- or oldest-first,
    so we scan for the row with the maximum date rather than trusting order.
    Returns ``None`` if there are no parseable data rows.
    """
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None:
        return None
    # Normalize header whitespace so the column map lines up.
    headers = {name: name.strip() for name in reader.fieldnames}

    latest_date: datetime.date | None = None
    latest_row: dict[str, str] | None = None
    for row in reader:
        raw_date = (row.get("Date") or "").strip()
        parsed = _parse_date(raw_date)
        if parsed is None:
            continue
        if latest_date is None or parsed > latest_date:
            latest_date = parsed
            latest_row = row
    if latest_date is None or latest_row is None:
        return None

    tenors: list[YieldTenor] = []
    for raw_name, clean_name in headers.items():
        mapping = _COLUMNS.get(clean_name)
        if mapping is None:
            continue
        label, months = mapping
        rate = _parse_rate(latest_row.get(raw_name))
        if rate is None:
            continue
        tenors.append(YieldTenor(label=label, months=months, rate=rate))

    if not tenors:
        return None
    tenors.sort(key=lambda t: t.months)
    return YieldCurve(as_of=latest_date, tenors=tuple(tenors))


def _parse_date(value: str) -> datetime.date | None:
    """Parse Treasury's ``MM/DD/YYYY`` date, or ``None`` if it isn't one."""
    try:
        return datetime.datetime.strptime(value, "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return None


def _parse_rate(value: str | None) -> float | None:
    """Parse a yield cell to a float percent, or ``None`` if blank/non-numeric."""
    if value is None:
        return None
    text = value.strip()
    if not text or text.upper() in {"N/A", "NA"}:
        return None
    try:
        return round(float(text), 4)
    except ValueError:
        return None
