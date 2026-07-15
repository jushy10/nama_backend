"""Interface Adapter: the SQLAlchemy-backed TickerRepository.

Implements the ``repository.py`` port against the database. The slice owns no table —
the facts it serves live on the shared ``stocks`` anchor, so this delegates entirely to
the anchor slice's query helpers (``app/stocks/stocks/models.py``; the name fill *is*
``get_or_create_stock``'s fill-but-never-clobber). It fills only name + exchange; the
universe-screen facts (``market_cap`` / ``sector`` / ``industry``) and the annual
slice's trailing growth are read-only reflections of those slices' writes. The saves
commit their own write so a successful lazy fill is durable independent of the
surrounding request.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.stocks.stocks import models
from app.stocks.ticker.repository import StoredTickerFacts, TickerRepository


class SqlTickerRepository(TickerRepository):
    """Reads and writes the anchor-level ticker facts through a request-scoped session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_facts(self, symbol: str) -> StoredTickerFacts:
        row = models.anchor_facts(self._session, symbol)
        if row is None:
            return StoredTickerFacts()  # no row yet -> every fact still unknown
        return StoredTickerFacts(
            name=row.name,
            exchange=row.exchange,
            market_cap=row.market_cap,
            sector=row.sector,
            industry=row.industry,
            revenue_growth_yoy=row.revenue_growth_yoy,
            eps_growth_yoy=row.eps_growth_yoy,
            forward_revenue_growth_yoy=row.forward_revenue_growth_yoy,
            forward_eps_growth_yoy=row.forward_eps_growth_yoy,
            fcf_per_share=row.fcf_per_share,
            ocf_per_share=row.ocf_per_share,
            fcf_growth_yoy=row.fcf_growth_yoy,
            gross_margin=row.gross_margin,
            operating_margin=row.operating_margin,
            net_margin=row.net_margin,
            return_on_equity=row.return_on_equity,
            current_ratio=row.current_ratio,
            debt_to_equity=row.debt_to_equity,
            beta=row.beta,
            book_value_per_share=row.book_value_per_share,
            sales_per_share=row.sales_per_share,
            dividend_per_share=row.dividend_per_share,
            ebitda=row.ebitda,
            total_debt=row.total_debt,
            cash_and_equivalents=row.cash_and_equivalents,
            shares_outstanding=row.shares_outstanding,
        )

    def save_name(self, symbol: str, name: str) -> None:
        models.get_or_create_stock(self._session, symbol, name)
        self._session.commit()

    def save_exchange(self, symbol: str, exchange: str) -> None:
        models.fill_exchange(self._session, symbol, exchange)
        self._session.commit()
