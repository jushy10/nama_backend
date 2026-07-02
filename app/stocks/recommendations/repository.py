"""Abstract persistence port for the recommendations slice.

The interface the use cases depend on — Dependency Inversion for storage. A use case is
handed a ``RecommendationsRepository`` and never knows whether it's backed by SQLAlchemy,
an in-memory fake (tests), or anything else; it just calls these methods. The concrete
SQLAlchemy implementation lives in ``db_repository.py``, over the models and queries in
``models.py``.

A *Repository*, not a *Provider*: the rows are slow-moving monthly snapshots refreshed
out of band (the cron endpoint) and lazily on a miss, not a live feed. Caching yfinance
this way keeps the endpoint off Yahoo, which rate-limits data-centre IPs.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.recommendations.entities import AnalystRecommendations


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync use case: the symbol to re-fetch and the
    display name already on its ``stocks`` row, so a nameless refresh doesn't drop a
    known company name when it re-stores the rows.
    """

    symbol: str
    name: str | None


class RecommendationsRepository(ABC):
    """A persistent store for a stock's analyst recommendation trends.

    The database-backed companion to the live ``RecommendationProvider``: the provider
    hits the vendor (yfinance), this caches the result so the endpoint rarely does.
    """

    @abstractmethod
    def get(self, symbol: str) -> AnalystRecommendations | None:
        """Return the stored trends for the (already-normalized) symbol, newest first,
        or ``None`` when nothing is stored yet. A miss is not an error — it's the gap
        the read-through cache fills from the live source. A stored symbol always has at
        least one trend row, so ``None`` unambiguously means "never cached", never
        "cached but empty"."""
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, recommendations: AnalystRecommendations
    ) -> None:
        """Merge ``recommendations``' snapshots into the store, stamping the fetch time.

        *Merge*, not rewrite — unlike the earnings repositories: Yahoo serves only the
        last few months, but a past month's split is a frozen fact, so a refresh replaces
        the fetched months and leaves earlier stored months intact. The table thereby
        accumulates a longer history than the source ever serves at once. Ensures the
        parent ``stocks`` row exists, setting its display name when one is supplied
        (never overwriting a known name with ``None``).
        """
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        """Return up to ``limit`` stored symbols most in need of a refresh,
        least-recently-refreshed first, each paired with the name on its ``stocks`` row.

        The out-of-band sync walks these to renew the rows users actually view while
        staying gentle on the vendor; symbols never stored (hence never viewed) aren't
        returned — those are filled lazily on first access instead."""
        raise NotImplementedError
