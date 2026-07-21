from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timezone

import pandas as pd
import yfinance as yf

from app.stocks.adapters.yfinance import currency, session
from app.stocks.adapters.yfinance.currency import CurrencyNormalizer
from app.stocks.company.earnings.annual.entities import AnnualEarnings, AnnualEarningsTimeline
from app.stocks.company.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.exceptions import StockDataUnavailable

# yfinance's relative period labels on the annual estimate frames.
_FY1 = "0y"  # the current, in-progress fiscal year
_FY2 = "+1y"  # the fiscal year after it

# Announcement-history rows to request from ``get_earnings_dates``: ~4 scheduled future rows
# plus ~24 past ones (~6 years of quarters), enough to sum a consensus-basis annual EPS for
# each of the _PAST reported years (the oldest needs announcements ~5 years back).
_EARNINGS_DATES_LIMIT = 28


class YfinanceAnnualEarningsProvider(AnnualEarningsProvider):
    _PAST = 4  # most recent reported fiscal years to keep (upcoming is capped at two: 0y, +1y)

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to
        # the real yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        try:
            ticker = self._ticker_factory(symbol)
            # Routed through the shared session module for pacing + a fresh-crumb retry on a raised 401.
            # No is_empty here: the forward estimate frames are legitimately empty for a stock
            # with no analyst coverage, so retrying on empty would just double those calls.
            eps_estimate = session.call(lambda: ticker.earnings_estimate)
            revenue_estimate = session.call(lambda: ticker.revenue_estimate)
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance annual earnings failed ({exc})"
            ) from exc

        # info drives two things: it labels the forward years (nextFiscalYearEnd) and it
        # tells us the issuer's reporting vs trading currency, so a foreign ADR's figures can
        # be normalized onto one currency. Best-effort — a blocked info degrades to {} (an
        # identity normalizer + the reported-year fallback label), never sinking the timeline.
        # The market-EPS currency (earnings_dates + earnings_estimate) is detected from the
        # forward annual estimate (0y) against info's trading-currency forwardEps.
        info = currency.read_info(ticker)
        normalizer = currency.build(
            self._ticker_factory,
            info,
            market_eps_sample=_cell(eps_estimate, _FY1, "avg"),
            market_eps_reference=_num(info.get("forwardEps")),
        )

        # Reported years come from the annual income statement — the fundamentals endpoint
        # Yahoo gates hardest from data-centre IPs. Best-effort: a failure drops the reported
        # years but leaves the (forward) timeline intact, so prod serves the estimates even
        # when this is blocked. An *empty* income_stmt is how a swallowed crumb 401 surfaces
        # (a real company always has one), so retry once with a fresh crumb.
        try:
            income_stmt = session.call(
                lambda: ticker.income_stmt, is_empty=session.frame_is_empty
            )
        except Exception:  # noqa: BLE001 — enrichment: degrade to no reported years
            income_stmt = None

        # The income statement is in the reporting currency (TWD for TSM); normalize its
        # figures onto the trading currency (USD) — a no-op for a domestic issuer.
        reported = _reported_years(income_stmt, normalizer)  # newest first, uncapped

        # Cash-flow per share (free + operating) is best-effort enrichment on the reported
        # years, from the annual cash-flow statement — the same hard-gated fundamentals class
        # as income_stmt. A blocked fetch just drops the per-share cash figures (the sync
        # carries the stored ones forward). Only worth fetching when there are reported years
        # to attach it to (and it needs income_stmt's share counts, so income_stmt must have
        # come through anyway).
        cash_per_share: dict[date, tuple[float | None, float | None]] = {}
        if reported:
            try:
                cashflow = session.call(
                    lambda: ticker.cashflow, is_empty=session.frame_is_empty
                )
            except Exception:  # noqa: BLE001 — enrichment: degrade to no cash-flow figures
                cashflow = None
            cash_per_share = _cash_flow_per_share(cashflow, income_stmt, normalizer)

        # The consensus-basis annual actuals (the sum of each fiscal year's four quarterly
        # "Reported EPS" values) need the announcement history. Best-effort enrichment on the
        # reported years: a failure fetching it drops the consensus figures, nothing else.
        earnings_dates = None
        if reported:
            try:
                earnings_dates = session.call(
                    lambda: ticker.get_earnings_dates(limit=_EARNINGS_DATES_LIMIT)
                )
            except Exception:  # noqa: BLE001 — enrichment: degrade to no consensus actuals
                earnings_dates = None
        consensus = _consensus_eps_actuals(earnings_dates, reported)

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
            # The consensus actual is a market-EPS figure (summed from earnings_dates), so it
            # rides the detected market-EPS currency — converted for a reporting-currency
            # issuer (BABA), left alone for a trading-currency one (TSM). The per-share cash
            # figures are already trading-currency (normalized in _cash_flow_per_share).
            cash = cash_per_share.get(year.period_end)
            years.append(
                replace(
                    year,
                    eps_actual_consensus=normalizer.market_to_trading(
                        consensus.get(year.period_end)
                    ),
                    fcf_per_share=cash[0] if cash else None,
                    ocf_per_share=cash[1] if cash else None,
                )
            )

        # Upcoming: the 0y/+1y forward estimates (at most two). revenue_estimate is reliably
        # reporting-currency; the EPS estimate is a market-EPS figure on the detected
        # market-EPS currency — both handled by the normalizer.
        for year in _upcoming_years(
            info, reported, eps_estimate, revenue_estimate, normalizer
        ):
            if year.fiscal_year in seen:
                continue
            seen.add(year.fiscal_year)
            years.append(year)

        # Emit the timeline chronologically: ascending by fiscal_year, so the oldest reported
        # year leads through to the furthest upcoming one.
        years.sort(key=lambda y: y.fiscal_year)
        return AnnualEarningsTimeline(symbol=symbol, years=tuple(years))


def _reported_years(
    frame, normalizer: CurrencyNormalizer
) -> list[AnnualEarnings]:
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
                eps_actual=normalizer.to_trading(eps),
                eps_estimate=None,
                revenue_actual=normalizer.to_trading(_cell_at(revenue_row, period)),
                revenue_estimate=None,
                net_income=normalizer.to_trading(_cell_at(net_income_row, period)),
            )
        )
    out.sort(key=lambda y: y.period_end or date.min, reverse=True)  # newest first
    return out


def _cash_flow_per_share(
    cashflow_frame, income_frame, normalizer: CurrencyNormalizer
) -> dict[date, tuple[float | None, float | None]]:
    if cashflow_frame is None or getattr(cashflow_frame, "empty", True):
        return {}
    shares_by_end = _shares_by_period_end(income_frame)
    if not shares_by_end:
        return {}  # no share count anywhere ⇒ nothing to divide by
    fcf_row = _row(cashflow_frame, "Free Cash Flow")
    ocf_row = _row(cashflow_frame, "Operating Cash Flow")
    try:
        periods = list(cashflow_frame.columns)
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return {}
    out: dict[date, tuple[float | None, float | None]] = {}
    for period in periods:
        period_end = _to_date(period)
        if period_end is None:
            continue
        shares = shares_by_end.get(period_end)
        if shares is None or shares <= 0:
            continue
        fcf_total = _cell_at(fcf_row, period)
        ocf_total = _cell_at(ocf_row, period)
        fcf_ps = (
            normalizer.to_trading(fcf_total / shares) if fcf_total is not None else None
        )
        ocf_ps = (
            normalizer.to_trading(ocf_total / shares) if ocf_total is not None else None
        )
        if fcf_ps is None and ocf_ps is None:
            continue  # nothing to attach for this year
        out[period_end] = (fcf_ps, ocf_ps)
    return out


def _shares_by_period_end(frame) -> dict[date, float]:
    if frame is None or getattr(frame, "empty", True):
        return {}
    shares_row = _row(frame, "Diluted Average Shares")
    if shares_row is None:
        shares_row = _row(frame, "Basic Average Shares")
    if shares_row is None:
        return {}
    try:
        periods = list(frame.columns)
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return {}
    out: dict[date, float] = {}
    for period in periods:
        period_end = _to_date(period)
        if period_end is None:
            continue
        shares = _cell_at(shares_row, period)
        if shares is not None and shares > 0:
            out[period_end] = shares
    return out


def _upcoming_years(
    info: dict,
    reported: list[AnnualEarnings],
    eps_estimate,
    revenue_estimate,
    normalizer: CurrencyNormalizer,
) -> list[AnnualEarnings]:
    fy1_end = _fiscal_year1_end(info, reported)
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
                eps_estimate=normalizer.market_to_trading(eps),
                revenue_actual=None,
                revenue_estimate=normalizer.to_trading(revenue),
            )
        )
    return out


def _fiscal_year1_end(info: dict, reported: list[AnnualEarnings]) -> date | None:
    stamp = info.get("nextFiscalYearEnd") if isinstance(info, dict) else None
    fy1_end = _epoch_to_date(stamp)
    if fy1_end is not None:
        return fy1_end
    if reported and reported[0].period_end is not None:  # reported is newest-first
        return _add_one_year(reported[0].period_end)
    return None


def _consensus_eps_actuals(
    dates_frame, reported: list[AnnualEarnings]
) -> dict[date, float]:
    quarters = _quarterly_reported_eps(dates_frame)
    out: dict[date, float] = {}
    for year in reported:
        fy_end = year.period_end
        if fy_end is None:
            continue
        window_start = _minus_one_year(fy_end)
        eps_in_year = [
            eps for quarter_end, eps in quarters.items()
            if window_start < quarter_end <= fy_end
        ]
        if len(eps_in_year) == 4:
            out[fy_end] = round(sum(eps_in_year), 4)
    return out


def _quarterly_reported_eps(frame) -> dict[date, float]:
    if frame is None or getattr(frame, "empty", True):
        return {}
    try:
        pairs = list(frame.iterrows())
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return {}
    rows: list[tuple[date, float]] = []
    for index, series in pairs:
        report_date = _to_date(index)
        if report_date is None:
            continue
        try:
            eps = _num(series.get("Reported EPS"))
        except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
            eps = None
        if eps is None:
            continue
        rows.append((report_date, eps))
    rows.sort(reverse=True)  # newest announcement first, so it wins its quarter below
    out: dict[date, float] = {}
    for report_date, eps in rows:
        out.setdefault(_period_end_before(report_date), eps)
    return out


def _period_end_before(report: date) -> date:
    ends = [
        date(report.year, 3, 31),
        date(report.year, 6, 30),
        date(report.year, 9, 30),
        date(report.year, 12, 31),
        date(report.year - 1, 12, 31),
    ]
    return max(end for end in ends if end < report)


def _row(frame, name: str):
    try:
        if name not in frame.index:
            return None
        return frame.loc[name]
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return None


def _cell_at(row, period) -> float | None:
    if row is None:
        return None
    try:
        return _num(row.get(period))
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


def _epoch_to_date(stamp) -> date | None:
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _add_one_year(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)


def _minus_one_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)
