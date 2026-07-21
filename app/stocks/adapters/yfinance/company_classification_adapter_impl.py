from __future__ import annotations

import yfinance as yf

from app.stocks.adapters.yfinance import session
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.catalog.universe.entities import CompanyClassification
from app.stocks.catalog.universe.interfaces import CompanyClassificationAdapter


class CompanyClassificationAdapterImpl(CompanyClassificationAdapter):
    def __init__(self, *, ticker_factory=None) -> None:
        # Injectable so tests supply a fake Ticker instead of reaching Yahoo; defaults to the
        # real yfinance client in production.
        self._ticker_factory = ticker_factory or yf.Ticker

    def get_classification(self, symbol: str) -> CompanyClassification:
        try:
            # An empty .info is how yfinance surfaces a swallowed crumb 401, so treat it as
            # retryable: the shared session module drops the cached crumb and re-fetches once.
            info = (
                session.call(
                    lambda: self._ticker_factory(symbol).info,
                    is_empty=lambda data: not data,
                )
                or {}
            )
        except Exception as exc:  # noqa: BLE001 — vendor boundary: any failure → domain error
            raise StockDataUnavailable(
                symbol, f"yfinance classification failed ({exc})"
            ) from exc
        return CompanyClassification.from_labels(
            info.get("sector"), info.get("industry"), info.get("country")
        )
