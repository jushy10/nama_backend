"""Entities: the investable-universe view of a stock.

Slice-local domain object (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py``, the same convention as the earnings and
recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

``ScreenedStock`` is one row of the screened universe: the identity facts the ``stocks``
anchor holds (``ticker`` / ``name`` / ``exchange``) alongside the screen's own figures —
``market_cap`` (the selection criterion) and ``sector``. It is the single shape the
screener returns, the anchor persists, and a search result carries, so nothing has to
re-map between "a screened stock", "a stored anchor row", and "a search hit".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenedStock:
    """One company in the screened universe.

    ``market_cap`` is in whole dollars (e.g. ``3.01e12`` for a $3.01T company). Everything
    but the ``ticker`` is optional: ``exchange`` comes from the screen, ``sector`` may be
    absent (the yfinance screen doesn't publish it, so it rides in ``None``), and a stored
    row read back for search carries whatever the anchor holds.
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None
