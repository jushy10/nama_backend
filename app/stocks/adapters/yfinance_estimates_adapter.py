"""Interface Adapter: forward analyst estimates from Yahoo Finance (via ``yfinance``).

Why Yahoo, not FMP: FMP's free tier gates forward estimates to a small symbol
allowlist — anything outside it returns a 402 ("this value set for 'symbol' is not
available under your current subscription"), so Micron, SanDisk, and most mid-caps
had no estimates at all. Yahoo's public consensus covers the broad US market with no
API key, so the endpoint can show a forward P/E for the whole universe instead of a
handful of names. It carries the current and next fiscal year's mean/low/high EPS,
mean revenue, and the analyst counts — exactly the FY1/FY2 pair a forward P/E and a
one-year forward-growth figure need.

Shape of the source: ``yfinance`` hands back pandas frames keyed by a *relative*
period label — ``0y`` is the current (in-progress) fiscal year, ``+1y`` the one after
— with no calendar date attached. The fiscal-year-end that labels FY1 comes separately
from ``Ticker.info`` (``nextFiscalYearEnd``); FY2 is a year later. That lets a stored
row be labelled by fiscal year the same way the FMP rows were.

This is the only module that knows ``yfinance``/Yahoo exists; swap it and nothing else
changes. It is deliberately defensive — Yahoo is an unofficial, best-effort feed that
reshapes payloads without notice and rate-limits data-centre IPs — so any vendor
failure becomes ``StockDataUnavailable`` and a symbol Yahoo doesn't cover yields an
empty (``is_empty``) estimate rather than an error.

Operational note: Yahoo blocks many cloud/data-centre IP ranges. Behind the persistent
DB cache this is tolerable — a blocked live refresh just serves the stored row — but on
a blocked host with a cold cache there will simply be no estimates until a reachable
host (or the cron job) fills them.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import yfinance as yf

from app.stocks.entities import AnalystEstimates, ForwardEstimate
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.estimates.ports import AnalystEstimatesProvider

# yfinance's relative period labels on the annual estimate frames.
_FY1 = "0y"  # the current, in-progress fiscal year
_FY2 = "+1y"  # the fiscal year after it

# An uncovered symbol (Yahoo has no forward estimate) yields this rather than an
# error — best-effort, the same contract the FMP adapter had.
_EMPTY = AnalystEstimates(
    fiscal_year=None,
    period_end=None,
    eps_avg=None,
    eps_low=None,
    eps_high=None,
    revenue_avg=None,
    num_analysts_eps=None,
    num_analysts_revenue=None,
)


class YfinanceEstimatesProvider(AnalystEstimatesProvider):
    """Fetches forward annual analyst estimates from Yahoo Finance (no API key)."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults
        # to the real yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_estimates(self, symbol: str) -> AnalystEstimates:
        try:
            ticker = self._ticker_factory(symbol)
            eps = ticker.earnings_estimate
            revenue = ticker.revenue_estimate
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance estimates failed ({exc})"
            ) from exc

        fy1_eps = _cell(eps, _FY1, "avg")
        fy1_revenue = _cell(revenue, _FY1, "avg")
        # Nothing worth attaching — Yahoo covers no forward year for this symbol.
        if fy1_eps is None and fy1_revenue is None:
            return _EMPTY

        fy2_eps = _cell(eps, _FY2, "avg")
        fy2_revenue = _cell(revenue, _FY2, "avg")

        fy1_end, fy2_end = self._fiscal_year_ends(ticker)
        fy1_year = fy1_end.year if fy1_end else None
        fy2_year = fy2_end.year if fy2_end else None

        # The forward series backs the FY1→FY2 growth (and lets the DB cache persist
        # FY2 revenue, which the headline fields don't carry). A series row needs a
        # period end, so it's built only when the fiscal year is known — mirroring how
        # the FMP rows always carried one.
        forward_years: list[ForwardEstimate] = []
        if fy1_end is not None:
            forward_years.append(
                ForwardEstimate(fy1_year, fy1_end, fy1_eps, fy1_revenue)
            )
            if fy2_end is not None and (fy2_eps is not None or fy2_revenue is not None):
                forward_years.append(
                    ForwardEstimate(fy2_year, fy2_end, fy2_eps, fy2_revenue)
                )

        return AnalystEstimates(
            fiscal_year=fy1_year,
            period_end=fy1_end,
            eps_avg=fy1_eps,
            eps_low=_cell(eps, _FY1, "low"),
            eps_high=_cell(eps, _FY1, "high"),
            revenue_avg=fy1_revenue,
            num_analysts_eps=_int_cell(eps, _FY1, "numberOfAnalysts"),
            num_analysts_revenue=_int_cell(revenue, _FY1, "numberOfAnalysts"),
            eps_avg_fy2=fy2_eps,
            fiscal_year_fy2=fy2_year,
            forward_years=tuple(forward_years),
        )

    def _fiscal_year_ends(self, ticker) -> tuple[date | None, date | None]:
        """Best-effort ``(FY1_end, FY2_end)`` from ``Ticker.info['nextFiscalYearEnd']``.

        The estimate frames carry no dates, so the fiscal-year-end that labels FY1 comes
        from ``info``; FY2 is a year on. Returns ``(None, None)`` when Yahoo doesn't
        report it — the estimates still serve, just without a fiscal-year label (and the
        DB cache then can't reconstruct the forward series, so growth is omitted).
        """
        try:
            info = ticker.info or {}
            stamp = info.get("nextFiscalYearEnd")
        except Exception:  # noqa: BLE001 — the date is optional; a bad `info` mustn't sink the estimate
            return (None, None)
        fy1_end = _epoch_to_date(stamp)
        if fy1_end is None:
            return (None, None)
        return (fy1_end, _add_one_year(fy1_end))


def _cell(frame, period: str, column: str) -> float | None:
    """One numeric cell of a yfinance estimate frame as a float, or ``None`` (missing
    row/column, NaN, or non-numeric). Keeps all pandas/NaN handling in the adapter."""
    value = _raw_cell(frame, period, column)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _int_cell(frame, period: str, column: str) -> int | None:
    """Like ``_cell`` but truncated to an int (analyst counts arrive as floats)."""
    number = _cell(frame, period, column)
    return None if number is None else int(number)


def _raw_cell(frame, period: str, column: str):
    """Read ``frame.loc[period, column]`` defensively — an uncovered symbol yields an
    empty/absent frame and Yahoo occasionally drops columns, so guard everything."""
    try:
        if frame is None or getattr(frame, "empty", True):
            return None
        if period not in frame.index or column not in frame.columns:
            return None
        return frame.loc[period, column]
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
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
