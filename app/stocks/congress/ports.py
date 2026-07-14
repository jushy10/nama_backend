"""Application port for the Congressional-trades live source.

The abstraction the out-of-band sync depends on for a *live* source of recent Congressional stock
trades — implemented by the community stock-watcher adapter in ``app/stocks/adapters``. Dependency
Inversion: the core reads through this interface, never httpx / a specific dataset directly, so the
source is swappable and the tests run offline against a hand-written fake. The *persistence* seam
is separate — the repository port lives in ``repository.py``.

Note the shape: unlike the per-symbol provider ports (insider / earnings), the Congressional feed
is a **single market-wide dataset** covering every member and ticker at once, so the source exposes
one bulk ``fetch_recent_trades`` rather than a per-symbol lookup. ``SyncCongressTrades`` fetches it
once and distributes the trades across the anchor. The *read* path never touches this port — it's
DB-only (served through the repository).
"""

from abc import ABC, abstractmethod

from app.stocks.congress.entities import CongressTrade


class CongressTradesSource(ABC):
    """A gateway for the market-wide feed of recent Congressional stock trades."""

    @abstractmethod
    def fetch_recent_trades(self) -> tuple[CongressTrade, ...]:
        """Return every recent Congressional stock trade the source carries, across all chambers,
        members and tickers, newest first.

        A market-wide feed, so there is no per-symbol "not found" — a stock nobody traded simply
        isn't in the result. When the source spans several chamber feeds it is best-effort per
        feed (one chamber down still returns the other's trades).

        Raises:
            StockDataUnavailable: every underlying feed failed (transport / bad response / unparseable
                body) — there is nothing to distribute this run.
        """
        raise NotImplementedError
