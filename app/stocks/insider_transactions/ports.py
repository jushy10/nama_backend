"""Application port for the insider-transactions live source.

The abstraction the read path depends on for a *live* source of a stock's SEC Form 4 insider
transactions — implemented by the SEC EDGAR adapter and the TTL DB-cache decorator in
``app/stocks/adapters``. Dependency Inversion: the core reads through this interface, never
SEC/httpx directly, so the source is swappable and the tests run offline against a hand-written
fake. The *persistence* seam is separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.insider_transactions.entities import InsiderActivity


class InsiderTransactionsProvider(ABC):
    """A gateway for a stock's recent insider (Form 4) transactions."""

    @abstractmethod
    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        """Return the insider activity for the (already-normalized) symbol.

        Returns an ``is_empty`` activity when the source covers the symbol but its insiders have
        no recent reportable transactions (or none we parse) — "no recent activity" is not an
        error for this best-effort feature.

        Raises:
            StockNotFound: the symbol is not covered by the source (no matching filer).
            StockDataUnavailable: the upstream source failed (transport / bad response).
        """
        raise NotImplementedError
