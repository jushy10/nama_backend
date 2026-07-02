"""HTTP API for invoking the annual-earnings refresh — the cron entrypoint.

The refresh is a use case (``SyncAnnualEarnings``) invoked over HTTP, so a scheduler (the
sync-annual-earnings GitHub workflow, or any cron) drives it by POSTing here. The endpoint
runs the refresh synchronously and returns a small JSON summary.

Wiring lives here, the composition-root way: build the live yfinance adapter + the SQL
repository and hand them to the use case. yfinance reads Yahoo's public data with no API key,
so there's no credential to gate on; the sync is always constructable.

Security: the endpoint writes the database (and hits Yahoo) and is triggered over the
public internet by the sync workflow, so it's guarded by the shared bearer-token
dependency (``cron_auth.require_cron_token``) — enforced once ``CRON_SYNC_TOKEN`` is set
in the app's environment, open until then.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.adapters.yfinance_annual_earnings_adapter import (
    YfinanceAnnualEarningsProvider,
)
from app.stocks.earnings.annual.db_repository import SqlAnnualEarningsRepository
from app.stocks.earnings.annual.use_cases import (
    AnnualEarningsSyncReport,
    SyncAnnualEarnings,
)
from app.stocks.endpoints.cron_auth import require_cron_token

router = APIRouter(tags=["annual-earnings-cron"], dependencies=[Depends(require_cron_token)])


class AnnualEarningsSyncResponse(BaseModel):
    """The refresh run's summary: stocks renewed, stocks the vendor couldn't serve (or
    returned empty for) this run, and the per-run cap that was applied."""

    refreshed: int
    failed: int
    limit: int


def get_sync_annual_earnings(db: Session = Depends(get_db)) -> SyncAnnualEarnings:
    # The refresh reads Yahoo directly (not the DB cache it fills). yfinance needs no key, so
    # there's nothing to gate on — the sync is always wired.
    return SyncAnnualEarnings(
        YfinanceAnnualEarningsProvider(), SqlAnnualEarningsRepository(db)
    )


def _present(report: AnnualEarningsSyncReport) -> AnnualEarningsSyncResponse:
    """Presenter: use-case result -> HTTP response DTO."""
    return AnnualEarningsSyncResponse(
        refreshed=report.refreshed, failed=report.failed, limit=report.limit
    )


@router.post(
    "/internal/earnings/annual/sync", response_model=AnnualEarningsSyncResponse
)
def sync_annual_earnings_endpoint(
    limit: int = Query(
        SyncAnnualEarnings.DEFAULT_LIMIT,
        ge=1,
        le=1000,
        description=(
            "Max stored stocks to refresh this run, stalest first. Kept modest so the "
            "sequential Yahoo calls stay gentle on its rate limits."
        ),
    ),
    use_case: SyncAnnualEarnings = Depends(get_sync_annual_earnings),
) -> AnnualEarningsSyncResponse:
    # Runs synchronously: a few hundred sequential Yahoo calls can take a while, so the caller
    # (and any proxy / load balancer in front) must allow a long enough idle timeout, or pass
    # a smaller `limit`.
    report = use_case.execute(limit=limit)
    return _present(report)
