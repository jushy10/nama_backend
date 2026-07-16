"""purge the Cboe Canada (.NE) CDR rows from the stocks anchor

The multi-market universe sync used to screen the whole ``region == ca`` Yahoo universe, which
includes the **Cboe Canada (.NE)** venue — Canadian Depositary Receipts that wrap US / foreign
companies (``INTC.NE`` → Intel, ``ZAAP.NE`` → Apple, ``CHEV.NE`` → Chevron). Those got upserted
onto the ``stocks`` anchor. The search now hides them, and the sync no longer ingests them
(``SyncUniverse`` drops ``.NE`` before the upsert), but the rows already written are still in the
table — so this one-time cleanup deletes them.

A pure **data** migration (no schema change): ``DELETE FROM stocks WHERE ticker LIKE '%.NE'``.
Every child table's FK to ``stocks.id`` is ``ON DELETE CASCADE``, so any dependent rows (an
earnings / recommendations / news row a sync happened to write for a ``.NE`` ticker) are removed
with the anchor. ``LIKE '%.NE'`` matches only a true suffix — the literal ``.`` anchors it, so a
name like ``STONE`` is unaffected — and tickers are stored upper-case, so the case-sensitive
``LIKE`` is exact.

This is a deliberate one-off correction, not a change to the sync's additive contract (the sync
still never deletes; it simply stops *adding* ``.NE``). Not reversible — the deleted rows are
re-derivable from a screen, minus the now-excluded ``.NE``, so ``downgrade`` is a no-op.

Revision ID: 0041_drop_ne_cdr_rows
Revises: 0040_domicile_country
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0041_drop_ne_cdr_rows"
down_revision: Union[str, None] = "0040_domicile_country"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # FK children (ON DELETE CASCADE) go with the anchor row. `.NE` is the Cboe Canada suffix;
    # the literal `.` anchors the LIKE to a true suffix (so `STONE` etc. are untouched).
    op.execute(r"DELETE FROM stocks WHERE ticker LIKE '%.NE'")


def downgrade() -> None:
    # The universe screen re-derives every anchor row (minus the now-excluded `.NE`), so there's
    # nothing to restore.
    pass
