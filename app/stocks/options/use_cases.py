"""Application use case for the options-flow slice.

One action, pure orchestration over the ``OptionsChainProvider`` port so it runs offline
in tests against a hand-written fake and knows nothing of Yahoo or HTTP:

- ``GetOptionsFlow`` — list the symbol's expiries, pick the one to show (the caller's
  ``expiration`` if given, else the nearest upcoming), fetch just that chain, and bundle
  it with the full expiry list so the client can switch expiries without a second list
  call.

Unlike the earnings / news slices there is no sync counterpart and no persistence: an
options chain decays by the hour (prices, volume and open interest all move intraday),
so the no-TTL read-through-cache pattern the slow-moving slices use doesn't fit — the
read is live per request, exactly like the ticker card's ``options_metrics`` block. The
chain is this endpoint's *reason to exist*, so a vendor failure propagates (the endpoint
maps it to a 502); a symbol with simply no listed options is an empty flow (a 200), the
same "no data ≠ error" stance the rest of the feature takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from app.stocks.options.entities import ExpiryChain
from app.stocks.options.ports import OptionsChainProvider


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use
    case — so every layer below sees a clean symbol. Mirrors the other slices' guard."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


@dataclass(frozen=True)
class OptionsFlow:
    """Everything the options-flow endpoint serves: one expiry's chain and the full list
    of listed expiries.

    A thin composite of the slice's entities (like the ticker slice's ``TickerCard``),
    which is why it lives here with the orchestration rather than in ``entities.py`` — the
    flow logic proper lives on ``ExpiryChain``. ``chain`` is ``None`` when the symbol has
    no listed options at all; ``expirations`` is still served (empty then) so the client
    can render an empty selector rather than guess."""

    symbol: str
    expirations: tuple[date, ...]
    chain: ExpiryChain | None  # None only when the symbol lists no options


class GetOptionsFlow:
    """Use case: a stock's options-flow read for one expiration.

    The chain is the primary (and only) read — a vendor failure propagates so the
    endpoint can surface it as a 502. A symbol with no listed options returns an empty
    ``OptionsFlow`` (chain ``None``), not an error. When the caller names an
    ``expiration`` it must be one the symbol actually lists (else a 400 at the edge);
    with none named, the nearest expiry on or after today is chosen — the one a flow
    screen opens on.
    """

    def __init__(
        self,
        options: OptionsChainProvider,
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
