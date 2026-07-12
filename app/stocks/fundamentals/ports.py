"""The live-source port for the fundamentals slice.

The abstraction the ``SyncFundamentals`` use case depends on — a single capability, phrased in
domain terms, that returns a :class:`Fundamentals` entity and raises the slice's domain
exceptions. The concrete adapter (Yahoo via ``yfinance``) lives in
``app/stocks/adapters/yfinance_fundamentals_adapter.py`` and is the only code that knows the
vendor exists.
"""

from abc import ABC, abstractmethod

from app.stocks.fundamentals.entities import Fundamentals


class FundamentalsProvider(ABC):
    """A live source of a stock's trailing fundamentals snapshot."""

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> Fundamentals:
        """Return the trailing fundamentals for ``symbol``.

        Raises ``StockNotFound`` when the vendor doesn't cover the symbol and
        ``StockDataUnavailable`` on a hard/blocked read (for Yahoo, an empty ``.info`` — its
        swallowed-401 / IP-block signal — so the sync skips the stock and leaves its stored
        figures intact rather than overwriting them with nothing). A *served* snapshot that is
        merely sparse is returned with its present fields set and the rest ``None`` — best-effort
        past a successful read, never raised.
        """
        raise NotImplementedError
