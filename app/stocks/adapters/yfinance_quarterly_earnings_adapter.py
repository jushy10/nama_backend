"""Interface Adapter: per-quarter earnings from Yahoo Finance (via ``yfinance``).

Builds a stock's earnings timeline — the recent reported quarters and the upcoming ones —
from two yfinance surfaces:

- ``Ticker.earnings_dates`` — a date-indexed frame of announcements: each row's *EPS
  Estimate* and, once reported, its *Reported EPS*. A row with no reported EPS is a future
  quarter; the surprise is computed here from actual vs. estimate (so the result doesn't
  depend on Yahoo's own ``Surprise(%)`` column or its scale). We keep the most recent
  ``_PAST`` reported quarters and the soonest ``_FUTURE`` upcoming ones.
- ``Ticker.revenue_estimate`` — the same period-indexed frame the estimates adapter reads;
  its ``0q`` / ``+1q`` rows give forward *quarterly* revenue for the nearest one or two
  upcoming quarters (Yahoo publishes forward revenue only that far out).

Fiscal alignment is best-effort: ``earnings_dates`` carries only the announcement date,
not a fiscal label, so the quarter's ``period_end`` (and hence ``fiscal_year`` /
``fiscal_quarter``) is derived as the most recent calendar quarter-end before the
announcement. That's exact for calendar-fiscal-year companies and off-by-a-label for
others (e.g. a company whose fiscal Q1 ends in December) — a documented limitation of a
source that doesn't report fiscal periods.

This is the only module that knows ``yfinance``/Yahoo exists; swap it and nothing else
changes. It is deliberately defensive — Yahoo is an unofficial, best-effort feed that
reshapes payloads without notice and rate-limits data-centre IPs — so any vendor failure
becomes ``StockDataUnavailable`` and a symbol Yahoo doesn't cover yields an empty timeline
rather than an error. Behind the persistent DB cache, a blocked live call just serves the
stored rows.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd
import yfinance as yf

from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.exceptions import StockDataUnavailable

# ``revenue_estimate`` rows carrying forward *quarterly* revenue: the current quarter and
# the next. They line up, in order, with the nearest upcoming quarters.
_FORWARD_REVENUE_LABELS = ("0q", "+1q")


class YfinanceQuarterlyEarningsProvider(QuarterlyEarningsProvider):
    """Fetches a stock's recent and upcoming quarterly earnings from Yahoo (no API key)."""

    _PAST = 4  # most recent reported quarters to keep
    _FUTURE = 4  # soonest upcoming quarters to keep

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults
        # to the real yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        try:
            ticker = self._ticker_factory(symbol)
            dates = ticker.earnings_dates
            revenue = ticker.revenue_estimate
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance quarterly earnings failed ({exc})"
            ) from exc

        rows = _parse_rows(dates)
        reported = sorted(
            (r for r in rows if r["eps_actual"] is not None),
            key=lambda r: r["report_date"],
            reverse=True,  # newest first
        )
        upcoming = sorted(
            (r for r in rows if r["eps_actual"] is None),
            key=lambda r: r["report_date"],  # soonest first
        )

        seen: set[tuple[int, int]] = set()
        quarters: list[QuarterlyEarnings] = []

        taken = 0
        for r in reported:
            if taken >= self._PAST:
                break
            quarter = _build_quarter(r, revenue_estimate=None)
            key = (quarter.fiscal_year, quarter.fiscal_quarter)
            if key in seen:  # a restated/duplicate announcement in the same quarter
                continue
            seen.add(key)
            quarters.append(quarter)
            taken += 1

        forward_revenue = [_cell(revenue, label, "avg") for label in _FORWARD_REVENUE_LABELS]
        taken = 0
        for r in upcoming:
            if taken >= self._FUTURE:
                break
            # The nearest upcoming quarters carry a forward revenue estimate (0q, +1q);
            # further-out quarters get None. Aligned by accepted position, not raw index.
            rev = forward_revenue[taken] if taken < len(forward_revenue) else None
            quarter = _build_quarter(r, revenue_estimate=rev)
            key = (quarter.fiscal_year, quarter.fiscal_quarter)
            if key in seen:
                continue
            seen.add(key)
            quarters.append(quarter)
            taken += 1

        return QuarterlyEarningsTimeline(symbol=symbol, quarters=tuple(quarters))


def _build_quarter(row: dict, *, revenue_estimate: float | None) -> QuarterlyEarnings:
    """Assemble one quarter from a parsed announcement row, deriving the fiscal identity
    from the announcement date and the surprise from actual vs. estimate."""
    report_date: date = row["report_date"]
    period_end = _period_end_before(report_date)
    eps_actual = row["eps_actual"]
    eps_estimate = row["eps_estimate"]

    surprise: float | None = None
    surprise_percent: float | None = None
    if eps_actual is not None and eps_estimate is not None:
        surprise = round(eps_actual - eps_estimate, 4)
        if eps_estimate != 0:
            surprise_percent = round(
                (eps_actual - eps_estimate) / abs(eps_estimate) * 100, 2
            )

    return QuarterlyEarnings(
        fiscal_year=period_end.year,
        fiscal_quarter=(period_end.month - 1) // 3 + 1,
        period_end=period_end,
        report_date=report_date,
        eps_actual=eps_actual,
        eps_estimate=eps_estimate,
        eps_surprise=surprise,
        eps_surprise_percent=surprise_percent,
        revenue_estimate=revenue_estimate,
    )


def _period_end_before(report: date) -> date:
    """The most recent calendar quarter-end strictly before an announcement date.

    Earnings are announced after the quarter closes, so the quarter being reported is the
    one that most recently ended. Exact for calendar fiscal years; a label offset for
    off-calendar ones (see the module docstring)."""
    ends = [
        date(report.year, 3, 31),
        date(report.year, 6, 30),
        date(report.year, 9, 30),
        date(report.year, 12, 31),
        date(report.year - 1, 12, 31),
    ]
    return max(end for end in ends if end < report)


def _parse_rows(frame) -> list[dict]:
    """``earnings_dates`` → a list of ``{report_date, eps_estimate, eps_actual}`` dicts.

    Rows without a usable announcement date are dropped (there'd be no quarter to key on).
    Keeps all pandas/NaN handling in the adapter."""
    if frame is None or getattr(frame, "empty", True):
        return []
    rows: list[dict] = []
    try:
        pairs = list(frame.iterrows())
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return []
    for index, series in pairs:
        report_date = _to_date(index)
        if report_date is None:
            continue
        rows.append(
            {
                "report_date": report_date,
                "eps_estimate": _num(_series_get(series, "EPS Estimate")),
                "eps_actual": _num(_series_get(series, "Reported EPS")),
            }
        )
    return rows


def _series_get(series, key: str):
    """One labelled value from a row Series, or ``None`` (missing column)."""
    try:
        return series.get(key)
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _cell(frame, period: str, column: str) -> float | None:
    """One numeric cell of a period-indexed estimate frame as a float, or ``None``
    (missing row/column, NaN, or non-numeric)."""
    try:
        if frame is None or getattr(frame, "empty", True):
            return None
        if period not in frame.index or column not in frame.columns:
            return None
        return _num(frame.loc[period, column])
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return None


def _num(value) -> float | None:
    """A pandas/NumPy/Python scalar → float, or ``None`` (missing, NaN/NaT, non-numeric)."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _to_date(value) -> date | None:
    """A pandas ``Timestamp`` / ``datetime`` (the ``earnings_dates`` index) → a ``date``;
    ``None`` for ``NaT`` or an unrecognized index value."""
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):  # pandas Timestamp is a datetime subclass
        return value.date()
    if isinstance(value, date):
        return value
    return None
