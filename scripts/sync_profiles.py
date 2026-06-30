"""Refresh stored company profiles from the vendors (stale-first, quota-capped).

The stock endpoint fills a symbol's profile lazily the first time it's viewed (see
``DbCachedCompanyProfileProvider``); profiles barely change, so this job mostly exists
to keep the rare description/name edit from lingering. Name comes from Finnhub,
description from FMP — the latter against a ~250-calls/day free quota — so, like the
estimates sync, this refreshes only rows already stored, oldest-fetched first, up to
``SYNC_PROFILES_LIMIT`` per run (default 200). Run quarterly (see the workflow):

    export FINNHUB_API_KEY=...                      # the company name
    export FMP_API_KEY=...                          # the business description
    export DATABASE_URL=postgresql+psycopg://...    # omit for local sqlite:///./nama.db
    export SYNC_PROFILES_LIMIT=200                  # optional; rows to refresh per run
    alembic upgrade head                            # create the tables (once per DB)
    python scripts/sync_profiles.py

Needs the app installed and the `stocks` / `stock_company_profile` tables to exist
(created by `alembic upgrade head`). With neither vendor key set there's nothing to
refresh, so it exits cleanly.
"""

from __future__ import annotations

import os

from sqlalchemy import select

from app.db import SessionLocal
from app.stocks.composite_company_profile_provider import CompositeCompanyProfileProvider
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.finnhub_company_profile_provider import FinnhubCompanyProfileProvider
from app.stocks.fmp_profile_provider import FmpProfileProvider
from app.stocks.ports import CompanyProfileProvider
from app.stocks.stock_profile_repository import (
    SqlCompanyProfileRepository,
    StockCompanyProfileRecord,
)
from app.stocks.stock_record import StockRecord

_DEFAULT_LIMIT = 200


def _limit() -> int:
    """How many rows to refresh this run. Capped to stay under FMP's daily quota."""
    raw = os.environ.get("SYNC_PROFILES_LIMIT")
    if not raw:
        return _DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        raise SystemExit(f"SYNC_PROFILES_LIMIT must be an integer, got {raw!r}")


def _build_provider() -> CompanyProfileProvider:
    """The same Finnhub-name + FMP-description composite the endpoint uses."""
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    fmp_key = os.environ.get("FMP_API_KEY")
    name_source = FinnhubCompanyProfileProvider(finnhub_key) if finnhub_key else None
    description_source = FmpProfileProvider(fmp_key) if fmp_key else None
    if name_source is None and description_source is None:
        raise SystemExit(
            "Neither FINNHUB_API_KEY nor FMP_API_KEY is set — nothing to refresh "
            "profiles from. Set at least one before running."
        )
    return CompositeCompanyProfileProvider(name_source, description_source)


def main() -> None:
    limit = _limit()
    provider = _build_provider()
    refreshed = 0
    failed = 0

    with SessionLocal() as session:
        repo = SqlCompanyProfileRepository(session)
        # Oldest-fetched first, so each capped run renews the stalest rows; symbols
        # never viewed (hence never stored) are filled lazily by the endpoint.
        targets = session.execute(
            select(StockRecord.symbol)
            .join(
                StockCompanyProfileRecord,
                StockCompanyProfileRecord.stock_id == StockRecord.id,
            )
            .order_by(StockCompanyProfileRecord.fetched_at.asc())
            .limit(limit)
        ).all()

        for (symbol,) in targets:
            try:
                profile = provider.get_profile(symbol)
            except (StockNotFound, StockDataUnavailable) as exc:
                failed += 1
                print(f"  ! {symbol}: {exc}")
                continue
            repo.upsert(symbol, profile)
            refreshed += 1

    print(
        f"Refreshed {refreshed} profile rows ({failed} failed) "
        f"-> stock_company_profile (cap {limit})."
    )


if __name__ == "__main__":
    main()
