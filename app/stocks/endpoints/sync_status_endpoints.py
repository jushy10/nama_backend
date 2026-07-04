"""HTTP API: ``GET /internal/sync/status`` — the pollable progress of every cron sweep.

Read-only. Reports each slice's in-process tracker (see ``sync_status.py``): whether a sweep is
running, how far along (``done`` / ``total``), the running ok/failed/skipped tallies, the last
symbol touched, and the run's start/finish stamps. One poll covers all slices, so a dashboard or
a human watching a refresh doesn't have to grep the logs.

Freshness/scope caveats it inherits from the tracker: in-process (resets on restart, correct
only while there is a single container) and, for the universe slice, ``total`` counts the
per-ticker *enrichment* pass — the bulk screen is one call, not a per-stock loop.

Security: unauthenticated like the other ``/internal/*`` endpoints (a shared ``CRON_SYNC_TOKEN``
guard is planned). This one only reads counters, so it leaks nothing sensitive.
"""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.stocks.endpoints.sync_status import all_snapshots

router = APIRouter(tags=["sync-status"])


class SyncStatusResponse(BaseModel):
    """One slice's sweep status. ``state`` is ``"idle"`` or ``"running"``; when idle the counts
    describe the last completed run. ``last_error`` is the exception repr if that run crashed."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    state: str
    limit: int | None
    total: int | None
    done: int
    ok: int
    failed: int
    skipped: int
    last_symbol: str | None
    started_at: str | None
    finished_at: str | None
    last_error: str | None


@router.get("/internal/sync/status", response_model=list[SyncStatusResponse])
def sync_status() -> list[SyncStatusResponse]:
    return [SyncStatusResponse.model_validate(snap) for snap in all_snapshots()]
