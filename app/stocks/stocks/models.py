from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Row, String, Uuid, false, select
from sqlalchemy.orm import Mapped, Session, mapped_column


from app.db import Base


class StockRecord(Base):
    __tablename__ = "stocks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revenue_growth_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_growth_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    forward_revenue_growth_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    forward_eps_growth_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    domicile_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    screened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    fcf_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocf_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    fcf_growth_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    fcf_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    operating_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_on_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    beta: Mapped[float | None] = mapped_column(Float, nullable=True)
    book_value_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    sales_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    dividend_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    ebitda: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_debt: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash_and_equivalents: Mapped[float | None] = mapped_column(Float, nullable=True)
    shares_outstanding: Mapped[float | None] = mapped_column(Float, nullable=True)
    ev_to_ebitda: Mapped[float | None] = mapped_column(Float, nullable=True)
    fundamentals_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    perf_one_week: Mapped[float | None] = mapped_column(Float, nullable=True)
    perf_one_month: Mapped[float | None] = mapped_column(Float, nullable=True)
    perf_three_month: Mapped[float | None] = mapped_column(Float, nullable=True)
    perf_six_month: Mapped[float | None] = mapped_column(Float, nullable=True)
    perf_ytd: Mapped[float | None] = mapped_column(Float, nullable=True)
    perf_one_year: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    in_sp500: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    in_nasdaq100: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    has_us_listing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )


def get_or_create_stock(
    session: Session, ticker: str, name: str | None
) -> StockRecord:
    stock = session.execute(
        select(StockRecord).where(StockRecord.ticker == ticker)
    ).scalar_one_or_none()
    if stock is None:
        stock = StockRecord(ticker=ticker, name=name)
        session.add(stock)
        session.flush()  # assign stock.id before a child row references it
    elif name and not stock.name:
        stock.name = name
    return stock


def anchor_facts(session: Session, ticker: str) -> Row | None:
    return session.execute(
        select(
            StockRecord.name,
            StockRecord.exchange,
            StockRecord.market_cap,
            StockRecord.sector,
            StockRecord.industry,
            StockRecord.revenue_growth_yoy,
            StockRecord.eps_growth_yoy,
            StockRecord.forward_revenue_growth_yoy,
            StockRecord.forward_eps_growth_yoy,
            StockRecord.fcf_per_share,
            StockRecord.ocf_per_share,
            StockRecord.fcf_growth_yoy,
            StockRecord.gross_margin,
            StockRecord.operating_margin,
            StockRecord.net_margin,
            StockRecord.return_on_equity,
            StockRecord.current_ratio,
            StockRecord.debt_to_equity,
            StockRecord.beta,
            StockRecord.book_value_per_share,
            StockRecord.sales_per_share,
            StockRecord.dividend_per_share,
            StockRecord.ebitda,
            StockRecord.total_debt,
            StockRecord.cash_and_equivalents,
            StockRecord.shares_outstanding,
        ).where(StockRecord.ticker == ticker)
    ).one_or_none()


def fill_exchange(session: Session, ticker: str, exchange: str) -> None:
    stock = get_or_create_stock(session, ticker, None)
    if not stock.exchange:
        stock.exchange = exchange
