"""Interface Adapter: the database-backed ConstituentRepository.

The screener's universe — which symbols belong to which index, and each one's
GICS sector — lives in the ``index_constituents`` table rather than a bundled
file, so it can be refreshed without redeploying the app. The table is populated
out of band and only read at request time (the app never writes it while serving).

This module owns both the ORM model (the storage shape) and the repository that
maps rows onto the Constituent *entity*. The domain entity stays free of
SQLAlchemy; only this adapter knows the table exists.
"""

from sqlalchemy import Boolean, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.stocks.entities import Constituent, StockIndex
from app.stocks.ports import ConstituentRepository


class ConstituentRecord(Base):
    """One index constituent as stored in the database.

    Index membership is a boolean per index — the index set is small and fixed
    (the same StockIndex values the API exposes), so a column each keeps the
    table legible and trivially queryable. ``name``/``sector`` are nullable so a
    thinly-covered symbol still gets a row.
    """

    __tablename__ = "index_constituents"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    in_sp500: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    in_nasdaq100: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# Each StockIndex paired with the boolean column that records its membership, so
# the enum and the schema stay in lockstep — one place to extend for a new index.
INDEX_COLUMNS: dict[StockIndex, str] = {
    StockIndex.SP500: "in_sp500",
    StockIndex.NASDAQ100: "in_nasdaq100",
}


def _to_entity(row: ConstituentRecord) -> Constituent:
    indices = frozenset(
        index.value for index, column in INDEX_COLUMNS.items() if getattr(row, column)
    )
    return Constituent(
        symbol=row.symbol, name=row.name, sector=row.sector, indices=indices
    )


class SqlConstituentRepository(ConstituentRepository):
    """Reads the index-constituents universe from the database.

    Holds a request-scoped session (injected by the router via ``get_db``). The
    screener loads the whole table once per call and filters in memory: the
    universe is small (a few hundred rows) and the screener response is cached,
    so a single SELECT beats per-filter round-trips.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def all(self) -> tuple[Constituent, ...]:
        rows = self._session.execute(select(ConstituentRecord)).scalars().all()
        return tuple(_to_entity(row) for row in rows)
