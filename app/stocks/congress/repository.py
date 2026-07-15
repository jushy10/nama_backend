"""Abstract persistence port for the Congressional-trades slice.

The interface the DB-only read path and the out-of-band sync depend on for storage — Dependency
Inversion, so the ``GetCongressTrades`` / ``GetCongressActivity`` use cases, the
``SyncCongressTrades`` sweep, and the tests are handed a ``CongressTradesRepository`` and never know
whether it's backed by SQLAlchemy or an in-memory fake. The concrete SQLAlchemy implementation
lives in ``db_repository.py``, over the models and queries in ``models.py``.

Unlike the read-through cache slices, the Congressional read is **DB-only**: a stock is served
straight from the store at any age and a miss reads empty — the read never fetches live. A
**weekly cron** (``SyncCongressTrades``) keeps the store current, fetching the whole market-wide
feed once and distributing it. ``refresh_targets`` orders that sweep — un-cached stocks first (so
it also *seeds* new coverage), then the least-recently-refreshed by ``fetched_at``.
"""

from abc import ABC, abstractmethod
from datetime import date
from typing import NamedTuple

from app.stocks.congress.entities import CongressActivity, CongressTrade


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync use case: the symbol to re-store and the display name
    already on its ``stocks`` row, so a nameless refresh doesn't drop a known company name.
    """

    symbol: str
    name: str | None


class CongressTradesRepository(ABC):
    """A persistent store for Congressional stock trades — the DB behind the DB-only reads."""

    @abstractmethod
    def get(self, symbol: str) -> CongressActivity | None:
        """Return the stored activity for the (already-normalized) symbol, or ``None`` when nothing
        is stored yet. A stored symbol always has at least one trade row, so ``None`` unambiguously
        means "never cached", never "cached but empty"."""
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, activity: CongressActivity) -> None:
        """Merge ``activity``'s trades into the store, stamping the fetch time.

        **Insert-only** by ``(member, transaction_date, amount_range, chamber)`` — a filed
        disclosure is a frozen fact, so a refresh adds only trades not already stored and never
        rewrites an existing row (like the insider / rating-changes slices). The accumulated feed
        is **pruned** to the newest N trades per stock so the history stays bounded (like the news
        slice). Every stored row's fetch stamp is refreshed to the current time (even when the
        fetch brought no new trades) so the sweep's staleness order treats the whole feed as freshly
        confirmed. Ensures the parent ``stocks`` row exists, setting its display name when one is
        supplied (never overwriting a known name with ``None``).
        """
        raise NotImplementedError

    @abstractmethod
    def recent_market_activity(
        self, *, since: date | None, limit: int, offset: int
    ) -> tuple[list[CongressTrade], int]:
        """A page of the whole market's recent trades (newest first) plus the full match count.

        ``since`` (inclusive) windows on the activity date — the disclosure date falling back to
        the transaction date; ``None`` means no window. Returns ``(page, total)`` where ``total`` is
        the count in the window before the page was cut. Each trade carries its ``ticker`` and
        ``company_name`` (the anchor's stored name) so the board reads without a second lookup.
        """
        raise NotImplementedError

    @abstractmethod
    def market_trades_in_window(self, *, since: date | None) -> list[CongressTrade]:
        """Every stored market-wide trade in the window (newest first), each carrying its ``ticker``
        and ``company_name`` — the unpaged set the attention leaderboard folds by ticker.

        ``since`` (inclusive) windows on the activity date (disclosure date falling back to the
        transaction date); ``None`` means all history. Unlike ``recent_market_activity`` this is not
        paged: the leaderboard aggregates the whole window, not a slice of it.
        """
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        """Return the stocks most in need of a refresh, un-cached first then least-recently
        refreshed, each paired with the name on its ``stocks`` row.

        Includes stocks not yet cached, so the out-of-band sync both *seeds* new coverage and
        renews stale rows. ``limit`` caps the batch; ``None`` returns every anchor stock (one sweep
        seeds them all)."""
        raise NotImplementedError
