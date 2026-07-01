"""Interface Adapter: per-year (annual) earnings from Yahoo Finance (via ``yfinance``).

Builds a stock's annual earnings timeline — the recent reported fiscal years and the
upcoming (estimated) ones — from three yfinance surfaces, mirroring the quarterly adapter
one scale up:

- ``Ticker.income_stmt`` — the annual income statement, one column per fiscal-year-end. Its
  ``Diluted EPS`` (falling back to ``Basic EPS``), ``Total Revenue``, and ``Net Income`` rows
  are the **reported** years; we keep the most recent ``_PAST`` of them. This is the
  fundamentals endpoint Yahoo gates hardest from data-centre IPs, so it's treated as
  best-effort: a failure fetching it drops the reported years but never sinks the timeline —
  the forward years still serve. (That's the deliberate trade-off in the pure-yfinance
  design: on ECS the reported half may be empty until sourced from a non-IP-gated feed.)
- ``Ticker.earnings_estimate`` / ``Ticker.revenue_estimate`` — period-indexed frames whose
  ``0y`` (current, in-progress fiscal year) and ``+1y`` (the one after) rows carry the
  forward consensus EPS and revenue. These are the source of the **upcoming** years: Yahoo
  publishes structured annual estimates only two years out, so this yields *at most two*.
  This endpoint is *not* IP-gated, so the forward half serves from ECS even when
  ``income_stmt`` is blocked — which is why the estimate frames, not the income statement,
  are the raising "primary" here.
- ``Ticker.info['nextFiscalYearEnd']`` — the fiscal-year-end that labels ``0y`` (the estimate
  frames carry no dates); ``+1y`` is a year on. Falls back to one year past the latest
  reported year when ``info`` is unavailable. Mirrors the estimates adapter's anchor.

There is deliberately **no annual surprise/beat**: Yahoo's estimate-vs-actual history is
per-quarter (``earnings_history``), so there is no historical annual estimate to compare a
reported year against. A reported year carries an actual with no estimate. A reported column
with no usable EPS at all is skipped, so ``eps_actual is None`` stays a sound reported-vs-
upcoming discriminator.

Fiscal alignment is best-effort and more exact than the quarterly adapter's: ``income_stmt``
reports the true fiscal-year-end date, so a reported year's ``fiscal_year`` is that date's
year (exact for calendar fiscal years; a label offset for a few off-calendar names, e.g. a
company whose fiscal year ends in January).

This is the only module that knows ``yfinance``/Yahoo exists; swap it and nothing else
changes. It is deliberately defensive — Yahoo is an unofficial, best-effort feed that
reshapes payloads without notice and rate-limits data-centre IPs — so any failure on the
primary (estimate) surfaces becomes ``StockDataUnavailable`` and a symbol Yahoo doesn't cover
yields an empty timeline rather than an error. Behind the persistent DB cache, a blocked live
call just serves the stored rows.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pandas as pd
import yfinance as yf

from app.stocks.earnings.annual.entities import AnnualEarnings, AnnualEarningsTimeline
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.exceptions import StockDataUnavailable

# yfinance's relative period labels on the annual estimate frames.
_FY1 = "0y"  # the current, in-progress fiscal year
_FY2 = "+1y"  # the fiscal year after it


class YfinanceAnnualEarningsProvider(AnnualEarningsProvider):
    """Fetches a stock's recent and upcoming annual earnings from Yahoo (no API key)."""

    _PAST = 4  # most recent reported fiscal years to keep (upcoming is capped at two: 0y, +1y)

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to
        # the real yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        try:
            ticker = self._ticker_factory(symbol)
            eps_estimate = ticker.earnings_estimate
            revenue_estimate = ticker.revenue_estimate
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance annual earnings failed ({exc})"
            ) from exc

        # Reported years come from the annual income statement — the fundamentals endpoint
        # Yahoo gates hardest from data-centre IPs. Best-effort: a failure drops the reported
        # years but leaves the (forward) timeline intact, so prod serves the estimates even
        # when this is blocked.
        try:
            income_stmt = ticker.income_stmt
        except Exception:  # noqa: BLE001 — enrichment: degrade to no reported years
            income_stmt = None

        reported = _reported_years(income_stmt)  # newest first, uncapped

        seen: set[int] = set()
        years: list[AnnualEarnings] = []

        # Past: keep the most recent reported years — walk newest-first and cap at _PAST
        # (the whole timeline is re-sorted chronologically before returning).
        for year in reported:
            if len(years) >= self._PAST:
                break
            if year.fiscal_year in seen:  # a restated/duplicate fiscal-year column
                continue
            seen.add(year.fiscal_year)
            years.append(year)

        # Upcoming: the 0y/+1y forward estimates (at most two).
        for year in _upcoming_years(ticker, reported, eps_estimate, revenue_estimate):
            if year.fiscal_year in seen:
                continue
            seen.add(year.fiscal_year)
            years.append(year)

        # Emit the timeline chronologically: ascending by fiscal_year, so the oldest reported
        # year leads through to the furthest upcoming one.
        years.sort(key=lambda y: y.fiscal_year)
        return AnnualEarningsTimeline(symbol=symbol, years=tuple(years))


def _reported_years(frame) -> list[AnnualEarnings]:
    """``income_stmt`` → the reported fiscal years, newest first.

    Reads ``Diluted EPS`` (falling back to ``Basic EPS``), ``Total Revenue``, and
    ``Net Income`` per fiscal-year-end column. A column with no usable EPS is skipped —
    without a reported EPS the year couldn't be told apart from an upcoming one (the
    ``eps_actual is None`` discriminator), and this feature is EPS-centric."""
    if frame is None or getattr(frame, "empty", True):
        return []
    eps_row = _row(frame, "Diluted EPS")
    if eps_row is None:
        eps_row = _row(frame, "Basic EPS")
    revenue_row = _row(frame, "Total Revenue")
    net_income_row = _row(frame, "Net Income")

    try:
        periods = list(frame.columns)
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return []

    out: list[AnnualEarnings] = []
    for period in periods:
        period_end = _to_date(period)
        if period_end is None:
            continue
        eps = _cell_at(eps_row, period)
        if eps is None:
            continue  # no reported EPS ⇒ can't distinguish from an upcoming year; skip
        out.append(
            AnnualEarnings(
                fiscal_year=period_end.year,
                period_end=period_end,
                eps_actual=eps,
                eps_estimate=None,
                revenue_actual=_cell_at(revenue_row, period),
                revenue_estimate=None,
                net_income=_cell_at(net_income_row, period),
            )
        )
    out.sort(key=lambda y: y.period_end or date.min, reverse=True)  # newest first
    return out


def _upcoming_years(
    ticker, reported: list[AnnualEarnings], eps_estimate, revenue_estimate
) -> list[AnnualEarnings]:
    """The next one or two fiscal years, from Yahoo's ``0y`` / ``+1y`` forward estimate rows
    (EPS + revenue) — the reliable source of forward annual consensus.

    ``0y`` is the current, in-progress fiscal year; ``+1y`` the one after. They're labelled
    by the fiscal-year-end from ``info['nextFiscalYearEnd']`` (``+1y`` a year on), falling
    back to one year past the latest reported year when ``info`` is unavailable. A year is
    emitted only when Yahoo actually has an estimate for it, so the result is at most two and
    may be one or none."""
    fy1_end = _fiscal_year1_end(ticker, reported)
    if fy1_end is None:
        return []  # nothing to anchor/label the forward years on
    fy2_end = _add_one_year(fy1_end)

    plan = ((_FY1, fy1_end), (_FY2, fy2_end))
    out: list[AnnualEarnings] = []
    for label, period_end in plan:
        eps = _cell(eps_estimate, label, "avg")
        revenue = _cell(revenue_estimate, label, "avg")
        if eps is None and revenue is None:
            continue  # Yahoo has nothing for this year — don't invent it
        out.append(
            AnnualEarnings(
                fiscal_year=period_end.year,
                period_end=period_end,
                eps_actual=None,
                eps_estimate=eps,
                revenue_actual=None,
                revenue_estimate=revenue,
            )
        )
    return out


def _fiscal_year1_end(ticker, reported: list[AnnualEarnings]) -> date | None:
    """The fiscal-year-end that labels ``0y`` (the current, in-progress year).

    Primary source is ``Ticker.info['nextFiscalYearEnd']`` (mirroring the estimates
    adapter); falls back to one year past the latest reported year's end when ``info`` is
    unavailable (e.g. ``income_stmt`` reachable but ``info`` not). ``None`` when neither is
    available, in which case the forward years are omitted."""
    try:
        info = ticker.info or {}
        stamp = info.get("nextFiscalYearEnd")
    except Exception:  # noqa: BLE001 — info is optional; a bad `info` mustn't sink the estimates
        stamp = None
    fy1_end = _epoch_to_date(stamp)
    if fy1_end is not None:
        return fy1_end
    if reported and reported[0].period_end is not None:  # reported is newest-first
        return _add_one_year(reported[0].period_end)
    return None


def _row(frame, name: str):
    """One labelled row of the income statement as a Series, or ``None`` (missing)."""
    try:
        if name not in frame.index:
            return None
        return frame.loc[name]
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return None


def _cell_at(row, period) -> float | None:
    """One period's value from an income-statement row Series → float, or ``None``."""
    if row is None:
        return None
    try:
        return _num(row.get(period))
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _cell(frame, period: str, column: str) -> float | None:
    """One numeric cell of a period-indexed estimate frame as a float, or ``None`` (missing
    row/column, NaN, or non-numeric)."""
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
    """A pandas ``Timestamp`` / ``datetime`` (an ``income_stmt`` column) → a ``date``;
    ``None`` for ``NaT`` or an unrecognized value."""
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


def _epoch_to_date(stamp) -> date | None:
    """A Unix timestamp (yfinance reports fiscal dates as epoch seconds) → a UTC date."""
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _add_one_year(day: date) -> date:
    """One year on from a fiscal-year-end date; a Feb-29 end clamps to Feb-28."""
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)
