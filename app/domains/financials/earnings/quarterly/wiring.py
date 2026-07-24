"""The quarterly-earnings slice's composition root — the endpoint and the cron runner
call build_*(db) and receive a finished use case; all construction knowledge lives
here."""

from functools import lru_cache

from sqlalchemy.orm import Session

from app.adapters.db.db_cached_quarterly_earnings_adapter_impl import (
    QuarterlyEarningsAdapterImpl as DbCachedQuarterlyEarningsAdapterImpl,
)
from app.adapters.yfinance.quarterly_earnings_adapter_impl import (
    QuarterlyEarningsAdapterImpl as YfinanceQuarterlyEarningsAdapterImpl,
)
from app.domains.financials.earnings.quarterly.db_repository import (
    DbQuarterlyEarningsRepository,
)
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.domains.financials.earnings.quarterly.use_cases import (
    GetQuarterlyEarnings,
    SyncQuarterlyEarnings,
)

# Pause between the sync's retry passes in production. The use case defaults this to 0
# (so the offline tests never sleep); here — the composition root — we dial it up so an
# intermittent Yahoo block has ~30s to lift before a blocked symbol is re-attempted. A
# batch run isn't behind the API Gateway's 30s clock (it's a one-off ECS task), so the
# added seconds are free.
_RETRY_BACKOFF_SECONDS = 30.0


@lru_cache(maxsize=1)
def get_live_quarterly_earnings_provider() -> QuarterlyEarningsAdapter:
    # One process-singleton live provider (no key, no connection pool to share); the DB
    # cache that wraps it is built per request, since it needs the request session.
    return YfinanceQuarterlyEarningsAdapterImpl()


def build_quarterly_earnings_provider(db: Session) -> QuarterlyEarningsAdapter:
    # A persistent DB cache (refreshed out of band by the quarterly-earnings cron +
    # lazily on a miss) sits in front of Yahoo so the read rarely calls it, and it
    # serves stored rows without a live round-trip. yfinance needs no key, so this is
    # always wired. Also injected into the ticker card (the trailing P/E's TTM sum).
    return DbCachedQuarterlyEarningsAdapterImpl(
        get_live_quarterly_earnings_provider(), DbQuarterlyEarningsRepository(db)
    )


def build_get_quarterly_earnings(db: Session) -> GetQuarterlyEarnings:
    return GetQuarterlyEarnings(build_quarterly_earnings_provider(db))


def build_sync_quarterly_earnings(db: Session) -> SyncQuarterlyEarnings:
    # The sweep talks to Yahoo directly — refreshing the stored rows is its whole point.
    return SyncQuarterlyEarnings(
        get_live_quarterly_earnings_provider(),
        DbQuarterlyEarningsRepository(db),
        retry_backoff_seconds=_RETRY_BACKOFF_SECONDS,
    )
