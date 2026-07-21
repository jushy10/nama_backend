from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from app.stocks.entities import normalize_symbol
from app.stocks.company.options.entities import ExpiryChain
from app.stocks.company.options.interfaces import OptionsChainAdapter


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


@dataclass(frozen=True)
class OptionsFlow:
    symbol: str
    expirations: tuple[date, ...]
    chain: ExpiryChain | None  # None only when the symbol lists no options


class GetOptionsFlow:
    def __init__(
        self,
        options: OptionsChainAdapter,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._options = options
        # Injectable clock: "nearest upcoming expiry" is anchored on today, and the tests
        # pin it the way the yfinance adapters pin theirs.
        self._today = today or date.today

    def execute(self, symbol: str, expiration: date | None = None) -> OptionsFlow:
        normalized = _normalize_symbol(symbol)
        expirations = self._options.get_expirations(normalized)  # primary; errors propagate
        if not expirations:
            return OptionsFlow(symbol=normalized, expirations=(), chain=None)

        target = self._select_expiration(expirations, expiration)
        chain = self._options.get_chain(normalized, target)
        return OptionsFlow(symbol=normalized, expirations=expirations, chain=chain)

    def _select_expiration(
        self, expirations: tuple[date, ...], requested: date | None
    ) -> date:
        if requested is not None:
            if requested not in expirations:
                raise ValueError(
                    f"{requested.isoformat()} is not a listed expiration for this symbol."
                )
            return requested
        today = self._today()
        upcoming = [e for e in expirations if e >= today]
        # Nearest upcoming expiry; if every listed expiry is already past (a stale feed),
        # fall back to the latest one rather than failing — there's still a chain to show.
        if upcoming:
            return min(upcoming, key=lambda e: e - today)
        return max(expirations)
