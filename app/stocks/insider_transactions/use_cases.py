"""Application use case for the insider-transactions slice.

One action, pure orchestration over the port so it runs offline in tests against a hand-written
fake and knows nothing of SEC EDGAR, HTTP, or SQLAlchemy:

- ``GetInsiderTransactions`` — the read path. Normalizes the symbol and returns the activity
  through the ``InsiderTransactionsProvider`` (wired in production as the TTL DB cache over SEC,
  so the read hits EDGAR only on a cold or stale miss).

There is deliberately **no ``Sync*`` use case** here (unlike the earnings / recommendations /
news / revenue-segments slices): this slice has no out-of-band cron. Freshness rides on the TTL
read-through cache, which re-fetches a stock on read once its stored rows age past the TTL.
"""

from __future__ import annotations

from app.stocks.insider_transactions.entities import InsiderActivity
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use case —
    so every layer below sees a clean symbol. Mirrors the stocks slice's guard."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


class GetInsiderTransactions:
    """Use case: retrieve a stock's recent insider (Form 4) transactions by its symbol.

    Best-effort: a stock with no recent insider activity yields an empty activity rather than an
    error, so the endpoint can present an empty result instead of a 404.
    """

    def __init__(self, provider: InsiderTransactionsProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> InsiderActivity:
        return self._provider.get_insider_transactions(_normalize_symbol(symbol))
