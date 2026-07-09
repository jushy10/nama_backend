"""Port: the options-chain gateway the ticker card's options read needs.

The abstraction the use case depends on, phrased in domain terms and returning the
slice's entities — the same convention as the other sub-slices' live-source ports.
Two calls rather than one because a full chain is huge (hundreds of contracts
across a dozen expiries) and the card needs exactly two slices of it: the use case
lists the expiries, picks its ~1-month and ~3-month windows, and fetches only those.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.stocks.ticker.entities import OptionContract, ReportedEps


class OptionChainProvider(ABC):
    """A gateway for a stock's listed options, one expiry at a time.

    Best-effort enrichment on the ticker card: the options read colors the entry
    decision but the card's reason to exist is the quote, so a failure here must
    not sink it.
    """

    @abstractmethod
    def get_expirations(self, symbol: str) -> tuple[date, ...]:
        """Return the symbol's listed option expiration dates, ascending.

        Empty when the symbol has no listed options — "no coverage" is not an
        error for best-effort enrichment.

        Raises:
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError

    @abstractmethod
    def get_chain(self, symbol: str, expiration: date) -> tuple[OptionContract, ...]:
        """Return every contract (calls and puts) for one expiration.

        Empty when the expiry has no quotable contracts.

        Raises:
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class EpsHistoryProvider(ABC):
    """A gateway for a stock's deep reported-EPS history — the trailing leg of the
    P/E-history walk.

    Distinct from the quarterly-earnings slice's provider (which serves a 4-recent +
    2-upcoming *timeline*): this returns as many *reported* quarters as the source
    publishes (~7 years), the depth a multi-year trailing-P/E series needs. Best-effort
    like the options read — the P/E history is a card-adjacent extra, and the live
    source (Yahoo) intermittently blocks data-centre IPs.
    """

    @abstractmethod
    def get_eps_history(self, symbol: str) -> tuple[ReportedEps, ...]:
        """Return the symbol's reported quarterly EPS, oldest first.

        Actuals only — quarters with no reported figure yet are omitted. Empty when the
        symbol has no earnings history (not an error for best-effort enrichment).

        Raises:
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
