"""Application use case for the ticker slice.

One action, pure orchestration over existing ports so it runs offline in tests
against hand-written fakes and knows nothing of Alpaca, Finnhub, the database, or
HTTP:

- ``GetTickerCard`` — the read path. Normalizes the symbol, takes the live quote
  through the ``StockQuoteProvider`` and the stored forward consensus through the
  ``AnalystEstimatesProvider`` (wired in production as the annual-earnings
  projection, DB-only), then layers best-effort enrichment on top: company
  fundamentals (market cap, dividend) and the trailing performance windows.
  Returns the assembled ``TickerCard``.

Unlike the earnings/recommendations slices there is no sync counterpart and no
repository: the card is built around the *live* quote, so nothing slice-owned is
worth persisting — the slow-moving half (the FY1/FY2 consensus) is already stored
and refreshed by the annual-earnings slice, and the fast-moving half (the quote)
must be fetched fresh anyway.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.entities import Quote, StockFundamentals, StockPerformance
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    AnalystEstimatesProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.ticker.entities import TickerValuation


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use
    case — so every layer below sees a clean symbol. Mirrors the stocks slice's guard."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


@dataclass(frozen=True)
class TickerCard:
    """Everything the ticker endpoint serves, assembled from the ports.

    A composition of shared entities rather than a new domain concept — which is
    why it lives here with the orchestration (like the sync slices' report
    dataclasses) instead of in the slice's ``entities.py``: the slice-local entity
    (``TickerValuation``) owns the forward-PEG rule, and this just bundles it with
    the quote and the best-effort enrichment blocks (``None`` when their provider
    is unconfigured or failed — enrichment must never sink the card).
    """

    quote: Quote  # live price + the day's move
    valuation: TickerValuation  # the forward-PEG read at that price
    fundamentals: StockFundamentals | None  # market cap + dividend (best-effort)
    performance: StockPerformance | None  # trailing return windows (best-effort)


class GetTickerCard:
    """Use case: a stock's ticker card — live quote, forward PEG, and enrichment.

    The quote and the estimates are primary — the card exists to price the forward
    PEG — so either failing propagates (the endpoint maps it to HTTP). But *absent*
    forward coverage is not a failure: an ``is_empty`` estimates block (symbol not
    cached by the annual slice yet, or no analyst coverage) yields a null PEG
    around a live quote, the same "no data ≠ error" stance the other slices take.
    Fundamentals and performance are best-effort enrichment, mirroring the stock
    snapshot: unconfigured or failing providers just leave their blocks ``None``.
    """

    def __init__(
        self,
        quotes: StockQuoteProvider,
        estimates: AnalystEstimatesProvider,
        fundamentals: StockFundamentalsProvider | None = None,
        performance: StockPerformanceProvider | None = None,
    ) -> None:
        self._quotes = quotes
        self._estimates = estimates
        self._fundamentals = fundamentals
        self._performance = performance

    def execute(self, symbol: str) -> TickerCard:
        normalized = _normalize_symbol(symbol)
        quote = self._quotes.get_quote(normalized)  # required; errors propagate
        estimates = self._estimates.get_estimates(normalized)  # required; ditto
        # The estimate entity owns the per-leg rules (positive-EPS guards,
        # FY1→FY2 growth); this just evaluates them at today's price. An empty
        # block naturally yields all-None legs — no special-casing needed.
        valuation = TickerValuation(
            symbol=normalized,
            price=quote.price,
            forward_pe=estimates.forward_pe(quote.price),
            forward_eps_growth=estimates.forward_eps_growth(),
        )
        return TickerCard(
            quote=quote,
            valuation=valuation,
            fundamentals=self._get_fundamentals(normalized),
            performance=self._get_performance(normalized),
        )

    def _get_fundamentals(self, symbol: str) -> StockFundamentals | None:
        if self._fundamentals is None:
            return None
        try:
            return self._fundamentals.get_fundamentals(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the card

    def _get_performance(self, symbol: str) -> StockPerformance | None:
        if self._performance is None:
            return None
        try:
            return self._performance.get_performance(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the card
