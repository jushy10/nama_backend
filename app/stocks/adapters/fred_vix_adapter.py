"""Interface Adapter: the CBOE Volatility Index (VIX) from FRED.

The Federal Reserve Bank of St. Louis (FRED) publishes CBOE's official VIX close
as the daily series ``VIXCLS`` with full history. We fetch it as a plain CSV and
read the two most recent observations into a ``VixSnapshot`` — the latest close
plus the immediately preceding one, so the entity can report the day-over-day
change. It's the only module that knows FRED backs the VIX; swap it for another
``VixProvider`` and only this file changes.

**Keyless**, and — like the FRED yield-history source — the CSV download endpoint
needs no API key and serves data-centre IPs, so it works from Fargate where the
Yahoo endpoints block us. This makes it the reliable, authoritative VIX source.
The one caveat is freshness: ``VIXCLS`` is an **end-of-day close** and can lag by
up to ~1 business day, so ``VixSnapshot.as_of`` is surfaced for an honest
"as of {date}" label rather than being presented as real-time.

``_http`` is the fake seam the offline tests swap; the CSV parse (``_parse_observations``)
is a pure function driven directly by the tests.
"""

from __future__ import annotations

import csv
import datetime
import logging
from io import StringIO

import httpx

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.sentiment.entities import VixSnapshot
from app.stocks.sentiment.ports import VixProvider

logger = logging.getLogger(__name__)

_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"

_USER_AGENT = "nama-backend/1.0 (CBOE VIX; +https://namainsights.com)"

# The VIX has no per-stock symbol; sentinel for a source-wide failure message,
# matching the other whole-market adapters.
_VIX = "*"


class FredVixProvider(VixProvider):
    """Reads the latest VIX close (and the prior close) from FRED (keyless)."""

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )

    def get_vix(self) -> VixSnapshot:
        try:
            resp = self._http.get(_URL)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(
                _VIX, f"FRED request for VIXCLS failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise StockDataUnavailable(
                _VIX, f"FRED returned HTTP {resp.status_code} for VIXCLS"
            )
        observations = _parse_observations(resp.text)
        if not observations:
            raise StockDataUnavailable(_VIX, "FRED returned no VIXCLS observations")
        latest_date, latest_value = observations[-1]
        previous_close = observations[-2][1] if len(observations) >= 2 else None
        return VixSnapshot(
            as_of=latest_date, value=latest_value, previous_close=previous_close
        )


def _parse_observations(text: str) -> list[tuple[datetime.date, float]]:
    """Parse a FRED CSV into chronological ``(date, value)`` observations.

    Pure function (the tested seam). FRED marks missing days with ``.``; those
    rows are dropped. The date column is ISO (``YYYY-MM-DD``) and the value
    column's header is the series id, so we read by position (col 0 date, col 1
    value) rather than by name. Rows can arrive out of order, so we sort.
    """
    reader = csv.reader(StringIO(text))
    rows = iter(reader)
    next(rows, None)  # skip the header row
    observations: list[tuple[datetime.date, float]] = []
    for row in rows:
        if len(row) < 2:
            continue
        parsed_date = _parse_date(row[0].strip())
        if parsed_date is None:
            continue
        value = _parse_value(row[1])
        if value is None:
            continue
        observations.append((parsed_date, value))
    observations.sort(key=lambda o: o[0])
    return observations


def _parse_date(value: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_value(value: str) -> float | None:
    text = value.strip()
    if not text or text == ".":
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None
