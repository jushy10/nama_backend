"""Application ports: the kernel abstractions many slices depend on.

This is the Dependency Inversion that makes the slice clean: a use case depends
on these interfaces, and the adapter layer provides the concrete (Alpaca-backed,
DB-backed, …) implementation. The core never imports a vendor; the vendor
imports the core.

Only the *shared* snapshot-and-price capabilities live here — the ones several
slices consume. A port used by exactly one sub-slice lives in that slice's own
``ports.py`` (candles in ``charts``, the sector/index boards in ``market``, the
logo read in ``logo``, and every AI-analysis port in ``analysis``).
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    Quote,
    Stock,
    StockPerformance,
)


class StockDataProvider(ABC):
    """A gateway for retrieving stock data from some external source."""

    @abstractmethod
    def get_stock(self, symbol: str) -> Stock:
        """Return a Stock for the given (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol does not exist / has no data.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class StockQuoteProvider(ABC):
    """A gateway for a stock's minimal live quote (price + day change).

    Separate from StockDataProvider because this backs a high-frequency polling
    endpoint: it returns only the snapshot-derived quote and skips the company
    metadata lookup, so a client refreshing every few seconds stays cheap.
    """

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote for the (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol does not exist / has no data.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class BulkQuoteProvider(ABC):
    """A gateway for many symbols' live quotes in one call — the batched cousin of
    ``StockQuoteProvider``.

    Backs a view that colours a whole board by the day's move (the heat map): one request for
    the entire symbol list instead of N per-symbol calls. **Best-effort per symbol** — a symbol
    the feed carries no quote for (e.g. not on the free IEX feed) is simply *absent* from the
    returned map, never an error, so the caller can size that tile from stored facts and leave
    it uncoloured. Only a hard feed failure over the whole batch is fatal.
    """

    @abstractmethod
    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Return the latest quote for each recognized symbol, keyed by symbol.

        Symbols the feed has no quote for are omitted (a partial map is normal, not an error);
        order and duplicates in the input don't matter. Given an empty input, returns an empty
        map without a call.

        Raises:
            StockDataUnavailable: the upstream feed failed for the whole request.
        """
        raise NotImplementedError


class StockPerformanceProvider(ABC):
    """A gateway for a stock's trailing price-return over standard windows.

    Separate from StockDataProvider: performance is derived from price history
    rather than the live snapshot, and the endpoint treats it as best-effort
    enrichment, so a failure here must not sink the price response.
    """

    @abstractmethod
    def get_performance(self, symbol: str) -> StockPerformance:
        """Return trailing-window performance for the (normalized) symbol.

        Raises:
            StockNotFound: the symbol has no price history.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class BulkPerformanceProvider(ABC):
    """A gateway for many symbols' trailing performance in one batched read — the bulk
    cousin of ``StockPerformanceProvider``.

    Backs the heat map's timeframe windows: instead of colouring the board only by the
    day's move, each tile also carries its trailing return over the standard windows
    (1W…1Y, YTD), computed once for the whole index rather than N per-symbol calls.
    **Best-effort per symbol** — a symbol the feed has no history for (e.g. not on the
    historical feed, or too newly listed) is simply *absent* from the returned map, so
    the caller leaves that tile's trailing windows blank; only a hard feed failure over
    the whole batch is fatal.
    """

    @abstractmethod
    def get_bulk_performance(
        self, symbols: Sequence[str]
    ) -> dict[str, StockPerformance]:
        """Return trailing-window performance for each recognized symbol, keyed by symbol.

        Symbols the feed has no history for are omitted (a partial map is normal, not an
        error); order and duplicates in the input don't matter. Given an empty input,
        returns an empty map without a call.

        Raises:
            StockDataUnavailable: the upstream feed failed for the whole request.
        """
        raise NotImplementedError


class AllTimeHighProvider(ABC):
    """A gateway for a stock's all-time high over its available price history.

    Derived from the full span of daily bars rather than the live snapshot, like
    trailing performance — and likewise best-effort enrichment on the stock view,
    so a failure here must not sink the price response. "All-time" is bounded by
    how far back the source's history reaches (surfaced on the returned entity).
    """

    @abstractmethod
    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        """Return the all-time high for the (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol has no price history.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class AnalystEstimatesProvider(ABC):
    """A gateway for a stock's forward analyst consensus estimates.

    Forward EPS/revenue expectations come from an estimates source — not the
    price feed or company filings — so this carries consensus *estimates*, never
    reported actuals. Best-effort enrichment on the stock snapshot (it backs the
    forward P/E and forward P/S), so a failure here must not sink the price response.
    """

    @abstractmethod
    def get_estimates(self, symbol: str) -> AnalystEstimates:
        """Return forward consensus estimates for the (already-normalized) symbol.

        Returns an ``is_empty`` ``AnalystEstimates`` (all ``None``) when the source
        covers no forward fiscal year for the symbol — "no data" is not an error for
        best-effort enrichment.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
