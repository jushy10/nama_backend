"""HTTP API for invoking the universe refresh — the cron entrypoint.

The refresh is a use case (``SyncUniverse``) invoked over HTTP, so a scheduler (the
sync-universe GitHub workflow, or any cron) drives it by POSTing here. The endpoint runs
the refresh synchronously and returns a small JSON summary.

Wiring lives here, the composition-root way: build the live yfinance screener adapter + the
SQL repository and hand them to the use case. Yahoo's screener needs no API key, so there's
no credential to gate on; the sync is always constructable.

Unlike the earnings / recommendations crons this makes no per-symbol vendor round-trips —
just a handful of paginated screen calls (yfinance pages the whole ≥$5B set 250 at a time,
~6 pages) followed by a batch of DB upserts — so there's no batching / limit knob: one POST
refreshes the whole universe.

Security: this endpoint is currently **unauthenticated** — it writes the database (and hits
Yahoo) and is triggered over the public internet by the sync workflow, so an auth token
(planned: a shared ``CRON_SYNC_TOKEN`` bearer guard) should be added before the endpoints
are considered hardened.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.yfinance_screener_adapter import YfinanceScreenerProvider
from app.stocks.exceptions import StockDataUnavailable
from app.stocks.universe.db_repository import SqlUniverseRepository
from app.stocks.universe.use_cases import SyncUniverse, UniverseSyncReport

router = APIRouter(tags=["universe-cron"])


class UniverseSyncResponse(BaseModel):
    """The refresh run's summary: how many names the screen returned, the anchors added /
    updated by the upsert, and ``skipped`` — ``true`` when the screen came back empty or
    implausibly small (a Yahoo block) and the upsert was skipped to avoid writing a partial
    set (the counts are then both zero). The sync is additive, so there is no ``removed``."""

    screened: int
    added: int
    updated: int
    skipped: bool


def get_sync_universe(db: Session = Depends(get_db)) -> SyncUniverse:
    # The refresh reads Yahoo directly (not the DB it fills). Yahoo's screener needs no key,
    # so there's nothing to gate on — the sync is always wired.
    return SyncUniverse(YfinanceScreenerProvider(), SqlUniverseRepository(db))


def _present(report: UniverseSyncReport) -> UniverseSyncResponse:
    """Presenter: use-case result -> HTTP response DTO."""
    return UniverseSyncResponse(
        screened=report.screened,
        added=report.added,
        updated=report.updated,
        skipped=report.skipped,
    )


@router.post("/internal/universe/sync", response_model=UniverseSyncResponse)
def sync_universe_endpoint(
    use_case: SyncUniverse = Depends(get_sync_universe),
) -> UniverseSyncResponse:
    # Runs synchronously: a few paginated screen fetches + a batch of DB upserts. No
    # per-symbol vendor calls, so it stays well under a gateway timeout — but the caller
    # should still allow a generous idle timeout.
    try:
        report = use_case.execute()
    except StockDataUnavailable as exc:
        # A hard screen failure (Yahoo block / bad payload). A merely *degraded* screen
        # doesn't raise — the use case skips it and reports skipped=true (a 200).
        raise HTTPException(502, str(exc)) from exc
    return _present(report)
