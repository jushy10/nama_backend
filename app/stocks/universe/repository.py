"""Abstract persistence port for the universe slice.

Dependency Inversion for storage: the use cases are handed a ``UniverseRepository`` and
never know whether it's backed by SQLAlchemy or an in-memory fake (tests) — they just call
these methods. The concrete SQLAlchemy implementation lives in ``db_repository.py``, over
the shared ``stocks`` anchor.

A *Repository*, not a *Provider*: the universe is a slow-moving set refreshed out of band
(the cron endpoint), not a live feed. It writes the screen straight onto the ``stocks``
anchor (ticker/name/exchange plus the denormalized ``sector``/``market_cap``/``screened_at``
columns) — there is no separate universe table.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stocks.universe.entities import ScreenedStock


@dataclass(frozen=True)
class UniverseSyncCounts:
    """The row-level outcome of one screen upsert: anchors newly marked as screened members
    (``added``) and existing members refreshed in place (``updated``).

    The sync is **additive** — it never removes a stock. A company that later falls below
    the floor keeps its last-screened facts rather than being deleted, because the
    ``stocks`` row is a shared anchor other slices reference; there is no ``removed`` count.
    ``added`` counts a stock the screen marks as a member for the first time (its
    ``screened_at`` was null — whether the anchor is brand new or was created earlier by
    another feature); ``updated`` counts one already carrying screen facts.
    """

    added: int
    updated: int


class UniverseRepository(ABC):
    """A persistent store for the screened universe, read for search and refreshed by the
    sync — the shared ``stocks`` anchor, in practice."""

    @abstractmethod
    def upsert_screen(self, stocks: tuple[ScreenedStock, ...]) -> UniverseSyncCounts:
        """Upsert every screened stock onto the ``stocks`` anchor and return the per-row
        counts.

        For each: create the anchor if absent, fill ticker/name/exchange when missing
        (never clobbering a settled value), and set/refresh the screen facts
        (``market_cap``/``sector``/``screened_at``) — ``sector`` only when supplied, so a
        source that omits it doesn't wipe a known one. Additive: stocks absent from the
        screen are left untouched (no delete). Commits its own write.
        """
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, *, limit: int) -> tuple[ScreenedStock, ...]:
        """Up to ``limit`` screened stocks whose ticker or name matches ``query`` (a
        case-insensitive substring), largest market cap first. Empty when nothing matches.
        Only screened members are returned (rows with a ``market_cap``), so a ticker that
        reached the anchor some other way — but was never screened — won't surface.
        """
        raise NotImplementedError
