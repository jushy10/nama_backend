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
from app.stocks.news.db_repository import SqlNewsRepository
from app.stocks.universe.db_repository import SqlStockSearchRepository
from app.stocks.wiring import bedrock_recovery_model_id, get_provider

logger = logging.getLogger(__name__)
router = APIRouter(tags=["market-brief-cron"])

# Single-flight guard for the brief generation only — independent of the other cron slices.
_sync_lock = threading.Lock()


def get_market_brief_provider() -> MarketBriefProvider:
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_MARKET_BRIEF_MODEL_ID")
    # The single incomplete-result retry escalates onto this model when set (else it
    # stays on the primary) — see wiring.bedrock_recovery_model_id.
    recovery = bedrock_recovery_model_id("BEDROCK_MARKET_BRIEF_RECOVERY_MODEL_ID")
    if model_id:
        return BedrockMarketBriefProvider(
            model_id=model_id, region=region, recovery_model_id=recovery
        )
    return BedrockMarketBriefProvider(region=region, recovery_model_id=recovery)


def run_market_brief_sync(limit: int | None) -> MarketBriefSyncReport:
    db = SessionLocal()
    try:
        provider = get_provider()  # the shared Alpaca singleton (boards + bulk quotes)
        use_case = GenerateDailyBrief(
            GetMarketOverview(provider),
            GetSectorPerformance(provider),
            GetStockHeatMap(SqlStockSearchRepository(db), provider),
            get_market_brief_provider(),
            SqlMarketBriefRepository(db),
            # DB-only news reader (never a live fetch) — the daily news sync keeps it warm,
            # so the movers' catalyst headlines cost the generation no extra vendor call.
            news=SqlNewsRepository(db),
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
