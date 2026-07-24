"""The revenue-segments slice's composition root — the endpoint and the cron runner
call build_*(db) and receive a finished use case; all construction knowledge lives
here."""

from functools import lru_cache

from sqlalchemy.orm import Session

from app.adapters.db.db_cached_revenue_segments_adapter_impl import (
    RevenueSegmentsAdapterImpl as DbCachedRevenueSegmentsAdapterImpl,
)
from app.adapters.sec_edgar.revenue_segments_adapter_impl import (
    RevenueSegmentsAdapterImpl as SecEdgarRevenueSegmentsAdapterImpl,
)
from app.domains.financials.revenue_segments.db_repository import (
    DbRevenueSegmentsRepository,
)
from app.domains.financials.revenue_segments.interfaces import RevenueSegmentsAdapter
from app.domains.financials.revenue_segments.use_cases import (
    GetRevenueSegments,
    SyncRevenueSegments,
)

# Production pacing between the live provider's SEC requests: keeps a burst of cold-miss
# reads and the cron's serial walk under EDGAR's ~10 req/s fair-use ceiling.
_SEC_MIN_REQUEST_INTERVAL = 0.15


@lru_cache(maxsize=1)
def get_live_revenue_segments_provider() -> RevenueSegmentsAdapter:
    # One process-singleton live provider (no key; it caches the ticker->CIK map across
    # calls); the DB cache that wraps it is built per request, since it needs the
    # request session.
    return SecEdgarRevenueSegmentsAdapterImpl(
        min_request_interval_seconds=_SEC_MIN_REQUEST_INTERVAL
    )


def build_revenue_segments_provider(db: Session) -> RevenueSegmentsAdapter:
    # A persistent DB cache (refreshed out of band by the revenue-segments cron + lazily
    # on a miss) sits in front of EDGAR so the read rarely walks the filing. SEC needs
    # no key, so this is always wired.
    return DbCachedRevenueSegmentsAdapterImpl(
        get_live_revenue_segments_provider(), DbRevenueSegmentsRepository(db)
    )


def build_get_revenue_segments(db: Session) -> GetRevenueSegments:
    return GetRevenueSegments(build_revenue_segments_provider(db))


def build_sync_revenue_segments(db: Session) -> SyncRevenueSegments:
    # The sweep talks to EDGAR directly — refreshing the stored rows is its whole point.
    return SyncRevenueSegments(
        get_live_revenue_segments_provider(), DbRevenueSegmentsRepository(db)
    )
