"""Interface Adapter: a stock's trailing fundamentals from Yahoo (via ``yfinance``).

The fundamentals sweep's per-stock source — the trailing valuation/profitability/health figures
that back the ticker card's metrics block and the AI scorecard's Profitability / Financial
health / Valuation sections. It's the only module that knows Yahoo/``yfinance`` backs the
fundamentals; swap it for another ``FundamentalsProvider`` and only this file changes. Sibling
of the earnings/ETF-profile yfinance adapters, and it reuses their two seams: the crumb-401
retry (``yfinance_session``) and the foreign-ADR currency normalizer (``yfinance_currency``).

One Yahoo surface is read per stock: ``Ticker.info`` — the crumb-gated ``quoteSummary`` blob
that carries every field below. It's Yahoo's most crumb-gated endpoint, so the read goes through
``yfinance_session.call`` with an ``is_empty`` predicate: an empty ``.info`` is treated as a
(likely swallowed) crumb 401, the cached crumb is dropped, and the call is retried once with a
fresh handshake.

**Unit normalization** (Yahoo's conventions, verified against large-cap ``.info`` blobs):

- ``grossMargins`` / ``operatingMargins`` / ``profitMargins`` / ``returnOnEquity`` = a FRACTION
  (``0.44`` = 44%) → ``×100`` → human percent, matching the percent basis the app stores
  margins on.
- ``currentRatio`` (``0.87``) and ``beta`` (``1.24``) → **as-is** (already the plain figure).
- ``debtToEquity`` — Yahoo gives it as a PERCENT (``154.0`` = 154% of equity) → ``÷100`` → a
  **ratio** (``1.54``), the basis the rest of the app uses for debt/equity.
- ``bookValue`` = book value **per share** → the P/B input, ``sales_per_share`` =
  ``totalRevenue ÷ sharesOutstanding`` → the P/S input, both reported in the filing currency and
  so normalized onto the trading currency (see below); ``dividendRate`` (fallback
  ``trailingAnnualDividendRate``) = the annual dividend per share → the yield input, already in
  the trading currency (it pairs with the USD price for Yahoo's own yield), so left untouched.

**Foreign-ADR currency.** ``bookValue`` and ``totalRevenue`` come off the financial statements,
which Yahoo always denominates in the *reporting* currency (TWD/CNY/…), while the quote the
reader prices them against is the *trading* currency (USD). Left raw, an ADR's P/B and P/S would
be off by the FX factor (~32× for TWD). So the two per-share inputs are passed through the shared
``yfinance_currency`` normalizer's unconditional reporting→trading conversion — the identity for a
US issuer or when the rate can't be read (best-effort, never-worse). The margins/ROE/ratios are
dimensionless and need no conversion; ``dividendRate`` is already trading-currency.

**Failure contract — raises on a hard ``.info`` read, best-effort on each field.** A hard
failure — a raised error, or an ``.info`` still empty after the crumb retry (Yahoo's
swallowed-401 / IP-block signal) — raises ``StockDataUnavailable`` so the sweep skips the stock
and leaves its stored figures intact. Everything past a served ``.info`` is best-effort: an
individual missing/non-numeric field degrades to ``None``, so a reachable-but-sparse stock
yields a partial snapshot, not an error.
"""

from __future__ import annotations

import yfinance as yf

from app.stocks.adapters import yfinance_currency, yfinance_session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.fundamentals.entities import Fundamentals
from app.stocks.fundamentals.ports import FundamentalsProvider


class YfinanceFundamentalsProvider(FundamentalsProvider):
    """Fetches a stock's trailing fundamentals from Yahoo's per-ticker ``.info`` (no API key).
    Raises ``StockDataUnavailable`` on a hard/blocked ``.info`` read; best-effort per field
    past that."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to the
        # real yfinance client in production. The same factory builds the FX-pair ticker the
        # currency normalizer needs, so a foreign ADR's rate is faked the same way.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_fundamentals(self, symbol: str) -> Fundamentals:
        ticker = self._ticker_factory(symbol)
        info = self._read_info(symbol, ticker)  # raises on a hard/blocked read
        # Identity for a US issuer (financialCurrency == currency, no FX call); otherwise the
        # reporting→trading spot conversion for the per-share statement figures.
        normalizer = yfinance_currency.build(self._ticker_factory, info)
        return Fundamentals(
            gross_margin=_percent_from_fraction(info.get("grossMargins")),
            operating_margin=_percent_from_fraction(info.get("operatingMargins")),
            net_margin=_percent_from_fraction(info.get("profitMargins")),
            return_on_equity=_percent_from_fraction(info.get("returnOnEquity")),
            current_ratio=_number(info.get("currentRatio")),
            debt_to_equity=_ratio_from_percent(info.get("debtToEquity")),
            beta=_number(info.get("beta")),
            book_value_per_share=normalizer.to_trading(_number(info.get("bookValue"))),
            sales_per_share=normalizer.to_trading(_sales_per_share(info)),
            dividend_per_share=_dividend_per_share(info),
        )

    def _read_info(self, symbol: str, ticker) -> dict:
        """Yahoo's ``.info`` blob, with the crumb-401 retry (an empty ``.info`` is a swallowed
        401 → drop the cached crumb, re-fetch once). Raises ``StockDataUnavailable`` on a hard
        failure — a raised error, or an ``.info`` still empty after the retry (the block signal)
        — so the sweep skips the stock and leaves its stored fundamentals intact rather than
        marking it freshly-synced with nothing to store."""
        try:
            info = yfinance_session.call(
                lambda: ticker.info,
                is_empty=lambda data: not data,
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance fundamentals failed ({exc})"
            ) from exc
        if not info:
            raise StockDataUnavailable(
                symbol,
                "yfinance fundamentals returned an empty .info (crumb 401 / IP block?)",
            )
        return info


def _sales_per_share(info: dict) -> float | None:
    """Trailing revenue per share = ``totalRevenue ÷ sharesOutstanding`` (both off ``.info``),
    in the filing currency (the caller converts it to trading currency). ``None`` when either
    input is missing/non-numeric or the share count is non-positive."""
    revenue = _number(info.get("totalRevenue"))
    shares = _number(info.get("sharesOutstanding"))
    if revenue is None or shares is None or shares <= 0:
        return None
    return revenue / shares


def _dividend_per_share(info: dict) -> float | None:
    """The annual dividend per share (trading currency) — Yahoo's forward ``dividendRate``,
    falling back to ``trailingAnnualDividendRate``. ``None`` (not ``0``) for a non-payer, so the
    yield the reader derives is absent rather than a misleading zero."""
    rate = _number(info.get("dividendRate"))
    if rate is None:
        rate = _number(info.get("trailingAnnualDividendRate"))
    return rate if rate else None


def _percent_from_fraction(value: object) -> float | None:
    """A vendor FRACTION (e.g. ``0.44``) → a human percent (``44.0``), or ``None`` when
    absent/non-numeric."""
    number = _number(value)
    return None if number is None else number * 100


def _ratio_from_percent(value: object) -> float | None:
    """Yahoo's ``debtToEquity`` (a PERCENT of equity, e.g. ``154.0``) → a plain ratio (``1.54``),
    the basis the app stores debt/equity on. ``None`` when absent/non-numeric."""
    number = _number(value)
    return None if number is None else number / 100


def _number(value: object) -> float | None:
    """A numeric vendor field → ``float``, or ``None`` when absent/non-numeric. ``bool`` is
    rejected (an ``int`` subclass, never a real figure), matching the sibling adapters."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
