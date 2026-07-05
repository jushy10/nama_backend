"""Interface Adapter: per-quarter earnings from Yahoo Finance (via ``yfinance``).

Builds a stock's earnings timeline — the recent reported quarters and the upcoming ones —
from three yfinance surfaces:

- ``Ticker.earnings_dates`` — a date-indexed frame of announcements: each row's *EPS
  Estimate* and, once reported, its *Reported EPS*. The rows with a reported EPS are the
  **past** quarters; we keep the most recent ``_PAST`` of them, and the surprise is computed
  here from actual vs. estimate (so the result doesn't depend on Yahoo's own ``Surprise(%)``
  column or its scale).
- ``Ticker.earnings_estimate`` / ``Ticker.revenue_estimate`` — period-indexed frames whose
  ``0q`` (current quarter) and ``+1q`` (the next one) rows carry the forward consensus EPS
  and revenue. These are the source of the **upcoming** quarters: Yahoo publishes structured
  forward estimates only two quarters out, so this yields *at most two* upcoming quarters —
  and it yields both even when ``earnings_dates`` lists only a single scheduled future date
  (which is common). A scheduled date from ``earnings_dates`` is attached as the report date
  when one lines up with the quarter.
- ``Ticker.quarterly_income_stmt`` — the reported ``Total Revenue`` per quarter, supplying
  the past quarters' ``revenue_actual``. Its columns carry the *true* fiscal period-end
  dates, so each quarter's revenue is the column most recently preceding its announcement
  date — never the calendar-derived fiscal label, which for an off-calendar filer (e.g. MU,
  quarters ending late Feb/May/Aug/Nov) names a different fiscal quarter than the one the
  EPS was reported for. Best-effort enrichment: a failure fetching it drops the reported
  revenue but never sinks the timeline.

Fiscal alignment is best-effort: ``earnings_dates`` carries only the announcement date, not
a fiscal label, so a quarter's ``period_end`` (and hence ``fiscal_year`` / ``fiscal_quarter``)
is derived as the most recent calendar quarter-end before the announcement; the upcoming
quarters are anchored one quarter past the latest reported one. That's exact for
calendar-fiscal-year companies and off-by-a-label for others (e.g. a company whose fiscal Q1
ends in December) — a documented limitation of a source that doesn't report fiscal periods.
The offset is cosmetic only: within a row, the EPS and the revenue always belong to the same
fiscal quarter, because revenue is matched by real period proximity, not by that label.

This is the only module that knows ``yfinance``/Yahoo exists; swap it and nothing else
changes. It is deliberately defensive — Yahoo is an unofficial, best-effort feed that
reshapes payloads without notice and rate-limits data-centre IPs — so any vendor failure
becomes ``StockDataUnavailable`` and a symbol Yahoo doesn't cover yields an empty timeline
rather than an error. Behind the persistent DB cache, a blocked live call just serves the
stored rows. Every Yahoo read is routed through ``yfinance_session`` (pacing + a one-shot
fresh-crumb retry on a 401); ``earnings_dates`` (the reported source) and
``quarterly_income_stmt`` additionally retry on an *empty* result, since a swallowed crumb
401 surfaces that way.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd
import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.exceptions import StockDataUnavailable

# The two forward quarters Yahoo publishes structured estimates for, in order: the current
# quarter (in progress, next to report) and the one after it.
_FORWARD_LABELS = ("0q", "+1q")

# The longest plausible gap between a fiscal quarter's end and its earnings announcement.
# Announcements land ~3–8 weeks after the close (the 10-Q deadline is 40–45 days; year-end
# reports stretch longer), while the *previous* quarter's end sits a further ~13 weeks back —
# ≥ ~115 days before the announcement — so this cap separates "the quarter being announced"
# from "the income statement hasn't published this quarter yet".
_MAX_REPORT_LAG_DAYS = 90


class YfinanceQuarterlyEarningsProvider(QuarterlyEarningsProvider):
    """Fetches a stock's recent and upcoming quarterly earnings from Yahoo (no API key)."""

    _PAST = 4  # most recent reported quarters to keep (upcoming is capped at two: 0q, +1q)

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults
        # to the real yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        try:
            ticker = self._ticker_factory(symbol)
            # earnings_dates is the primary reported source — a real company always has
            # history, so an empty frame means a swallowed crumb 401: retry with a fresh
            # crumb. The forward estimate frames are legitimately empty for an uncovered
            # stock, so they get pacing + a raised-401 retry but no retry-on-empty.
            dates = yfinance_session.call(
                lambda: ticker.earnings_dates, is_empty=yfinance_session.frame_is_empty
            )
            eps_estimate = yfinance_session.call(lambda: ticker.earnings_estimate)
            revenue_estimate = yfinance_session.call(lambda: ticker.revenue_estimate)
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance quarterly earnings failed ({exc})"
            ) from exc

        # Reported revenue is best-effort enrichment on the past quarters — a failure
        # fetching the income statement must not sink the (primary) earnings timeline. Like
        # the annual fundamentals endpoint, an empty result is a swallowed crumb 401, so
        # retry once with a fresh crumb.
        try:
            income_stmt = yfinance_session.call(
                lambda: ticker.quarterly_income_stmt,
                is_empty=yfinance_session.frame_is_empty,
            )
        except Exception:  # noqa: BLE001 — enrichment: degrade to no revenue_actual
            income_stmt = None
        revenue_actuals = _revenue_actuals(income_stmt)

        rows = _parse_rows(dates)
        reported = sorted(
            (r for r in rows if r["eps_actual"] is not None),
            key=lambda r: r["report_date"],
            reverse=True,  # newest first
        )
        future_dates = sorted(r["report_date"] for r in rows if r["eps_actual"] is None)

        seen: set[tuple[int, int]] = set()
        quarters: list[QuarterlyEarnings] = []

        # Past: keep the most recent reported quarters — walk newest-first and cap at
        # _PAST (the whole timeline is re-sorted chronologically before returning).
        for row in reported:
            if len(quarters) >= self._PAST:
                break
            quarter = _build_reported(row, revenue_actuals)
            key = (quarter.fiscal_year, quarter.fiscal_quarter)
            if key in seen:  # a restated/duplicate announcement in the same quarter
                continue
            seen.add(key)
            quarters.append(quarter)

        # Upcoming: the 0q/+1q forward estimates (at most two), so a stock with a single
        # scheduled future date still surfaces both quarters Yahoo estimates.
        for quarter in _upcoming_quarters(
            reported, future_dates, eps_estimate, revenue_estimate
        ):
            key = (quarter.fiscal_year, quarter.fiscal_quarter)
            if key in seen:
                continue
            seen.add(key)
            quarters.append(quarter)

        # Emit the timeline chronologically: ascending by (fiscal_year, fiscal_quarter),
        # so the oldest reported quarter leads through to the furthest upcoming one.
        quarters.sort(key=lambda q: (q.fiscal_year, q.fiscal_quarter))
        return QuarterlyEarningsTimeline(symbol=symbol, quarters=tuple(quarters))


def _upcoming_quarters(
    reported: list[dict], future_dates: list[date], eps_estimate, revenue_estimate
) -> list[QuarterlyEarnings]:
    """The next one or two upcoming quarters, from Yahoo's ``0q`` / ``+1q`` forward estimate
    rows (EPS + revenue) — the reliable source of *two* forward quarters, unlike
    ``earnings_dates``, which often lists only the single next scheduled date.

    ``0q`` is the quarter after the most recently reported one (yfinance's "current
    quarter"), which anchors the pair; if nothing has been reported yet, the nearest
    scheduled date's quarter is used instead. A scheduled ``earnings_dates`` date is attached
    as the report date when one lines up with the quarter's period. A quarter is emitted only
    when Yahoo actually has an estimate (or a date) for it, so the result is at most two and
    may be one or none.
    """
    if reported:
        q0_end = _next_quarter_end(_period_end_before(reported[0]["report_date"]))
    elif future_dates:
        q0_end = _period_end_before(future_dates[0])
    else:
        return []  # nothing to anchor the forward quarters on

    # Match any scheduled future dates to a quarter by period end (not by order).
    date_by_period: dict[date, date] = {}
    for announced in future_dates:
        date_by_period.setdefault(_period_end_before(announced), announced)

    plan = ((_FORWARD_LABELS[0], q0_end), (_FORWARD_LABELS[1], _next_quarter_end(q0_end)))
    out: list[QuarterlyEarnings] = []
    for label, period_end in plan:
        eps = _cell(eps_estimate, label, "avg")
        revenue = _cell(revenue_estimate, label, "avg")
        report_date = date_by_period.get(period_end)
        if eps is None and revenue is None and report_date is None:
            continue  # Yahoo has nothing for this quarter — don't invent it
        out.append(_build_upcoming(period_end, report_date, eps, revenue))
    return out


def _build_reported(
    row: dict, revenue_by_period_end: dict[date, float]
) -> QuarterlyEarnings:
    """One reported quarter from an ``earnings_dates`` row: the reported EPS against the
    estimate that preceded it (surprise computed here, not read from Yahoo's own
    ``Surprise(%)`` column), plus the reported revenue matched from the income statement."""
    report_date: date = row["report_date"]
    period_end = _period_end_before(report_date)
    fiscal_year = period_end.year
    fiscal_quarter = _quarter_of(period_end)
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
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        period_end=period_end,
        report_date=report_date,
        eps_actual=eps_actual,
        eps_estimate=eps_estimate,
        eps_surprise=surprise,
        eps_surprise_percent=surprise_percent,
        revenue_estimate=None,
        revenue_actual=_revenue_for(report_date, revenue_by_period_end),
    )


def _build_upcoming(
    period_end: date, report_date: date | None, eps_estimate: float | None, revenue_estimate: float | None
) -> QuarterlyEarnings:
    """One upcoming quarter from the forward estimates: a consensus EPS and revenue, no
    actual yet. ``report_date`` is set only when Yahoo has scheduled the announcement."""
    return QuarterlyEarnings(
        fiscal_year=period_end.year,
        fiscal_quarter=_quarter_of(period_end),
        period_end=period_end,
        report_date=report_date,
        eps_actual=None,
        eps_estimate=eps_estimate,
        eps_surprise=None,
        eps_surprise_percent=None,
        revenue_estimate=revenue_estimate,
    )


def _quarter_of(period_end: date) -> int:
    """The calendar quarter (1–4) a quarter-end date falls in."""
    return (period_end.month - 1) // 3 + 1


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


def _next_quarter_end(period_end: date) -> date:
    """The calendar quarter-end one quarter after ``period_end`` (Dec 31 wraps to Mar 31)."""
    ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    quarter = _quarter_of(period_end)
    if quarter == 4:
        return date(period_end.year + 1, 3, 31)
    month, day = ends[quarter + 1]
    return date(period_end.year, month, day)


def _revenue_actuals(frame) -> dict[date, float]:
    """``quarterly_income_stmt`` → reported ``Total Revenue`` keyed by the column's *true*
    fiscal period-end date, matched to the reported quarters by announcement-date proximity
    (``_revenue_for``). Never keyed by the calendar fiscal label: for an off-calendar filer
    the label names a different fiscal quarter than the one the EPS was reported for, so a
    label match would pair one quarter's EPS with another quarter's revenue."""
    out: dict[date, float] = {}
    if frame is None or getattr(frame, "empty", True):
        return out
    try:
        if "Total Revenue" not in frame.index:
            return out
        series = frame.loc["Total Revenue"]
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return out
    for period_end, value in series.items():
        day = _to_date(period_end)
        revenue = _num(value)
        if day is None or revenue is None:
            continue
        out[day] = revenue
    return out


def _revenue_for(report_date: date, revenue_by_period_end: dict[date, float]) -> float | None:
    """The reported revenue for the quarter announced on ``report_date``: the income
    statement's most recent period end preceding the announcement — earnings are announced
    weeks after the quarter closes, so that column is the quarter being reported. If the
    nearest preceding column is older than a plausible announcement lag, the statement
    doesn't carry this quarter yet; return nothing rather than the previous quarter's
    revenue."""
    preceding = [end for end in revenue_by_period_end if end < report_date]
    if not preceding:
        return None
    period_end = max(preceding)
    if (report_date - period_end).days > _MAX_REPORT_LAG_DAYS:
        return None
    return revenue_by_period_end[period_end]


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
