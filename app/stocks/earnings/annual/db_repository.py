from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.earnings.annual import models
from app.stocks.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.earnings.annual.models import StockAnnualEarningsRecord
from app.stocks.earnings.annual.repository import (
    AnnualEarningsRepository,
    RefreshTarget,
)


def _to_entity(row: StockAnnualEarningsRecord) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=row.fiscal_year,
        period_end=row.period_end,
        eps_actual=row.eps_actual,
        eps_estimate=row.eps_estimate,
        revenue_actual=row.revenue_actual,
        revenue_estimate=row.revenue_estimate,
        net_income=row.net_income,
        eps_actual_consensus=row.eps_actual_consensus,
        fcf_per_share=row.fcf_per_share,
        ocf_per_share=row.ocf_per_share,
    )


def _to_timeline(
    symbol: str, rows: list[StockAnnualEarningsRecord]
) -> AnnualEarningsTimeline:
    years = sorted((_to_entity(row) for row in rows), key=lambda y: y.fiscal_year)
    return AnnualEarningsTimeline(symbol=symbol, years=tuple(years))


class SqlAnnualEarningsRepository(AnnualEarningsRepository):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> AnnualEarningsTimeline | None:
        rows = models.years_by_symbol(self._session, symbol)
        if not rows:
            return None
        return _to_timeline(symbol, rows)

    def upsert(
        self, symbol: str, name: str | None, timeline: AnnualEarningsTimeline
    ) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Persist the latest trailing + forward YoY snapshots on the shared anchor. Unlike
        # the fill-once name/exchange, these are *overwritten* every refresh — the reported
        # window and the forward consensus both roll forward, so the snapshots are meant to
        # move (each drops back to None when the window can't support it: fewer than two
        # reported years for trailing, fewer than two upcoming for forward). The entity owns
        # every calc; this layer just lands them on the row. Every write path (cron sync +
        # lazy fill) funnels through here, so both keep them current.
        stock.revenue_growth_yoy = timeline.latest_revenue_growth_yoy
        stock.eps_growth_yoy = timeline.latest_eps_growth_yoy
        stock.forward_revenue_growth_yoy = timeline.forward_revenue_growth_yoy
        stock.forward_eps_growth_yoy = timeline.forward_eps_growth_yoy
        # The free/operating cash-flow per share (newest reported year) and the trailing
        # FCF-per-share growth — the same overwrite-every-refresh snapshot as the growth
        # pair. The per-share cash figures feed the ticker card's live FCF multiples
        # (priced on the quote, not stored); fcf_growth_yoy is served directly. Each drops
        # to None when the window can't support it (no reported year / fewer than two).
        stock.fcf_per_share = timeline.latest_fcf_per_share
        stock.ocf_per_share = timeline.latest_ocf_per_share
        stock.fcf_growth_yoy = timeline.latest_fcf_growth_yoy

        # Rewrite the whole window: clear the stock's rows, then insert the new set.
        # Simpler and correct for a variable-length set of years than diffing.
        models.delete_years_for_stock(self._session, stock.id)
        now = self._now()
        for year in timeline.years:
            self._session.add(
                StockAnnualEarningsRecord(
                    stock_id=stock.id,
                    fiscal_year=year.fiscal_year,
                    period_end=year.period_end,
                    eps_actual=year.eps_actual,
                    eps_estimate=year.eps_estimate,
                    revenue_actual=year.revenue_actual,
                    revenue_estimate=year.revenue_estimate,
                    net_income=year.net_income,
                    eps_actual_consensus=year.eps_actual_consensus,
                    fcf_per_share=year.fcf_per_share,
                    ocf_per_share=year.ocf_per_share,
                    fetched_at=now,
                )
            )
        self._session.commit()

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        # Delegates the query to models (un-cached first, then stalest); this layer just
        # wraps each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
