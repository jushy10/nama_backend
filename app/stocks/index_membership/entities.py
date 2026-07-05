"""Entities: the index-membership view of the market.

Slice-local domain object (this sub-slice keeps its own ``entities`` rather than reaching into
the shared ``app/stocks/entities.py``, the same convention as the universe, earnings and
recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

``IndexMembershipSnapshot`` is one point-in-time picture of which stocks belong to the tracked
indices, each as a set of tickers. It is the single shape the membership source returns and the
sync reconciles onto the ``stocks`` anchor.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexMembershipSnapshot:
    """The current membership of the tracked indices, each a set of normalized tickers.

    Tickers are upper-cased and use the ``-`` class-share separator (e.g. ``BRK-B``) — the same
    convention the ``stocks`` anchor stores — so the reconcile matches existing rows. An index
    that couldn't be fetched this run comes back as an **empty** set: the sync's plausibility
    floor treats that as "skip this index", never "clear every member".
    """

    sp500: frozenset[str]
    nasdaq100: frozenset[str]
