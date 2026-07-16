"""Interface Adapter: a stock's sector + industry + issuer domicile from Yahoo (via ``yfinance``).

Yahoo's bulk screener carries none of these — the universe screen adapter documents this — so
they're read here one ticker at a time off ``Ticker.info`` (the same per-ticker surface the
annual-earnings adapter reads ``nextFiscalYearEnd`` from). ``info['sector']`` / ``info['industry']``
are Yahoo's display labels (``"Technology"`` / ``"Consumer Electronics"``), which the entity slugs
to snake_case; ``info['country']`` is the company's home country (``"United States"`` /
``"Canada"`` / ``"Switzerland"``), which the entity maps to an ISO-2 code — the domicile the
universe search splits the US / Canadian screeners on. All three ride one ``.info`` call. This is
the only module that knows ``yfinance``/Yahoo backs the classification; swap it for another
``CompanyClassificationProvider`` and only this file changes.

Best-effort by design: ``.info`` is an unofficial, rate-limited surface Yahoo gates from
data-centre IPs, so any failure becomes ``StockDataUnavailable`` (the sync counts it and
moves on), and a symbol Yahoo doesn't classify yields an empty ``CompanyClassification``
(both sides ``None``) rather than an error.

``.info`` is Yahoo's most crumb-gated endpoint (the ``quoteSummary`` surface), so it's the
one most often lost to a transient **HTTP 401 "Invalid Crumb"** from a data-centre IP —
which yfinance *swallows* into an empty ``.info`` under its default ``hide_exceptions``. The
fetch therefore goes through ``yfinance_session.call`` with an ``is_empty`` predicate: an
empty ``.info`` is treated as a (likely swallowed) crumb 401, the cached crumb is dropped,
and the call is retried once with a fresh handshake. A genuinely unclassified symbol simply
comes back empty after that retry, unchanged.
"""

from __future__ import annotations

import yfinance as yf

from app.stocks.adapters import yfinance_session
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
                symbol, f"yfinance classification failed ({exc})"
            ) from exc
        return CompanyClassification.from_labels(
            info.get("sector"), info.get("industry"), info.get("country")
        )
