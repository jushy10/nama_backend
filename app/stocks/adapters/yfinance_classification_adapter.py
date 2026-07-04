"""Interface Adapter: a stock's sector + industry from Yahoo (via ``yfinance``).

Yahoo's bulk screener carries no sector/industry — the universe screen adapter documents
this — so they're read here one ticker at a time off ``Ticker.info`` (the same per-ticker
surface the annual-earnings adapter reads ``nextFiscalYearEnd`` from). ``info['sector']`` /
``info['industry']`` are Yahoo's display labels (``"Technology"`` / ``"Consumer
Electronics"``); the entity slugs them to snake_case. This is the only module that knows
``yfinance``/Yahoo backs the classification; swap it for another
``CompanyClassificationProvider`` and only this file changes.

Best-effort by design: ``.info`` is an unofficial, rate-limited surface Yahoo gates from
data-centre IPs, so any failure becomes ``StockDataUnavailable`` (the sync counts it and
moves on), and a symbol Yahoo doesn't classify yields an empty ``CompanyClassification``
(both sides ``None``) rather than an error.
"""

from __future__ import annotations

import yfinance as yf

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.entities import CompanyClassification
from app.stocks.universe.ports import CompanyClassificationProvider


class YfinanceClassificationProvider(CompanyClassificationProvider):
    """Fetches a stock's sector + industry from Yahoo's per-ticker ``.info`` (no API key)."""

    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to the
        # real yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_classification(self, symbol: str) -> CompanyClassification:
        try:
            info = self._ticker_factory(symbol).info or {}
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance classification failed ({exc})"
            ) from exc
        return CompanyClassification.from_labels(
            info.get("sector"), info.get("industry")
        )
