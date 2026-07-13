"""Interface Adapter: a DB-only (no live fall-through) view of the insider-transactions cache.

The read path serves a stock's Form 4 feed **straight from the database and only from the
database** — a stored feed is returned, a miss yields an *empty* one, and a cache-read failure
degrades to empty too. It **never** fetches from SEC EDGAR on a read. Keeping the store current is
entirely the weekly ``sync-insider-transactions`` cron's job (``SyncInsiderTransactions``), which
seeds un-cached stocks and refreshes stale ones out of band.

This is deliberately *not* a read-through cache (it replaced ``DbCachedInsiderTransactionsProvider``):
a read-through fetches live on a cold miss, so the first view of a not-yet-synced ticker would pay
the full multi-request SEC Form 4 walk — up to ~26 sequential, paced requests — inside the user
request. Making the read DB-only guarantees a read *never* walks the filings, at the cost that a
ticker the cron hasn't reached yet reads as empty until the next sweep seeds it (indistinguishable
from a stock with genuinely no recent insider activity — both an empty 200, the endpoint's
best-effort contract). It's the same DB-only division of labour the AI-analysis context providers
use (``db_only_context_providers``), applied here to a primary read.

It implements ``InsiderTransactionsProvider``, so it slots into the read wiring exactly where the
read-through cache used to, with the use case none the wiser. The live SEC provider now backs only
the cron.
"""

from __future__ import annotations

import logging

from app.stocks.insider_transactions.entities import InsiderActivity
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider
from app.stocks.insider_transactions.repository import InsiderTransactionsRepository

logger = logging.getLogger(__name__)


class DbOnlyInsiderTransactionsProvider(InsiderTransactionsProvider):
    """Serve the stored insider feed; a miss (or read error) yields empty. Never fetches live."""

    def __init__(self, repo: InsiderTransactionsRepository) -> None:
        self._repo = repo

    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort feed; a DB hiccup reads empty, never 500s
            logger.warning(
                "insider transactions cache read failed for %s", symbol, exc_info=True
            )
            stored = None
        return stored if stored is not None else InsiderActivity(symbol)
