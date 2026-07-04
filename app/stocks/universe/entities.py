"""Entities: the investable-universe view of a stock.

Slice-local domain object (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py``, the same convention as the earnings and
recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

``ScreenedStock`` is one row of the screened universe: the identity facts the ``stocks``
anchor holds (``ticker`` / ``name`` / ``exchange``) alongside the two figures the screen
turns on — ``market_cap`` (the selection criterion) and ``sector``. It is the single shape
the screener returns, the store persists, and a search result carries, so nothing has to
re-map between "a screened stock", "a stored universe row", and "a search hit".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenedStock:
    """One company in the screened universe.

    ``market_cap`` is in whole dollars (e.g. ``3.01e12`` for a $3.01T company). Everything
    but the ``ticker`` is optional: the screen may omit a name/sector, exchange is filled
    separately (or lazily, later), and a stored row read back for search carries whatever
    the anchor and universe row hold.
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None
