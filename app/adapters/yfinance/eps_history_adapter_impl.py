from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd
import yfinance as yf

from app.adapters.yfinance import currency, session
from app.adapters.yfinance.currency import CurrencyNormalizer
from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.pricing.ticker.entities import ReportedEps
from app.domains.pricing.ticker.interfaces import EpsHistoryAdapter

# Announcement rows to request from ``get_earnings_dates``: ~4 scheduled future + ~24 past
# (~7 years of quarters). Matches the annual adapter's depth — the deepest window Yahoo
# reliably serves, and more than a trailing-P/E chart needs.
_EARNINGS_DATES_LIMIT = 28


class EpsHistoryAdapterImpl(EpsHistoryAdapter):
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
            # Routed through the shared session module for pacing + a fresh-crumb retry on a raised
            # 401. No is_empty retry: a genuinely uncovered symbol has an empty frame, and
            # retrying on empty would just double the call for every such stock.
            frame = session.call(
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
        info = currency.read_info(ticker)
        try:
            eps_estimate = session.call(lambda: ticker.earnings_estimate)
        except Exception:  # noqa: BLE001 — detection input only: degrade to no conversion
            eps_estimate = None
        return currency.build(
            self._ticker_factory,
            info,
            market_eps_sample=_cell(eps_estimate, "0y", "avg"),
            market_eps_reference=_num(info.get("forwardEps")),
        )


def _parse(frame, normalizer: CurrencyNormalizer) -> tuple[ReportedEps, ...]:
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
