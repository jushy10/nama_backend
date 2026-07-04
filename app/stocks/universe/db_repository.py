"""Interface Adapter: the SQLAlchemy-backed UniverseRepository.

Implements ``repository.py`` against the shared ``stocks`` anchor — the universe has no
table of its own, so the screen is written straight onto ``stocks`` (ticker/name/exchange
plus the denormalized ``sector``/``industry``/``market_cap``/``screened_at`` columns). Maps
``ScreenedStock`` / ``CompanyClassification`` entities onto anchor rows; only this layer
touches SQLAlchemy. ``upsert_screen`` (the screen) and ``set_classification`` (the per-ticker
enrichment) each commit their own write, so a successful — or partial — sync is durable
independent of the request. (There is no read/search side yet — that endpoint is deferred.)
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import nulls_last, or_, select
from sqlalchemy.orm import Session

from app.stocks.stocks.models import StockRecord, get_or_create_stock
from app.stocks.universe.entities import CompanyClassification, ScreenedStock
from app.stocks.universe.repository import UniverseRepository, UniverseSyncCounts


class SqlUniverseRepository(UniverseRepository):
    """Writes the universe through a request-scoped session, onto the ``stocks`` anchor.
    ``upsert_screen`` commits its own write so a successful sync is durable independent of
    the surrounding request.
    """

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the screen stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def upsert_screen(
        self, stocks: tuple[ScreenedStock, ...]
    ) -> UniverseSyncCounts:
        now = self._now()
        added = 0
        updated = 0
        for stock in stocks:
            anchor = get_or_create_stock(self._session, stock.ticker, stock.name)
            # A stock is "added" the first time the screen marks it (screened_at still
            # null) — whether the anchor is brand new or predates the screen; else it's an
            # in-place refresh.
            if anchor.screened_at is None:
                added += 1
            else:
                updated += 1
            # Fill identity facts when missing; never clobber a settled value (the same
            # rule get_or_create_stock applies to the name).
            if stock.exchange and not anchor.exchange:
                anchor.exchange = stock.exchange
            if stock.sector and not anchor.sector:
                anchor.sector = stock.sector
            # Refresh the drifting screen facts + freshness stamp on every run.
            anchor.market_cap = stock.market_cap
            anchor.screened_at = now
        self._session.commit()
        return UniverseSyncCounts(added=added, updated=updated)

    def tickers_missing_classification(self, limit: int) -> tuple[str, ...]:
        # Missing *either* side: a stock is on the work-list until both sector and industry
        # are filled, so a one-sided classification (Yahoo returned only industry, say) gets
        # revisited instead of being stuck with a null sector forever — set_classification is
        # fill-once per side, so a later run completes it.
        #
        # Largest market cap first (ticker as a stable tiebreak) so a capped, rate-limited
        # run spends its scarce successful .info calls on the biggest, most-viewed names —
        # a megacap like NVDA/GOOGL is classified in the first run rather than starved
        # behind thousands of alphabetically-earlier small caps. A non-screened incidental
        # ticker (market_cap NULL) sorts last, after every screened member.
        rows = (
            self._session.execute(
                select(StockRecord.ticker)
                .where(
                    or_(
                        StockRecord.industry.is_(None),
                        StockRecord.sector.is_(None),
                    )
                )
                .order_by(nulls_last(StockRecord.market_cap.desc()), StockRecord.ticker)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return tuple(rows)

    def set_classification(
        self, ticker: str, classification: CompanyClassification
    ) -> None:
        stock = self._session.execute(
            select(StockRecord).where(StockRecord.ticker == ticker)
        ).scalar_one_or_none()
        if stock is None:
            return
        # Fill-once per side: write only what the source supplies and the column still lacks,
        # so a settled value survives and a one-sided classification leaves room for the rest.
        if classification.industry and not stock.industry:
            stock.industry = classification.industry
        if classification.sector and not stock.sector:
            stock.sector = classification.sector
        self._session.commit()
