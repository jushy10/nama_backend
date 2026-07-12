"""Abstract persistence port for the fundamentals slice.

The interface the ``SyncFundamentals`` use case depends on — Dependency Inversion for storage.
The use case is handed a ``FundamentalsRepository`` and never knows whether it's backed by
SQLAlchemy (the anchor columns) or an in-memory fake (tests); it just calls these two methods.
The concrete SQLAlchemy implementation lives in ``db_repository.py``.

A *Repository*, not a *Provider*: the fundamentals are slow-moving reference data refreshed
out of band (the ``sync-fundamentals`` cron), not a live feed. Caching Yahoo's ``.info`` this
way keeps the request path off Yahoo, which rate-limits data-centre IPs.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.fundamentals.entities import Fundamentals


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync: the symbol to re-fetch and the display name
    already on its ``stocks`` row, so a nameless refresh doesn't drop a known company name.
    """

    symbol: str
    name: str | None


class FundamentalsRepository(ABC):
    """A persistent store for a stock's trailing fundamentals, on the shared ``stocks`` anchor.

    Table-less: the figures are denormalized columns on ``stocks`` (like the index-membership
    flags and the universe screen facts), so this port writes them straight onto the anchor row.
    """

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        """Return the anchor stocks most in need of a fundamentals refresh — **un-synced first**
        (a ``NULL`` ``fundamentals_synced_at`` sorts ahead of every synced row), then the
        stalest-synced — each paired with the name on its ``stocks`` row.

        Includes stocks never synced, so one sweep both *seeds* new coverage and renews stale
        rows. ``limit`` caps the batch; ``None`` returns every anchor stock (one sweep seeds
        them all).
        """
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, fundamentals: Fundamentals) -> None:
        """Write ``fundamentals`` onto the ``symbol``'s anchor row and stamp
        ``fundamentals_synced_at``, creating the row if absent (setting its display name when
        one is supplied, never overwriting a known name with ``None``).

        Overwrites every fundamentals column, including to ``None`` — a moving snapshot, not
        fill-once, so a figure Yahoo has since dropped is cleared rather than left stale. The
        caller skips an all-``None`` snapshot (see ``Fundamentals.is_empty``), so a stamped row
        always carries at least one figure.
        """
        raise NotImplementedError
