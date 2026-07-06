"""Application use cases for the ETF slice.

Pure orchestration over the ports so each runs offline in tests against hand-written fakes and
knows nothing of Yahoo, HTTP, or SQLAlchemy:

- ``SyncEtfs`` — the out-of-band populator. Screen the top US ETFs and upsert the result into
  the ``etfs`` table (additive: it never removes a fund). Invoked by the (fire-and-forget) cron
  endpoint. Guarded so a blocked/truncated screen (empty or implausibly small) skips the write
  rather than churning a partial set. One pass — unlike the stock universe there's no per-ticker
  enrichment (the ETF screen already carries every fact we store).
- ``SearchEtfs`` — the read side (``GET /stocks/etfs``): normalize a search request at the edge
  and hand the read repository a clean ``EtfSearchCriteria``, returning the matched page. No
  live feed — the set is already in the table.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.etfs.entities import (
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSort,
    SortDirection,
)
from app.stocks.etfs.ports import EtfScreener
from app.stocks.etfs.repository import EtfRepository, EtfSearchRepository


@dataclass(frozen=True)
class EtfSyncReport:
    """The outcome of one sync run.

    ``screened`` is the screen size and ``added`` / ``updated`` the rows the upsert inserted /
    refreshed. ``skipped`` is ``True`` when the screen came back empty or implausibly small (a
    truncated or blocked fetch) so *nothing* was written; the counts are then all zero. There is
    no ``removed`` count — the sync is additive.
    """

    screened: int
    added: int
    updated: int
    skipped: bool


class SyncEtfs:
    """Populate/refresh the searchable ETF set from a live top-ETFs screen."""

    # Below this many screened funds the result is treated as truncated or blocked (a healthy
    # top-ETFs screen is ~540), so the upsert is skipped — a bad Yahoo day shouldn't re-stamp
    # only a partial slice as freshly screened. The screener also raises on a hard failure
    # (which propagates); this guards a *degraded* success.
    MIN_PLAUSIBLE_SCREEN = 50

    def __init__(self, screener: EtfScreener, repository: EtfRepository) -> None:
        self._screener = screener
        self._repository = repository

    def execute(self) -> EtfSyncReport:
        """Screen the top ETFs and upsert the result into the ``etfs`` table.

        A hard screen failure (``StockDataUnavailable``) propagates to the caller (the
        background runner logs it). A *degraded* screen — fewer than ``MIN_PLAUSIBLE_SCREEN``
        funds — is skipped so a partial/blocked fetch isn't written. Otherwise the whole screen
        is upserted (additive).
        """
        screened = self._screener.screen()
        if len(screened) < self.MIN_PLAUSIBLE_SCREEN:
            return EtfSyncReport(
                screened=len(screened), added=0, updated=0, skipped=True
            )
        counts = self._repository.upsert_screen(screened)
        return EtfSyncReport(
            screened=len(screened),
            added=counts.added,
            updated=counts.updated,
            skipped=False,
        )


class SearchEtfs:
    """Search/filter/sort the stored ETF set for the ``GET /stocks/etfs`` list.

    Pure orchestration over the read repository: normalize the request once at the edge, hand
    the repository a clean ``EtfSearchCriteria``, return the page it matches. No live feed, no
    vendor — the set is already stored by the sync.
    """

    # The default page size, and the ceiling a client can ask for. The endpoint enforces the
    # same bounds on its query param; the use case clamps too, so a direct caller (or a test)
    # can't ask for an unbounded or zero page.
    DEFAULT_LIMIT = 25
    MAX_LIMIT = 100

    def __init__(self, repository: EtfSearchRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        query: str | None = None,
        sort: EtfSort = EtfSort.NET_ASSETS,
        direction: SortDirection = SortDirection.DESC,
        limit: int | None = None,
        offset: int = 0,
    ) -> EtfSearchPage:
        """Normalize the inputs once, at the edge, then run the search.

        ``query`` is trimmed (blank → no text filter); ``limit`` defaults to ``DEFAULT_LIMIT``
        and is clamped to ``[1, MAX_LIMIT]``, ``offset`` floored at 0. The sort/direction pass
        through as-is (already validated enums). The repository does the rest.
        """
        text = (query or "").strip()
        capped = self.DEFAULT_LIMIT if limit is None else min(max(1, limit), self.MAX_LIMIT)
        criteria = EtfSearchCriteria(
            query=text or None,
            sort=sort,
            direction=direction,
            limit=capped,
            offset=max(0, offset),
        )
        return self._repository.search(criteria)
