"""The annual-earnings slice's composition root — the endpoint and the cron runner
call build_*(db) and receive a finished use case; all construction knowledge lives
here."""

from functools import lru_cache

from sqlalchemy.orm import Session

from app.adapters.db.db_cached_annual_earnings_adapter_impl import (
    AnnualEarningsAdapterImpl as DbCachedAnnualEarningsAdapterImpl,
)
from app.adapters.yfinance.annual_earnings_adapter_impl import (
    AnnualEarningsAdapterImpl as YfinanceAnnualEarningsAdapterImpl,
)
from app.domains.financials.earnings.annual.db_repository import (
    DbAnnualEarningsRepository,
)
from app.domains.financials.earnings.annual.interfaces import AnnualEarningsAdapter
from app.domains.financials.earnings.annual.use_cases import (
    GetAnnualEarnings,
    SyncAnnualEarnings,
)

# Pause between the sync's retry passes in production. The use case defaults this to 0
# (so the offline tests never sleep); here — the composition root — we dial it up so an
# intermittent Yahoo block has ~30s to lift before a blocked symbol is re-attempted. A
# batch run isn't behind the API Gateway's 30s clock (it's a one-off ECS task), so the
# added seconds are free.
_RETRY_BACKOFF_SECONDS = 30.0


@lru_cache(maxsize=1)
def get_live_annual_earnings_provider() -> AnnualEarningsAdapter:
    # One process-singleton live provider (no key, no connection pool to share); the DB
    # cache that wraps it is built per request, since it needs the request session.
    return YfinanceAnnualEarningsAdapterImpl()


def build_annual_earnings_provider(db: Session) -> AnnualEarningsAdapter:
    # A persistent DB cache (refreshed out of band by the annual-earnings cron + lazily
    # on a miss) sits in front of Yahoo so the read rarely calls it, and it serves
    # stored rows without a live round-trip. yfinance needs no key, so this is always
    # wired.
    return DbCachedAnnualEarningsAdapterImpl(
        get_live_annual_earnings_provider(), DbAnnualEarningsRepository(db)
    )


def build_get_annual_earnings(db: Session) -> GetAnnualEarnings:
    return GetAnnualEarnings(build_annual_earnings_provider(db))


def build_sync_annual_earnings(db: Session) -> SyncAnnualEarnings:
    # The sweep talks to Yahoo directly — refreshing the stored rows is its whole point.
    return SyncAnnualEarnings(
        get_live_annual_earnings_provider(),
        DbAnnualEarningsRepository(db),
        retry_backoff_seconds=_RETRY_BACKOFF_SECONDS,
    )
