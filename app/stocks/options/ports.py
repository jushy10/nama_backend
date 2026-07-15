"""Port: the options-chain gateway the flow use case depends on.

The abstraction phrased in domain terms and returning the slice's entities — the same
convention as the other sub-slices' live-source ports. Two calls rather than one because
a symbol's full board is huge (hundreds of contracts across a dozen-plus expiries) and a
flow view reads one expiry at a time: the use case lists the expiries, picks the one to
show, and fetches only that chain.

This is deliberately a *separate* port from the ticker slice's ``OptionChainProvider``
(which returns that slice's leaner contract for the card's four summary metrics): this
one returns the flow slice's richer ``ExpiryChain`` — the contracts plus the underlying
spot the vendor reports alongside them — so the two slices stay independent and neither
imports the other's entities. A future paid OPRA/time-and-sales provider would implement
this same port, and only the adapter behind it would change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.stocks.options.entities import ExpiryChain


class OptionsChainProvider(ABC):
    """A gateway for a stock's listed options, one expiry at a time."""

    @abstractmethod
    def get_expirations(self, symbol: str) -> tuple[date, ...]:
        """Return the symbol's listed option expiration dates, ascending.

        Empty when the symbol has no listed options — "no coverage" is not an error.

        Raises:
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError

    @abstractmethod
    def get_chain(self, symbol: str, expiration: date) -> ExpiryChain:
        """Return one expiration's full chain (calls and puts) plus the underlying spot.

        The returned ``ExpiryChain`` may carry an empty ``contracts`` tuple when the
        expiry has no quotable contracts, and ``spot`` may be ``None`` when the feed omits
        the underlying quote — both are "thin coverage", not errors.

        Raises:
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
