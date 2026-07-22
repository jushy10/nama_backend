from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from app.adapters.yfinance import currency, session
from app.adapters.yfinance.currency import CurrencyNormalizer
from app.domains.financials.earnings.quarterly.entities import (
    EarningsSession,
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.domains.shared.exceptions import StockDataUnavailable

# The two forward quarters Yahoo publishes structured estimates for, in order: the current
# quarter (in progress, next to report) and the one after it.
_FORWARD_LABELS = ("0q", "+1q")

# The longest plausible gap between a fiscal quarter's end and its earnings announcement.
# Announcements land ~3–8 weeks after the close (the 10-Q deadline is 40–45 days; year-end
# reports stretch longer), while the *previous* quarter's end sits a further ~13 weeks back —
# ≥ ~115 days before the announcement — so this cap separates "the quarter being announced"
# from "the income statement hasn't published this quarter yet".
_MAX_REPORT_LAG_DAYS = 90

# Earnings times on the ``earnings_dates`` index are quoted against the US exchange session,
# so the before-open / after-close split (EarningsSession) is read on Eastern wall-clock time.
_EASTERN = ZoneInfo("America/New_York")


class QuarterlyEarningsAdapterImpl(QuarterlyEarningsAdapter):
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
            dates = session.call(
                lambda: ticker.earnings_dates, is_empty=session.frame_is_empty
            )
            eps_estimate = session.call(lambda: ticker.earnings_estimate)
            revenue_estimate = session.call(lambda: ticker.revenue_estimate)
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance quarterly earnings failed ({exc})"
            ) from exc

        # info tells us the issuer's reporting vs trading currency, so a foreign ADR's
        # figures can be normalized onto one currency. Best-effort — a blocked info degrades
        # to an identity normalizer, never sinking the timeline. The market-EPS currency
        # (earnings_dates + earnings_estimate) is detected from the forward annual estimate
        # (0y — present on the same estimate frame) against info's trading-currency forwardEps.
        info = currency.read_info(ticker)
        normalizer = currency.build(
            self._ticker_factory,
            info,
            market_eps_sample=_cell(eps_estimate, "0y", "avg"),
            market_eps_reference=_num(info.get("forwardEps")),
        )

        # Reported revenue is best-effort enrichment on the past quarters — a failure
        # fetching the income statement must not sink the (primary) earnings timeline. Like
        # the annual fundamentals endpoint, an empty result is a swallowed crumb 401, so
        # retry once with a fresh crumb.
        try:
            income_stmt = session.call(
                lambda: ticker.quarterly_income_stmt,
                is_empty=session.frame_is_empty,
            )
        except Exception:  # noqa: BLE001 — enrichment: degrade to no revenue_actual
            income_stmt = None
        # The income statement is in the reporting currency; normalize revenue onto the
        # trading currency (a no-op for a domestic issuer).
        revenue_actuals = _revenue_actuals(income_stmt, normalizer)

        rows = _parse_rows(dates)
        reported = sorted(
            (r for r in rows if r["eps_actual"] is not None),
            key=lambda r: r["report_date"],
            reverse=True,  # newest first
        )
        future_rows = sorted(
            (r for r in rows if r["eps_actual"] is None),
            key=lambda r: r["report_date"],
        )

        seen: set[tuple[int, int]] = set()
        quarters: list[QuarterlyEarnings] = []

        # Past: keep the most recent reported quarters — walk newest-first and cap at
        # _PAST (the whole timeline is re-sorted chronologically before returning).
        for row in reported:
            if len(quarters) >= self._PAST:
                break
            quarter = _build_reported(row, revenue_actuals, normalizer)
            key = (quarter.fiscal_year, quarter.fiscal_quarter)
            if key in seen:  # a restated/duplicate announcement in the same quarter
                continue
            seen.add(key)
            quarters.append(quarter)

        # Upcoming: the 0q/+1q forward estimates (at most two), so a stock with a single
        # scheduled future date still surfaces both quarters Yahoo estimates.
        for quarter in _upcoming_quarters(
            reported, future_rows, eps_estimate, revenue_estimate, normalizer
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
    reported: list[dict],
    future_rows: list[dict],
    eps_estimate,
    revenue_estimate,
    normalizer: CurrencyNormalizer,
) -> list[QuarterlyEarnings]:
    future_dates = [r["report_date"] for r in future_rows]
    if reported:
        q0_end = _next_quarter_end(_period_end_before(reported[0]["report_date"]))
    elif future_dates:
        q0_end = _period_end_before(future_dates[0])
    else:
        return []  # nothing to anchor the forward quarters on

    # Match any scheduled future rows to a quarter by period end (not by order), carrying
    # the announcement's session alongside its date.
    date_by_period: dict[date, date] = {}
    session_by_period: dict[date, EarningsSession] = {}
    for row in future_rows:
        period = _period_end_before(row["report_date"])
        if period not in date_by_period:
            date_by_period[period] = row["report_date"]
            session_by_period[period] = row["report_session"]

    plan = ((_FORWARD_LABELS[0], q0_end), (_FORWARD_LABELS[1], _next_quarter_end(q0_end)))
    out: list[QuarterlyEarnings] = []
    for label, period_end in plan:
        eps = _cell(eps_estimate, label, "avg")
        revenue = _cell(revenue_estimate, label, "avg")
        report_date = date_by_period.get(period_end)
        if eps is None and revenue is None and report_date is None:
            continue  # Yahoo has nothing for this quarter — don't invent it
        eps = normalizer.market_to_trading(eps)
        revenue = normalizer.to_trading(revenue)
        session = session_by_period.get(period_end, EarningsSession.UNKNOWN)
        out.append(_build_upcoming(period_end, report_date, eps, revenue, session))
    return out


def _build_reported(
    row: dict,
    revenue_by_period_end: dict[date, float],
    normalizer: CurrencyNormalizer,
) -> QuarterlyEarnings:
    report_date: date = row["report_date"]
    period_end = _period_end_before(report_date)
    fiscal_year = period_end.year
    fiscal_quarter = _quarter_of(period_end)
    eps_actual = normalizer.market_to_trading(row["eps_actual"])
    eps_estimate = normalizer.market_to_trading(row["eps_estimate"])

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
        report_session=row["report_session"],
    )


def _build_upcoming(
    period_end: date,
    report_date: date | None,
    eps_estimate: float | None,
    revenue_estimate: float | None,
    report_session: EarningsSession = EarningsSession.UNKNOWN,
) -> QuarterlyEarnings:
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
        report_session=report_session,
    )


def _quarter_of(period_end: date) -> int:
    return (period_end.month - 1) // 3 + 1


def _period_end_before(report: date) -> date:
    ends = [
        date(report.year, 3, 31),
        date(report.year, 6, 30),
        date(report.year, 9, 30),
        date(report.year, 12, 31),
        date(report.year - 1, 12, 31),
    ]
    return max(end for end in ends if end < report)


def _next_quarter_end(period_end: date) -> date:
    ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    quarter = _quarter_of(period_end)
    if quarter == 4:
        return date(period_end.year + 1, 3, 31)
    month, day = ends[quarter + 1]
    return date(period_end.year, month, day)


def _revenue_actuals(frame, normalizer: CurrencyNormalizer) -> dict[date, float]:
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
        out[day] = normalizer.to_trading(revenue)
    return out


def _revenue_for(report_date: date, revenue_by_period_end: dict[date, float]) -> float | None:
    preceding = [end for end in revenue_by_period_end if end < report_date]
    if not preceding:
        return None
    period_end = max(preceding)
    if (report_date - period_end).days > _MAX_REPORT_LAG_DAYS:
        return None
    return revenue_by_period_end[period_end]


def _parse_rows(frame) -> list[dict]:
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
                "report_session": _to_session(index),
                "eps_estimate": _num(_series_get(series, "EPS Estimate")),
                "eps_actual": _num(_series_get(series, "Reported EPS")),
            }
        )
    return rows


def _series_get(series, key: str):
    try:
        return series.get(key)
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _cell(frame, period: str, column: str) -> float | None:
    try:
        if frame is None or getattr(frame, "empty", True):
            return None
        if period not in frame.index or column not in frame.columns:
            return None
        return _num(frame.loc[period, column])
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return None


def _num(value) -> float | None:
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


def _to_session(value) -> EarningsSession:
    try:
        if pd.isna(value):
            return EarningsSession.UNKNOWN
    except (TypeError, ValueError):
        pass
    try:
        if getattr(value, "tzinfo", None) is not None:
            # pandas Timestamp uses tz_convert; a plain datetime uses astimezone.
            value = (
                value.tz_convert(_EASTERN)
                if hasattr(value, "tz_convert")
                else value.astimezone(_EASTERN)
            )
        local_time = value.time()
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return EarningsSession.UNKNOWN
    return EarningsSession.from_local_time(local_time)


def _to_date(value) -> date | None:
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
