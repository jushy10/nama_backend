from __future__ import annotations

import csv
import datetime
import logging
from io import StringIO

import httpx

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.market.yields.entities import YieldHistory, YieldObservation, YieldSeries
from app.stocks.market.yields.ports import YieldHistoryProvider

logger = logging.getLogger(__name__)

_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

_USER_AGENT = "nama-backend/1.0 (treasury yield history; +https://namainsights.com)"

# The series we pull, in the order they should appear (2Y then 10Y), mapped to
# the maturity label the entity uses.
_SERIES: tuple[tuple[str, str], ...] = (("DGS2", "2Y"), ("DGS10", "10Y"))

# History has no single symbol; sentinel for a source-wide failure message.
_HISTORY = "*"


class FredYieldHistoryProvider(YieldHistoryProvider):
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
