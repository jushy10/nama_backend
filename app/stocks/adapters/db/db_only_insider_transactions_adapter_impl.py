from __future__ import annotations

import logging

from app.stocks.company.insider_transactions.entities import InsiderActivity
from app.stocks.company.insider_transactions.interfaces import InsiderTransactionsAdapter
from app.stocks.company.insider_transactions.interfaces import InsiderTransactionsRepositoryAdapter

logger = logging.getLogger(__name__)


class InsiderTransactionsAdapterImpl(InsiderTransactionsAdapter):
    def __init__(self, repo: InsiderTransactionsRepositoryAdapter) -> None:
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
