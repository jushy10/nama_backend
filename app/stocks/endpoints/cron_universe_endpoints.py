"""HTTP API for invoking the universe refresh — the cron entrypoint.

The refresh is a use case (``SyncUniverse``) invoked over HTTP, so a scheduler (the
sync-universe GitHub workflow, or any cron) drives it by POSTing here. The endpoint runs
the refresh synchronously and returns a small JSON summary.

Wiring lives here, the composition-root way: build the live Nasdaq screener adapter + the
SQL repository and hand them to the use case. Nasdaq's screener needs no API key, so
there's no credential to gate on; the sync is always constructable.

Unlike the earnings / recommendations crons this is a *single bulk* screen (one Nasdaq call
for the whole board) with no per-symbol vendor round-trips, so there's no batching / limit
knob — one POST refreshes the whole universe.

Security: this endpoint is currently **unauthenticated** — it writes the database (and hits
Nasdaq) and is triggered over the public internet by the sync workflow, so an auth token
(planned: a shared ``CRON_SYNC_TOKEN`` bearer guard) should be added before the endpoints
are considered hardened.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.nasdaq_screener_adapter import NasdaqScreenerProvider
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.db_repository import SqlUniverseRepository
from app.stocks.universe.use_cases import SyncUniverse, UniverseSyncReport

router = APIRouter(tags=["universe-cron"])


class UniverseSyncResponse(BaseModel):
    """The refresh run's summary: how many names the screen returned, the universe rows
    added / updated / removed by the reconcile, and ``skipped`` — ``true`` when the screen
    came back empty or implausibly small (a Nasdaq block) and the reconcile was skipped to
    leave the stored universe intact (the counts are then all zero)."""

    screened: int
    added: int
    updated: int
    removed: int
    skipped: bool


def get_sync_universe(db: Session = Depends(get_db)) -> SyncUniverse:
    # The refresh reads Nasdaq directly (not the DB it fills). Nasdaq's screener needs no
    # key, so there's nothing to gate on — the sync is always wired.
    return SyncUniverse(NasdaqScreenerProvider(), SqlUniverseRepository(db))


def _present(report: UniverseSyncReport) -> UniverseSyncResponse:
    """Presenter: use-case result -> HTTP response DTO."""
    return UniverseSyncResponse(
        screened=report.screened,
        added=report.added,
        updated=report.updated,
        removed=report.removed,
        skipped=report.skipped,
    )


@router.post("/internal/universe/sync", response_model=UniverseSyncResponse)
def sync_universe_endpoint(
    use_case: SyncUniverse = Depends(get_sync_universe),
) -> UniverseSyncResponse:
    # Runs synchronously: one bulk Nasdaq fetch + a batch of DB upserts. No per-symbol
    # vendor calls, so it stays well under a gateway timeout — but the caller should still
    # allow a generous idle timeout.
    try:
        report = use_case.execute()
    except StockDataUnavailable as exc:
        # A hard screen failure (transport / non-200 / bad shape). A merely *degraded*
        # screen doesn't raise — the use case skips it and reports skipped=true (a 200).
        raise HTTPException(502, str(exc)) from exc
    return _present(report)
