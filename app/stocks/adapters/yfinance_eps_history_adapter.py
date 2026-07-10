"""Adapter: a stock's deep reported-EPS history from Yahoo (yfinance), keyless.

The trailing leg of the ticker card's P/E-history walk (``GET
/stocks/ticker/{ticker}/pe-history``). Where the quarterly-earnings adapter builds a
4-recent + 2-upcoming *timeline*, this asks ``Ticker.get_earnings_dates`` for a deep
window (~7 years of quarters) and keeps only the *reported* rows — the raw EPS run the
use case rolls into a trailing-twelve-month series to divide each historical close by.

Yahoo is the one vendor that publishes this depth without a key, and — like the other
yfinance adapters — it's IP-gated intermittently from data-centre IPs, so the read is
best-effort: any failure becomes ``StockDataUnavailable`` for the use case to swallow,
and an uncovered symbol is an empty tuple (no history, not an error). Routed through
``yfinance_session`` for request pacing + a fresh-crumb retry on a 401, the same seam the
sibling adapters share; ``_ticker_factory`` is the fake seam the offline tests drive.

**Currency (foreign ADRs).** ``get_earnings_dates`` "Reported EPS" is a *market* EPS
surface, which Yahoo quotes per-ADR in a currency that varies by issuer — USD for TSM but
the reporting currency (CNY) for BABA and many Chinese ADRs — while the P/E-history prices
this EPS divides into are always the trading currency (USD). Dividing a USD close by a CNY
EPS understates the multiple ~7×, so each reported EPS is run through the shared
``yfinance_currency`` normalizer onto the trading currency. Its currency is detected once —
the ``earnings_estimate`` ``0y`` forward annual estimate against the trading-currency
``info['forwardEps']`` — exactly as the quarterly / annual earnings adapters do. The extra
``info`` / ``earnings_estimate`` reads are best-effort enrichment: a blocked one yields the
identity normalizer (no conversion) rather than sinking the history, and it's a no-op for
every US issuer.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd
import yfinance as yf

from app.stocks.adapters import yfinance_currency, yfinance_session
from app.stocks.adapters.yfinance_currency import CurrencyNormalizer
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.ticker.entities import ReportedEps
from app.stocks.ticker.ports import EpsHistoryProvider

# Announcement rows to request from ``get_earnings_dates``: ~4 scheduled future + ~24 past
# (~7 years of quarters). Matches the annual adapter's depth — the deepest window Yahoo
# reliably serves, and more than a trailing-P/E chart needs.
_EARNINGS_DATES_LIMIT = 28


class YfinanceEpsHistoryProvider(EpsHistoryProvider):
    """Fetches a stock's deep reported-EPS history from Yahoo (no API key)."""

    def __init__(
        self, *, ticker_factory=None, limit: int = _EARNINGS_DATES_LIMIT
    ) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to
        # the real yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker
        self._limit = limit

    def get_eps_history(self, symbol: str) -> tuple[ReportedEps, ...]:
        try:
            ticker = self._ticker_factory(symbol)
            # Routed through yfinance_session for pacing + a fresh-crumb retry on a raised
            # 401. No is_empty retry: a genuinely uncovered symbol has an empty frame, and
            # retrying on empty would just double the call for every such stock.
            frame = yfinance_session.call(
                lambda: ticker.get_earnings_dates(limit=self._limit)
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance EPS history failed ({exc})"
            ) from exc
        # A foreign ADR's "Reported EPS" may be quoted in its reporting currency (CNY for
        # BABA) while the P/E-history prices are USD, so normalize each reported EPS onto the
        # trading currency. Best-effort — a blocked info/estimate read degrades to the
        # identity normalizer (no conversion), never sinking the (primary) history.
        return _parse(frame, self._currency_normalizer(ticker))

    def _currency_normalizer(self, ticker) -> CurrencyNormalizer:
        """The currency normalizer for this issuer, mirroring the quarterly / annual earnings
        adapters: detect the *market* EPS currency once from the ``earnings_estimate`` ``0y``
        forward annual estimate against the trading-currency ``info['forwardEps']``. Every read
        here is best-effort — a failure degrades to the identity normalizer — and it's a no-op
        (no FX call) for a domestic issuer."""
        info = yfinance_currency.read_info(ticker)
        try:
            eps_estimate = yfinance_session.call(lambda: ticker.earnings_estimate)
        except Exception:  # noqa: BLE001 — detection input only: degrade to no conversion
            eps_estimate = None
        return yfinance_currency.build(
            self._ticker_factory,
            info,
            market_eps_sample=_cell(eps_estimate, "0y", "avg"),
            market_eps_reference=_num(info.get("forwardEps")),
        )


def _parse(frame, normalizer: CurrencyNormalizer) -> tuple[ReportedEps, ...]:
    """``get_earnings_dates`` → the reported quarters, oldest first.

    Keeps only rows with a real announcement date AND a reported (actual) EPS — future
    quarters carry a NaN ``Reported EPS`` and are dropped. Deduped by date (Yahoo can list
    a boundary quarter twice), keeping the last reported figure for a date. Each reported EPS
    is normalized onto the trading currency (``market_to_trading`` — a no-op for a domestic
    issuer) so it divides cleanly into the USD P/E-history prices."""
    if frame is None or getattr(frame, "empty", True):
        return ()
    try:
        pairs = list(frame.iterrows())
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return ()
    by_date: dict[date, float] = {}
    for index, series in pairs:
        report_date = _to_date(index)
        if report_date is None:
            continue
        eps = _num(_series_get(series, "Reported EPS"))
        if eps is None:
            continue  # a future/unreported quarter — nothing to anchor a P/E on
        by_date[report_date] = normalizer.market_to_trading(eps)
    return tuple(ReportedEps(report_date=d, eps=by_date[d]) for d in sorted(by_date))


def _series_get(series, key: str):
    """One labelled value from a row Series, or ``None`` (missing column)."""
    try:
        return series.get(key)
    except Exception:  # noqa: BLE001 — a frame quirk must not escape the adapter
        return None


def _cell(frame, period: str, column: str) -> float | None:
    """One numeric cell of a period-indexed estimate frame as a float, or ``None`` (missing
    row/column, NaN, non-numeric) — reads the ``0y`` forward estimate for currency detection.
    The same helper the quarterly / annual adapters use on the estimate frames."""
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
