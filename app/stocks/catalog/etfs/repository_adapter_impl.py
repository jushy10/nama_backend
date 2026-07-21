from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, literal, nulls_last, or_, select
from sqlalchemy.orm import Session

from app.stocks.catalog.etfs import models
from app.stocks.catalog.etfs.entities import (
    EtfCategories,
    EtfHolding,
    EtfProfile,
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSearchResult,
    EtfSectorWeight,
    EtfSort,
    ScreenedEtf,
    SortDirection,
)
from app.stocks.catalog.etfs.models import (
    EtfRecord,
    EtfSectorWeightingRecord,
    EtfTopHoldingRecord,
    get_or_create_etf,
)
from app.stocks.catalog.etfs.interfaces import (
    EtfLookupRepositoryAdapter,
    EtfRepositoryAdapter,
    EtfSearchRepositoryAdapter,
    EtfSyncCounts,
)


class EtfRepositoryAdapterImpl(EtfRepositoryAdapter):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the screen stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def upsert_screen(self, etfs: tuple[ScreenedEtf, ...]) -> EtfSyncCounts:
        now = self._now()
        added = 0
        updated = 0
        for etf in etfs:
            row = get_or_create_etf(self._session, etf.ticker, etf.name)
            # A fund is "added" the first time the screen marks it (screened_at still null);
            # else it's an in-place refresh.
            if row.screened_at is None:
                added += 1
            else:
                updated += 1
            # Fill the identity fact when missing; never clobber a settled value (the name is
            # handled the same way inside get_or_create_etf).
            if etf.exchange and not row.exchange:
                row.exchange = etf.exchange
            # Refresh the drifting screen figures + freshness stamp on every run. Category is
            # left untouched here — the enrichment pass owns it.
            row.net_assets = etf.net_assets
            row.expense_ratio = etf.expense_ratio
            row.screened_at = now
        self._session.commit()
        return EtfSyncCounts(added=added, updated=updated)

    def profile_refresh_targets(self, limit: int | None) -> tuple[str, ...]:
        # Stalest-first (never-fetched ahead of stamped, then oldest refresh; ticker tiebreak) so a
        # capped, rate-limited run refreshes the funds most out of date and successive capped runs
        # round-robin the whole set. The ordering + limit live in models. ``limit=None`` sweeps all.
        return tuple(models.profile_refresh_targets(self._session, limit))

    def upsert_profile(self, ticker: str, profile: EtfProfile) -> None:
        etf = self._session.execute(
            select(EtfRecord).where(EtfRecord.ticker == ticker)
        ).scalar_one_or_none()
        if etf is None:
            return
        now = self._now()
        # Scalars: write each only when the fetch carried it, so a sparse/transient response never
        # clobbers a stored value with null. net_assets/expense_ratio are the screen's — untouched.
        if profile.category is not None:
            etf.category = profile.category
        if profile.fund_family is not None:
            etf.fund_family = profile.fund_family
        if profile.dividend_yield is not None:
            etf.dividend_yield = profile.dividend_yield
        if profile.description is not None:
            etf.description = profile.description
        if profile.nav is not None:
            etf.nav = profile.nav
        # The trailing-return ladder (ytd/3y/5y) is deliberately not persisted — the detail card
        # reads those live from Yahoo — so it's dropped here even though the fetch carries it.
        etf.profile_fetched_at = now
        # Child sets: replace wholesale only when the fetch returned rows; an empty list leaves the
        # stored rows intact (a blocked funds_data read must not wipe good holdings/sectors).
        if profile.sector_weightings:
            models.delete_sector_weightings_for_etf(self._session, etf.id)
            for weight in profile.sector_weightings:
                self._session.add(
                    EtfSectorWeightingRecord(
                        etf_id=etf.id,
                        sector=weight.sector,
                        weight=weight.weight,
                        fetched_at=now,
                    )
                )
        if profile.top_holdings:
            models.delete_top_holdings_for_etf(self._session, etf.id)
            for position, holding in enumerate(profile.top_holdings):
                self._session.add(
                    EtfTopHoldingRecord(
                        etf_id=etf.id,
                        position=position,
                        ticker=holding.ticker,
                        name=holding.name,
                        weight=holding.weight,
                        fetched_at=now,
                    )
                )
        self._session.commit()


# Each domain sort field → the ``etfs`` column it orders by. Both are nullable, so whichever is
# chosen gets wrapped in nulls_last below — a missing figure sorts to the bottom in either
# direction.
_SORT_COLUMNS = {
    EtfSort.NET_ASSETS: EtfRecord.net_assets,
    EtfSort.EXPENSE_RATIO: EtfRecord.expense_ratio,
    EtfSort.DIVIDEND_YIELD: EtfRecord.dividend_yield,
}


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_result(row: EtfRecord) -> EtfSearchResult:
    return EtfSearchResult(
        ticker=row.ticker,
        name=row.name,
        exchange=row.exchange,
        net_assets=row.net_assets,
        expense_ratio=row.expense_ratio,
        category=row.category,
        dividend_yield=row.dividend_yield,
    )


class EtfSearchRepositoryAdapterImpl(EtfSearchRepositoryAdapter):
    def __init__(self, session: Session) -> None:
        self._session = session

    def search(self, criteria: EtfSearchCriteria) -> EtfSearchPage:
        conditions = self._conditions(criteria)
        # Total match count, before the page window, so the client can render a pager.
        total = self._session.execute(
            select(func.count()).select_from(EtfRecord).where(*conditions)
        ).scalar_one()
        column = _SORT_COLUMNS[criteria.sort]
        ordering = (
            column.desc() if criteria.direction is SortDirection.DESC else column.asc()
        )
        rows = (
            self._session.execute(
                select(EtfRecord)
                .where(*conditions)
                # nulls_last so a fund missing the sort figure sinks to the bottom (either
                # direction); ticker as a stable tiebreak so offset paging over equal values
                # never skips or repeats a row.
                .order_by(nulls_last(ordering), EtfRecord.ticker.asc())
                .limit(criteria.limit)
                .offset(criteria.offset)
            )
            .scalars()
            .all()
        )
        return EtfSearchPage(
            results=tuple(_to_result(row) for row in rows),
            total=total,
            limit=criteria.limit,
            offset=criteria.offset,
        )

    def categories(self) -> EtfCategories:
        rows = (
            self._session.execute(
                select(EtfRecord.category)
                .where(EtfRecord.category.is_not(None))
                .distinct()
                .order_by(EtfRecord.category)
            )
            .scalars()
            .all()
        )
        return EtfCategories(categories=tuple(rows))

    def _conditions(self, criteria: EtfSearchCriteria) -> list:
        conditions: list = []
        if criteria.query:
            like = f"%{_escape_like(criteria.query)}%"
            # Match name OR ticker — so "gold" surfaces a gold-miners ETF (by name) and "SPY"
            # matches by ticker.
            conditions.append(
                or_(
                    EtfRecord.name.ilike(like, escape="\\"),
                    EtfRecord.ticker.ilike(like, escape="\\"),
                )
            )
        # Multi-select: match ANY of the chosen categories (an IN set — one value still renders a
        # plain `= :x`). An empty tuple adds no term (don't filter on category).
        if criteria.categories:
            conditions.append(EtfRecord.category.in_(criteria.categories))
        return conditions


class EtfLookupRepositoryAdapterImpl(EtfLookupRepositoryAdapter):
    def __init__(self, session: Session) -> None:
        self._session = session

    def is_etf(self, ticker: str) -> bool:
        # A single indexed existence probe on the unique ``ticker``: select a literal and cap at
        # one row so the DB stops at the first hit and never materialises the record — cheap
        # enough to run on every ticker-card request to set its ``asset_type``.
        hit = self._session.execute(
            select(literal(True)).where(EtfRecord.ticker == ticker).limit(1)
        ).scalar_one_or_none()
        return hit is not None

    def get(self, ticker: str) -> EtfSearchResult | None:
        row = self._session.execute(
            select(EtfRecord).where(EtfRecord.ticker == ticker)
        ).scalar_one_or_none()
        return None if row is None else _to_result(row)

    def get_stored_profile(self, ticker: str) -> EtfProfile:
        row = self._session.execute(
            select(EtfRecord).where(EtfRecord.ticker == ticker)
        ).scalar_one_or_none()
        if row is None:
            return EtfProfile.empty()
        sectors = tuple(
            EtfSectorWeight(sector=r.sector, weight=r.weight)
            for r in models.sector_weightings_for_etf(self._session, ticker)
        )
        holdings = tuple(
            EtfHolding(ticker=r.ticker, name=r.name, weight=r.weight)
            for r in models.top_holdings_for_etf(self._session, ticker)
        )
        # net_assets/expense_ratio deliberately left None — the detail resolves them from the
        # stored screen facts (``get``), not the profile (which never owned those columns). The
        # trailing returns (ytd/3y/5y) are likewise None here — no longer stored; the detail card
        # overlays them from a live Yahoo read when the performance block is requested.
        return EtfProfile(
            category=row.category,
            fund_family=row.fund_family,
            nav=row.nav,
            dividend_yield=row.dividend_yield,
            description=row.description,
            top_holdings=holdings,
            sector_weightings=sectors,
        )
