"""Interface Adapter: an ETF's category from Yahoo (via ``yfinance``).

Yahoo's bulk ETF screen carries no category — the ETF screen adapter documents this — so it's
read here one ticker at a time off ``Ticker.info`` (``info['category']`` is Yahoo's display
label, e.g. ``"Large Growth"`` / ``"Commodities Focused"``; the entity slugs it to snake_case).
This is the only module that knows ``yfinance``/Yahoo backs the ETF category; swap it for another
``EtfCategoryProvider`` and only this file changes. It's the ETF analogue of
``yfinance_classification_adapter`` (stock sector/industry).

Best-effort by design: ``.info`` is an unofficial, rate-limited surface Yahoo gates from
data-centre IPs, so any failure becomes ``StockDataUnavailable`` (the sync counts it and moves
on), and a fund Yahoo doesn't categorise yields an empty ``EtfClassification`` (``category``
``None``) rather than an error.

``.info`` is Yahoo's most crumb-gated endpoint (the ``quoteSummary`` surface), so it's the one
most often lost to a transient **HTTP 401 "Invalid Crumb"** from a data-centre IP — which
yfinance *swallows* into an empty ``.info`` under its default ``hide_exceptions``. The fetch
therefore goes through ``yfinance_session.call`` with an ``is_empty`` predicate: an empty
``.info`` is treated as a (likely swallowed) crumb 401, the cached crumb is dropped, and the call
is retried once with a fresh handshake. A genuinely uncategorised fund simply comes back empty
after that retry, unchanged.
"""

from __future__ import annotations

import yfinance as yf

from app.stocks.adapters import yfinance_session
from app.stocks.etfs.entities import EtfClassification
from app.stocks.etfs.ports import EtfCategoryProvider
from app.stocks.exceptions import StockDataUnavailable


class YfinanceEtfCategoryProvider(EtfCategoryProvider):
    """Fetches an ETF's category from Yahoo's per-ticker ``.info`` (no API key)."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to the real
        # yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_category(self, symbol: str) -> EtfClassification:
        try:
            # An empty .info is how yfinance surfaces a swallowed crumb 401, so treat it as
            # retryable: yfinance_session drops the cached crumb and re-fetches once.
            info = (
                yfinance_session.call(
                    lambda: self._ticker_factory(symbol).info,
                    is_empty=lambda data: not data,
                )
                or {}
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance ETF category failed ({exc})"
            ) from exc
        return EtfClassification.from_label(info.get("category"))
