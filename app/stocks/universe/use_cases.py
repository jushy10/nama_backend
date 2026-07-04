"""Application use cases for the universe slice.

One action, pure orchestration over the ports so it runs offline in tests against
hand-written fakes and knows nothing of Yahoo, HTTP, or SQLAlchemy:

- ``SyncUniverse`` — the out-of-band populator. Screens the US market at/above the floor
  and upserts the result onto the ``stocks`` anchor (additive: it never removes a stock).
  Invoked by the cron endpoint. Guarded so a blocked/truncated screen (empty or
  implausibly small) is skipped rather than churning a partial set.

The read/search path over the populated universe is **deferred** — there is no search
endpoint yet, only the sync that fills the anchor.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.universe.ports import StockScreener
from app.stocks.universe.repository import UniverseRepository


@dataclass(frozen=True)
class UniverseSyncReport:
    """The outcome of one sync run: the screen size and the anchors added / updated by the
    upsert, plus ``skipped`` — ``True`` when the upsert was deliberately not run because the
    screen came back empty or implausibly small (a truncated or blocked fetch), so nothing
    was written. When ``skipped`` is ``True`` the two counts are both zero. There is no
    ``removed`` count: the sync is additive (a shared anchor is never deleted)."""

    screened: int
    added: int
    updated: int
    skipped: bool


class SyncUniverse:
    """Populate/refresh the searchable universe from a live market screen."""

    # The market-cap floor that defines the universe: US companies worth at least $1B.
    MIN_MARKET_CAP = 1_000_000_000.0

    # Below this many screened names the result is treated as truncated or blocked (a
    # healthy US ≥$1B screen is ~2,800 names), so the upsert is skipped — a bad
    # vendor day shouldn't re-stamp only a partial slice as freshly screened. The screener
    # also raises on a hard failure (which propagates); this guards a *degraded* success.
    MIN_PLAUSIBLE_SCREEN = 100

    def __init__(self, screener: StockScreener, repository: UniverseRepository) -> None:
        self._screener = screener
        self._repository = repository

    def execute(self) -> UniverseSyncReport:
        """Screen the market and upsert the result onto the anchor.

        A hard screen failure (``StockDataUnavailable``) propagates to the caller (the cron
        endpoint maps it to a 502). A *degraded* screen — fewer than ``MIN_PLAUSIBLE_SCREEN``
        names — is skipped so a partial/blocked fetch isn't written. Otherwise the whole
        screen is upserted (additive: present stocks are inserted/refreshed, absent ones
        left untouched).
        """
        screened = self._screener.screen(min_market_cap=self.MIN_MARKET_CAP)
        if len(screened) < self.MIN_PLAUSIBLE_SCREEN:
            return UniverseSyncReport(
                screened=len(screened), added=0, updated=0, skipped=True
            )
        counts = self._repository.upsert_screen(screened)
        return UniverseSyncReport(
            screened=len(screened),
            added=counts.added,
            updated=counts.updated,
            skipped=False,
        )
