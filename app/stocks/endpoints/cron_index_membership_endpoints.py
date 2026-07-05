"""HTTP API for invoking the index-membership refresh — the cron entrypoint.

The refresh is a use case (``SyncIndexMembership``) driven over HTTP: a scheduler (the
sync-index-membership GitHub workflow, or any cron) POSTs here to kick it off. The workflow now
launches this as a one-off ECS task via ``python -m app.sync index-membership`` (reusing
``run_index_membership_sync`` below), the same shape as the other sync sweeps; this endpoint
stays as the runner's source and for a manual/HTTP trigger.

The run is **fire-and-forget**, the same shape as the earnings/recommendations crons: it
schedules the work on a background thread and returns ``202`` at once, so it can't 504 at the
API Gateway's hard 30s integration timeout. The shared ``background_sync`` helper owns the
threading, the single-flight guard, and the exception handling (see it for the full rationale).

Unlike the per-symbol earnings sweeps there is **no stalest-N ``limit``**: the reconcile always
processes the whole membership set (it's a full mark/clear against both index lists), so the
endpoint exposes no ``limit`` query param and passes ``0`` to the shared helper. The ``limit: 0``
that then appears in the ``202`` body is a cosmetic artefact of the shared ``SyncTriggerResponse``
shape; it means nothing here.

Wiring lives here, the composition-root way: ``run_index_membership_sync`` opens a fresh session
and builds the Wikipedia adapter + the SQL repository for the use case. The membership source is
Wikipedia's S&P 500 / Nasdaq-100 rosters — **keyless** (this replaced Finnhub's paid, and
403-gated, ``/index/constituents``), so there is no credential to gate on and the sync is always
constructable, like the universe sweep. A source failure (e.g. Wikipedia unreachable, or a page
whose roster can't be parsed) surfaces later as a logged failure inside ``background_sync``, not
a startup error.

Security: this endpoint is currently **unauthenticated** — it writes the database (and hits
Wikipedia) and is triggered over the public internet by the sync workflow, so an auth token
(planned: a shared ``CRON_SYNC_TOKEN`` bearer guard) should be added before the endpoints are
considered hardened.
"""

import logging
import threading

from fastapi import APIRouter, Depends, Response, status

from app.db import SessionLocal
from app.stocks.adapters.wikipedia_index_membership_adapter import (
    WikipediaIndexMembershipProvider,
)
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.index_membership.db_repository import SqlIndexMembershipRepository
from app.stocks.index_membership.use_cases import (
    IndexMembershipSyncReport,
    SyncIndexMembership,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["index-membership-cron"])

# Single-flight guard for the index-membership reconcile only — independent of the other cron
# slices, which may run at the same time (a lock only stops a sweep overlapping itself).
_sync_lock = threading.Lock()


def run_index_membership_sync(_limit: int) -> IndexMembershipSyncReport:
    """Perform one full reconcile with its **own** DB session (the request-scoped ``get_db`` one
    is closed by the time the background thread runs). The ``_limit`` from the shared runner
    signature is ignored — this reconcile is always a full mark/clear against both index lists,
    not a stalest-N sweep. The Wikipedia source is keyless, so there's nothing to read from the
    env."""
    db = SessionLocal()
    try:
        # No per-item heartbeat here: this reconcile is two Finnhub list fetches plus fast flag
        # writes (seconds), not a thousands-of-stocks sweep — so a start line + the done line
        # below are the whole progress story.
        logger.info(
            "index-membership sync: fetching S&P 500 + Nasdaq-100 membership from Finnhub"
        )
        report = SyncIndexMembership(
            WikipediaIndexMembershipProvider(), SqlIndexMembershipRepository(db)
        ).execute()
        logger.info(
            "index-membership sync done: sp500 members=%d marked=%d cleared=%d skipped=%s | "
            "nasdaq100 members=%d marked=%d cleared=%d skipped=%s",
            report.sp500_members,
            report.sp500_marked,
            report.sp500_cleared,
            report.sp500_skipped,
            report.nasdaq100_members,
            report.nasdaq100_marked,
            report.nasdaq100_cleared,
            report.nasdaq100_skipped,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the reconcile's unit of work; tests override it with a fake.

    The Wikipedia source is keyless, so — unlike the old Finnhub wiring — there's no API key to
    gate on: the runner is always available (the same shape as the universe sweep)."""
    return run_index_membership_sync


@router.post(
    "/internal/index-membership/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_index_membership_endpoint(
    response: Response,
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the reconcile on a guarded background thread and return 202 at once,
    # or 200 "already_running" if one is already in flight. There's no stalest-N here, so the
    # shared helper's ``limit`` is passed as 0 (cosmetic). See background_sync.trigger_sync.
    return trigger_sync(_sync_lock, run, 0, response, label="index-membership sync")
