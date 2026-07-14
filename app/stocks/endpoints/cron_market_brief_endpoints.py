"""HTTP API for invoking the daily-market-brief generation — the cron entrypoint.

The generation is a use case (``GenerateDailyBrief``) driven over HTTP: a scheduler (the
sync-market-brief GitHub workflow, or any cron) POSTs here to kick it off. Unlike the
per-stock sweeps this is a **single unit of work** — gather the day's whole-market reads, ask
the model for a brief, and upsert today's row — so the ``limit`` the shared trigger machinery
carries is ignored (there's nothing to cap).

Fire-and-forget for the same reason as the other crons: the gather + model call runs past API
Gateway's hard 30s integration timeout, so the endpoint schedules it on a background thread and
returns ``202`` at once; the shared ``background_sync`` helper owns the threading, the
single-flight guard, and the exception handling. Re-running a day is safe — the upsert is keyed
by date, so a second run overwrites the day's row rather than duplicating it.

Wiring lives here, the composition-root way: ``run_market_brief_sync`` opens a fresh session and
builds the two Alpaca boards, the heat-map read, the Bedrock brief adapter, and the SQL store.
The Alpaca singleton needs the ``APCA_*`` keys (its usual 503 gate) and Bedrock needs the
process's AWS credentials + the ``bedrock`` extra; on the ECS sync task both are present.

Security: the trigger is guarded by the shared bearer token (``require_cron_token``) — an unset
token is a ``503``, a missing/wrong one a ``401``. The GitHub workflow doesn't POST here (it runs
the generation as a one-off ECS task via ``python -m app.sync market-brief``), so this guard only
gates the manual / HTTP trigger.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response, status

from app.db import SessionLocal
from app.stocks.adapters.bedrock.market_brief_adapter import BedrockMarketBriefProvider
from app.stocks.brief.db_repository import SqlMarketBriefRepository
from app.stocks.brief.ports import MarketBriefProvider
from app.stocks.brief.use_cases import (
    GenerateDailyBrief,
    MarketBriefSyncReport,
)
from app.stocks.endpoints.background_sync import (
    SyncRunner,
    SyncTriggerResponse,
    trigger_sync,
)
from app.stocks.endpoints.cron_auth import require_cron_token
from app.stocks.heatmap.use_cases import GetStockHeatMap
from app.stocks.market.use_cases import GetMarketOverview, GetSectorPerformance
from app.stocks.universe.db_repository import SqlStockSearchRepository
from app.stocks.wiring import get_provider

logger = logging.getLogger(__name__)
router = APIRouter(tags=["market-brief-cron"])

# Single-flight guard for the brief generation only — independent of the other cron slices.
_sync_lock = threading.Lock()


def get_market_brief_provider() -> MarketBriefProvider:
    """Build the Bedrock brief adapter from deploy-time config.

    ``BEDROCK_REGION`` (shared) + ``BEDROCK_MARKET_BRIEF_MODEL_ID`` (this analyser's optional
    override) the composition-root way; a missing ``bedrock`` extra surfaces as the adapter's
    ``ImportError``, which the runner logs as a failed generation."""
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_MARKET_BRIEF_MODEL_ID")
    if model_id:
        return BedrockMarketBriefProvider(model_id=model_id, region=region)
    return BedrockMarketBriefProvider(region=region)


def run_market_brief_sync(limit: int | None) -> MarketBriefSyncReport:
    """Generate and store today's brief with its **own** DB session (the request-scoped
    ``get_db`` one is closed by the time the background thread runs). ``limit`` is ignored —
    the brief is a single unit of work, not a per-stock sweep."""
    db = SessionLocal()
    try:
        provider = get_provider()  # the shared Alpaca singleton (boards + bulk quotes)
        use_case = GenerateDailyBrief(
            GetMarketOverview(provider),
            GetSectorPerformance(provider),
            GetStockHeatMap(SqlStockSearchRepository(db), provider),
            get_market_brief_provider(),
            SqlMarketBriefRepository(db),
        )
        brief = use_case.execute()
        report = MarketBriefSyncReport(
            generated=brief is not None,
            brief_date=(
                brief.brief_date
                if brief is not None
                else datetime.now(timezone.utc).date()
            ),
        )
        logger.info(
            "market-brief sync done: generated=%s date=%s",
            report.generated,
            report.brief_date,
        )
        return report
    finally:
        db.close()


def get_sync_runner() -> SyncRunner:
    """DI seam for the generation's unit of work; tests override it with a fake."""
    return run_market_brief_sync


@router.post(
    "/internal/market-brief/sync",
    response_model=SyncTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_cron_token)],
)
async def sync_market_brief_endpoint(
    response: Response,
    limit: int | None = Query(
        None,
        ge=1,
        description="Ignored — the brief is a single unit of work, not a per-stock sweep.",
    ),
    run: SyncRunner = Depends(get_sync_runner),
) -> SyncTriggerResponse:
    # Fire-and-forget: start the generation on a guarded background thread and return 202 at
    # once, or 200 "already_running" if one is already in flight.
    return trigger_sync(_sync_lock, run, limit, response, label="market-brief sync")
