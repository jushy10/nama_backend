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
  reported year when ``info`` is unavailable.
- ``Ticker.get_earnings_dates`` — the announcement history, fetched deeper than the quarterly
  adapter needs it (several years back). A reported year's ``eps_actual_consensus`` is the sum
  of its four quarterly *Reported EPS* values — the analyst-consensus (adjusted) basis, the
  same basis the forward ``eps_estimate`` is quoted on, unlike the GAAP-diluted ``eps_actual``
  from the income statement. Quarters are assigned to a fiscal year by their derived calendar
  quarter-end (the most recent quarter-end before the announcement, the quarterly adapter's
  convention) falling within the year ending at the fiscal-year-end; any 1-year window holds
  exactly four calendar quarter-ends, so a year is summed only when all four slots carry a
  reported EPS — anything else (thin history, semi-annual reporters, restatement noise) yields
  ``None`` rather than a wrong sum. Best-effort enrichment: a failure fetching the history
  drops the consensus actuals but never sinks the timeline.

There is deliberately **no annual surprise/beat**: Yahoo's estimate-vs-actual history is
per-quarter (``earnings_history``), so there is no historical annual estimate to compare a
reported year against. A reported year carries an actual with no estimate. A reported column
with no usable EPS at all is skipped, so ``eps_actual is None`` stays a sound reported-vs-
upcoming discriminator.

Fiscal alignment is best-effort and more exact than the quarterly adapter's: ``income_stmt``
reports the true fiscal-year-end date, so a reported year's ``fiscal_year`` is that date's
year (exact for calendar fiscal years; a label offset for a few off-calendar names, e.g. a
company whose fiscal year ends in January).

Currency: a foreign ADR (TSM, TM, BABA, …) reports in one currency but trades in another,
and Yahoo returns these surfaces in a mix — ``income_stmt`` (all rows) and ``revenue_estimate``
reliably in the *reporting* currency, but the *market* EPS surfaces (``earnings_dates``, hence
the consensus actual, and ``earnings_estimate``) in either currency depending on the issuer
(USD for TSM, CNY for BABA). A shared ``yfinance_currency`` normalizer (built from ``info``'s
``financialCurrency`` vs ``currency``, with the market-EPS currency detected once from the
``0y`` estimate against ``info['forwardEps']``) converts everything onto the trading currency
so the whole timeline reads in one currency; it's the identity for a domestic issuer, so this
path is untouched for US names.

This is the only module that knows ``yfinance``/Yahoo exists; swap it and nothing else
changes. It is deliberately defensive — Yahoo is an unofficial, best-effort feed that
reshapes payloads without notice and rate-limits data-centre IPs — so any failure on the
primary (estimate) surfaces becomes ``StockDataUnavailable`` and a symbol Yahoo doesn't cover
yields an empty timeline rather than an error. Behind the persistent DB cache, a blocked live
call just serves the stored rows. Every Yahoo read is routed through ``yfinance_session``
(pacing + a one-shot fresh-crumb retry on a 401); ``income_stmt`` — the hardest-gated
endpoint — additionally retries on an *empty* result, since a swallowed crumb 401 surfaces
that way.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timezone

import pandas as pd
import yfinance as yf

from app.stocks.adapters import yfinance_currency, yfinance_session
from app.stocks.adapters.yfinance_currency import CurrencyNormalizer
from app.stocks.earnings.annual.entities import AnnualEarnings, AnnualEarningsTimeline
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.exceptions import StockDataUnavailable

# yfinance's relative period labels on the annual estimate frames.
_FY1 = "0y"  # the current, in-progress fiscal year
_FY2 = "+1y"  # the fiscal year after it

# Announcement-history rows to request from ``get_earnings_dates``: ~4 scheduled future rows
# plus ~24 past ones (~6 years of quarters), enough to sum a consensus-basis annual EPS for
# each of the _PAST reported years (the oldest needs announcements ~5 years back).
_EARNINGS_DATES_LIMIT = 28


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
            # Routed through yfinance_session for pacing + a fresh-crumb retry on a raised 401.
            # No is_empty here: the forward estimate frames are legitimately empty for a stock
            # with no analyst coverage, so retrying on empty would just double those calls.
            eps_estimate = yfinance_session.call(lambda: ticker.earnings_estimate)
            revenue_estimate = yfinance_session.call(lambda: ticker.revenue_estimate)
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
        info = yfinance_currency.read_info(ticker)
        normalizer = yfinance_currency.build(
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
            income_stmt = yfinance_session.call(
                lambda: ticker.income_stmt, is_empty=yfinance_session.frame_is_empty
            )
        except Exception:  # noqa: BLE001 — enrichment: degrade to no reported years
            income_stmt = None

        # The income statement is in the reporting currency (TWD for TSM); normalize its
        # figures onto the trading currency (USD) — a no-op for a domestic issuer.
        reported = _reported_years(income_stmt, normalizer)  # newest first, uncapped

        # The consensus-basis annual actuals (the sum of each fiscal year's four quarterly
        # "Reported EPS" values) need the announcement history. Best-effort enrichment on the
        # reported years: a failure fetching it drops the consensus figures, nothing else.
        earnings_dates = None
        if reported:
            try:
                earnings_dates = yfinance_session.call(
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
            # issuer (BABA), left alone for a trading-currency one (TSM).
            years.append(
                replace(
                    year,
                    eps_actual_consensus=normalizer.market_to_trading(
                        consensus.get(year.period_end)
                    ),
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
    """``income_stmt`` → the reported fiscal years, newest first.

    Reads ``Diluted EPS`` (falling back to ``Basic EPS``), ``Total Revenue``, and
    ``Net Income`` per fiscal-year-end column. A column with no usable EPS is skipped —
    without a reported EPS the year couldn't be told apart from an upcoming one (the
    ``eps_actual is None`` discriminator), and this feature is EPS-centric.

    The income statement is always in the issuer's reporting currency, so every figure is
    put through ``normalizer.to_trading`` (a no-op for a domestic issuer, an FX conversion
    for a foreign ADR). The EPS discriminator is applied on the raw value *before*
    conversion, so a converted figure that stays non-``None`` keeps the year reported."""
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


def _upcoming_years(
    info: dict,
    reported: list[AnnualEarnings],
    eps_estimate,
    revenue_estimate,
    normalizer: CurrencyNormalizer,
) -> list[AnnualEarnings]:
    """The next one or two fiscal years, from Yahoo's ``0y`` / ``+1y`` forward estimate rows
    (EPS + revenue) — the reliable source of forward annual consensus.

    ``0y`` is the current, in-progress fiscal year; ``+1y`` the one after. They're labelled
    by the fiscal-year-end from ``info['nextFiscalYearEnd']`` (``+1y`` a year on), falling
    back to one year past the latest reported year when ``info`` is unavailable. A year is
    emitted only when Yahoo actually has an estimate for it, so the result is at most two and
    may be one or none.

    Currency: ``revenue_estimate`` is reliably reporting-currency (converted outright); the
    EPS estimate is a market-EPS figure on the detected market-EPS currency — both no-ops for
    a domestic issuer."""
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
    """The fiscal-year-end that labels ``0y`` (the current, in-progress year).

    Primary source is ``info['nextFiscalYearEnd']`` (``info`` already fetched by the
    caller); falls back to one year past the latest reported year's end when ``info`` lacks
    it (e.g. ``income_stmt`` reachable but ``info`` not). ``None`` when neither is available,
    in which case the forward years are omitted."""
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
    """Fiscal-year-end → the year's actual EPS on the analyst-consensus basis.

    The sum of the four quarterly "Reported EPS" values (``earnings_dates``) belonging to
    the fiscal year — the adjusted basis analysts estimate against, so it's comparable with
    the forward ``eps_estimate`` in a way the GAAP-diluted ``eps_actual`` isn't. A quarter
    belongs to the year when its derived calendar quarter-end falls in the year ending at
    the (true, income-statement) fiscal-year-end; any 1-year window holds exactly four
    calendar quarter-ends, so a year is emitted only when all four slots carry a reported
    EPS. Fewer means the history ran out (or a non-quarterly reporter) — better no figure
    than a wrong one."""
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
    """``earnings_dates`` → one reported (consensus-basis) EPS per derived calendar
    quarter-end, the most recent announcement winning a duplicate (a restatement).

    The derivation mirrors the quarterly adapter: earnings are announced weeks after the
    quarter closes, so the quarter being reported is the calendar quarter that most recently
    ended before the announcement. Rows without a reported EPS (scheduled future
    announcements) are dropped."""
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
    """The most recent calendar quarter-end strictly before an announcement date (the
    quarterly adapter's fiscal-alignment convention, mirrored here — adapters don't import
    each other)."""
    ends = [
        date(report.year, 3, 31),
        date(report.year, 6, 30),
        date(report.year, 9, 30),
        date(report.year, 12, 31),
        date(report.year - 1, 12, 31),
    ]
    return max(end for end in ends if end < report)


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


def _minus_one_year(day: date) -> date:
    """One year back from a fiscal-year-end date; a Feb-29 end clamps to Feb-28."""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)
