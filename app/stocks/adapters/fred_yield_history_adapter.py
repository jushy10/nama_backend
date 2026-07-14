"""Interface Adapter: the 2Y/10Y Treasury yield history from FRED.

The Federal Reserve Bank of St. Louis (FRED) publishes each constant-maturity
Treasury yield as a daily series with full history: ``DGS2`` (2-year) and
``DGS10`` (10-year). We fetch each as a plain CSV and read the trailing window
into ``YieldSeries`` entities. It's the only module that knows FRED backs the
history; swap it for another ``YieldHistoryProvider`` and only this file changes.

**Keyless** — the CSV download endpoint needs no API key and, like Treasury.gov,
serves data-centre IPs, so it works from Fargate where the Yahoo endpoints block
us. We fetch the two series (one call each) and pair them into a ``YieldHistory``;
because the whole point of the read is the 2Y-vs-10Y comparison, an empty or
failed series is a real outage (``StockDataUnavailable``), not a soft-degrade.
``_http`` is the fake seam the offline tests swap; ``_today`` is injectable so
the trailing-window cutoff is deterministic in tests.
"""

from __future__ import annotations

import csv
import datetime
import logging
from io import StringIO

import httpx

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.yields.entities import YieldHistory, YieldObservation, YieldSeries
from app.stocks.yields.ports import YieldHistoryProvider

logger = logging.getLogger(__name__)

_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

_USER_AGENT = "nama-backend/1.0 (treasury yield history; +https://namainsights.com)"

# The series we pull, in the order they should appear (2Y then 10Y), mapped to
# the maturity label the entity uses.
_SERIES: tuple[tuple[str, str], ...] = (("DGS2", "2Y"), ("DGS10", "10Y"))

# History has no single symbol; sentinel for a source-wide failure message.
_HISTORY = "*"


class FredYieldHistoryProvider(YieldHistoryProvider):
    """Reads the 2Y and 10Y Treasury yield history from FRED (keyless)."""

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )
        self._today = datetime.date.today

    def get_yield_history(self, lookback_days: int) -> YieldHistory:
        cutoff = self._today() - datetime.timedelta(days=lookback_days)
        series = tuple(
            self._fetch_series(series_id, label, cutoff)
            for series_id, label in _SERIES
        )
        return YieldHistory(series=series)

    def _fetch_series(
        self, series_id: str, label: str, cutoff: datetime.date
    ) -> YieldSeries:
        url = _BASE_URL.format(series_id=series_id)
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(
                _HISTORY, f"FRED request for {series_id} failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise StockDataUnavailable(
                _HISTORY, f"FRED returned HTTP {resp.status_code} for {series_id}"
            )
        observations = _parse_series(resp.text, cutoff)
        if not observations:
            raise StockDataUnavailable(
                _HISTORY, f"FRED returned no observations for {series_id}"
            )
        return YieldSeries(label=label, observations=observations)


def _parse_series(
    text: str, cutoff: datetime.date
) -> tuple[YieldObservation, ...]:
    """Parse a FRED CSV into chronological observations on/after ``cutoff``.

    Pure function (the tested seam). FRED marks missing days with ``.``; those
    rows are dropped. The date column is ISO (``YYYY-MM-DD``); the value column's
    header is the series id, so we read by position (col 0 date, col 1 value)
    rather than name.
    """
    reader = csv.reader(StringIO(text))
    rows = iter(reader)
    next(rows, None)  # skip the header row
    observations: list[YieldObservation] = []
    for row in rows:
        if len(row) < 2:
            continue
        parsed_date = _parse_date(row[0].strip())
        if parsed_date is None or parsed_date < cutoff:
            continue
        rate = _parse_rate(row[1])
        if rate is None:
            continue
        observations.append(YieldObservation(on=parsed_date, rate=rate))
    observations.sort(key=lambda o: o.on)
    return tuple(observations)


def _parse_date(value: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_rate(value: str) -> float | None:
    text = value.strip()
    if not text or text == ".":
        return None
    try:
        return round(float(text), 4)
    except ValueError:
        return None
