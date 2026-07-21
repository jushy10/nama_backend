from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexMembershipSnapshot:
    sp500: frozenset[str]
    nasdaq100: frozenset[str]
