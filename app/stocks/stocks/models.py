"""Database model + queries for the shared ``stocks`` anchor.

The ``stocks`` table is the single row every per-feature table (the earnings
timelines, …) points at, so the same stock is one thing everyone references rather than
a symbol string copied around. It's owned by no single feature, so it gets its own slice
here. Feature slices import ``StockRecord`` + ``get_or_create_stock`` and add their own
child tables beside it. The schema is created by migration 0002 (the since-removed
analyst-estimates feature was the first to need the anchor); migration 0009 added
``exchange``, 0010 renamed the ``symbol`` column to ``ticker`` (the domain layers
still say "symbol" — the rename is a table-vocabulary choice), 0011 added the trailing
year-over-year growth columns, 0012 the three universe-screen columns, 0013 the
``industry`` column, 0014 the ``in_sp500`` / ``in_nasdaq100`` index-membership flags,
0017 the ``pe_ratio`` column, and 0018 the forward year-over-year growth columns (all below).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Row, String, Uuid, false, select
from sqlalchemy.orm import Mapped, Session, mapped_column


from app.db import Base


class StockRecord(Base):
    """A stock as stored in the database — the anchor per-feature tables reference.

    ``id`` is a surrogate UUID so child rows have a stable foreign key; ``ticker`` is
    what everything is looked up by (unique); ``name`` is the company display name and
    ``exchange`` the listing venue (e.g. "NASDAQ") — both nullable so a lazily-stored
    ticker (which arrives alone) still gets a row until whichever feature first learns
    them fills them in.

    ``revenue_growth_yoy`` / ``eps_growth_yoy`` are the stock's *latest trailing*
    year-over-year growth (percent) — the newest reported fiscal year over the one
    before it, written by the annual-earnings slice from its stored timeline. Unlike
    ``name``/``exchange`` (fill-once identity facts) these are a moving snapshot:
    they're **overwritten** on every annual refresh as the latest reported year rolls
    forward, so a stock carries exactly one pair (the current one), not a history. The
    EPS figure is on the analyst-consensus (adjusted) basis, matching the annual
    slice's ``eps_actual_consensus``. Nullable — unset until the annual slice has two
    reported years cached (and EPS best-effort, since the consensus basis often isn't).

    ``forward_revenue_growth_yoy`` / ``forward_eps_growth_yoy`` are the *forward* mirror
    of that pair — the analyst-consensus FY1 -> FY2 change (percent), feeding the universe
    search's forward-growth sorts and the AI analysis context. Written the same way (the
    annual slice overwrites both on every refresh from its stored forward years), and both
    legs sit on
    the consensus basis so neither carries a basis caveat. Nullable and more often unset
    than the trailing pair: they need *two* upcoming years and Yahoo frequently publishes
    only FY1 (0018).

    ``sector`` / ``industry`` / ``market_cap`` / ``screened_at`` are the universe screen's
    facts, filled by the universe sync (the ≥$1B US screen) and deliberately denormalized
    onto the anchor so search is a single-table read. All four are nullable: a ticker that
    reached the table some other way (a ticker-card lookup, an earnings refresh) has never
    been screened, so they stay null — which is exactly how search tells a screened company
    apart from an incidentally-known symbol (it filters on ``market_cap IS NOT NULL``).
    ``market_cap`` is whole dollars; ``screened_at`` is when the last screen that included
    the stock ran (the freshness stamp). ``sector`` and ``industry`` are the company's
    classification as snake_case slugs (e.g. ``technology`` / ``consumer_electronics``),
    filled once by the sync's enrichment pass from Yahoo's per-ticker ``.info`` — the bulk
    screen carries neither, so they lag the other screen facts until enrichment reaches the
    stock (and stay null for a symbol Yahoo doesn't classify).

    ``pe_ratio`` is the stock's trailing P/E on the analyst-consensus (adjusted) EPS basis —
    the same figure the ticker card computes live (``TickerValuation.trailing_pe``): a market
    price over the quarterly slice's TTM consensus EPS. It's written by the universe sync on
    the same sweep as ``market_cap`` (from the screen-time price the screen already carries),
    and like ``market_cap`` it's a *drifting, price-derived snapshot* — overwritten every run,
    not a fill-once fact. Nullable and null until the quarterly cache holds four reported
    quarters, and whenever the trailing year is a loss (a P/E off negative earnings is
    meaningless). It exists to make the universe *sortable* by valuation; the card still serves
    its own live P/E off the quote, so the two share a basis but not a freshness.

    ``in_sp500`` / ``in_nasdaq100`` are index-membership flags, reconciled by the
    index-membership sync (Finnhub → this anchor). Unlike the screen facts these are
    ``NOT NULL`` (default ``False``): membership is a known yes/no — absent from the
    source list means "not a member", not "unknown" — so every row carries a definite
    answer. The reconcile both *marks* current members and *clears* companies that dropped
    out of an index, so a stale flag never lingers.
    """

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
    screened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    in_sp500: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    in_nasdaq100: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )


def get_or_create_stock(
    session: Session, ticker: str, name: str | None
) -> StockRecord:
    """Return the ``stocks`` row for ``ticker``, creating it if absent.

    Fills a missing name when one is supplied, but never clobbers a known name with
    ``None`` — so whichever feature first learns the company name sets it, and a later
    nameless write (e.g. an earnings refresh) leaves it intact. The new row is flushed
    so its ``id`` is available for a child row in the same unit of work.
    """
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
    """The stored anchor columns the ticker card serves DB-first, in one query —
    ``None`` when the row doesn't exist yet.

    Returns a ``Row`` with named columns (``name``, ``exchange``, ``market_cap``,
    ``sector``, ``industry``, ``revenue_growth_yoy``, ``eps_growth_yoy``); per-field
    ``None`` for whatever the row hasn't learned. Widened past name/exchange because
    the card also serves the universe-screen facts and the annual slice's trailing
    growth straight off the anchor — the caller maps it into ``StoredTickerFacts``."""
    return session.execute(
        select(
            StockRecord.name,
            StockRecord.exchange,
            StockRecord.market_cap,
            StockRecord.sector,
            StockRecord.industry,
            StockRecord.revenue_growth_yoy,
            StockRecord.eps_growth_yoy,
        ).where(StockRecord.ticker == ticker)
    ).one_or_none()


def fill_exchange(session: Session, ticker: str, exchange: str) -> None:
    """Record ``ticker``'s listing exchange, creating the anchor row if absent.

    Same semantics as the name on ``get_or_create_stock``: fill when missing, never
    clobber a known value — an exchange effectively never changes, so the first
    feature to learn it settles it."""
    stock = get_or_create_stock(session, ticker, None)
    if not stock.exchange:
        stock.exchange = exchange
