"""Abstract persistence port for the universe slice.

Dependency Inversion for storage: the use cases are handed a ``UniverseRepository`` and
never know whether it's backed by SQLAlchemy or an in-memory fake (tests) — they just call
these methods. The concrete SQLAlchemy implementation lives in ``db_repository.py``, over
the models and queries in ``models.py``.

A *Repository*, not a *Provider*: the universe is a slow-moving set refreshed out of band
(the cron endpoint), not a live feed. It writes into the shared ``stocks`` anchor
(ticker/name/exchange) and the slice's own ``stock_universe`` child rows (market cap /
sector / screen time).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stocks.universe.entities import ScreenedStock


@dataclass(frozen=True)
class UniverseSyncCounts:
    """The row-level outcome of one reconcile: universe rows newly inserted, updated in
    place, and removed (a stock that fell below the floor or delisted). Anchor rows are
    never removed — other slices may still reference them, and their identity facts stay
    valid — so ``removed`` counts only ``stock_universe`` rows."""

    added: int
    updated: int
    removed: int


class UniverseRepository(ABC):
    """A persistent store for the screened universe, read for search and rewritten by the
    sync."""

    @abstractmethod
    def replace_universe(self, stocks: tuple[ScreenedStock, ...]) -> UniverseSyncCounts:
        """Reconcile the stored universe to exactly ``stocks``.

        For each screened stock: fill the ``stocks`` anchor (ticker/name/exchange, never
        clobbering a known value) and upsert its ``stock_universe`` row (market cap /
        sector / screen time). Then remove universe rows whose ticker is absent from
        ``stocks`` — the anchor row stays. Commits its own write; returns the per-row
        counts.

        The caller (``SyncUniverse``) guarantees ``stocks`` is a *complete, non-empty*
        screen before calling — this method always reconciles, deletes included, so a
        partial screen must never reach it.
        """
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, *, limit: int) -> tuple[ScreenedStock, ...]:
        """Up to ``limit`` universe stocks whose ticker or name matches ``query`` (a
        case-insensitive substring), largest market cap first. Empty when nothing matches.
        Only stocks currently in the screened universe are returned (the read joins the
        ``stock_universe`` rows), so a stock whose anchor exists but which isn't a member
        won't surface."""
        raise NotImplementedError
