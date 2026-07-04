"""Application use cases for the universe slice.

Two actions, both pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of Nasdaq, HTTP, or SQLAlchemy:

- ``SyncUniverse`` — the out-of-band populator. Screens the US market at/above the floor
  and reconciles the stored universe to it. Invoked by the cron endpoint. Guarded so a
  vendor block (an empty or implausibly small screen) leaves the stored universe intact
  rather than wiping it.
- ``SearchStocks`` — the read path. Normalizes the query and returns the matching universe
  stocks, largest market cap first.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.ports import StockScreener
from app.stocks.universe.repository import UniverseRepository


@dataclass(frozen=True)
class UniverseSyncReport:
    """The outcome of one sync run: the screen size and the rows added / updated / removed,
    plus ``skipped`` — ``True`` when the reconcile was deliberately not run because the
    screen came back empty or implausibly small (a truncated or blocked fetch), so the
    stored universe was left untouched. When ``skipped`` is ``True`` the three counts are
    all zero."""

    screened: int
    added: int
    updated: int
    removed: int
    skipped: bool


class SyncUniverse:
    """Populate/refresh the searchable universe from a live market screen."""

    # The market-cap floor that defines the universe: US companies worth at least $5B.
    MIN_MARKET_CAP = 5_000_000_000.0

    # Below this many screened names the result is treated as truncated or blocked (a
    # healthy US ≥$5B screen is ~1,000–1,300 names), so the reconcile is skipped — a bad
    # vendor day must delay new data, never delete the stored universe. The screener also
    # raises on a hard failure (which propagates); this guards a *degraded* success.
    MIN_PLAUSIBLE_SCREEN = 100

    def __init__(self, screener: StockScreener, repository: UniverseRepository) -> None:
        self._screener = screener
        self._repository = repository

    def execute(self) -> UniverseSyncReport:
        """Screen the market and reconcile the store to it.

        A hard screen failure (``StockDataUnavailable``) propagates to the caller (the
        cron endpoint maps it to a 502). A *degraded* screen — fewer than
        ``MIN_PLAUSIBLE_SCREEN`` names — is skipped so the stored universe survives a
        partial/blocked fetch. Otherwise the whole universe is in hand, so the repository
        reconciles: upsert present, remove absent.
        """
        screened = self._screener.screen(min_market_cap=self.MIN_MARKET_CAP)
        if len(screened) < self.MIN_PLAUSIBLE_SCREEN:
            return UniverseSyncReport(
                screened=len(screened), added=0, updated=0, removed=0, skipped=True
            )
        counts = self._repository.replace_universe(screened)
        return UniverseSyncReport(
            screened=len(screened),
            added=counts.added,
            updated=counts.updated,
            removed=counts.removed,
            skipped=False,
        )


class SearchStocks:
    """Use case: find universe stocks by ticker or company name."""

    DEFAULT_LIMIT = 20
    MAX_LIMIT = 100

    def __init__(self, repository: UniverseRepository) -> None:
        self._repository = repository

    def execute(
        self, query: str, *, limit: int | None = None
    ) -> tuple[ScreenedStock, ...]:
        """Return up to ``limit`` (default ``DEFAULT_LIMIT``, capped at ``MAX_LIMIT``)
        universe stocks matching ``query``, largest market cap first. Raises ``ValueError``
        for a blank query — searching for nothing is a client error, not an empty result."""
        normalized = (query or "").strip()
        if not normalized:
            raise ValueError("A search query is required.")
        capped = (
            self.DEFAULT_LIMIT
            if limit is None
            else max(1, min(limit, self.MAX_LIMIT))
        )
        return self._repository.search(normalized, limit=capped)
