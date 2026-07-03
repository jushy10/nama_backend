"""Application use case for the ticker slice.

One action, pure orchestration over existing ports so it runs offline in tests
against hand-written fakes and knows nothing of Alpaca, Finnhub, the database, or
HTTP:

- ``GetTickerCard`` — the read path. Normalizes the ticker and the requested
  includes, takes the live quote through the ``StockQuoteProvider``, layers the
  always-on enrichment (the clean display name; fundamentals for the market cap;
  the listing exchange, DB-first with a one-time lazy fill from the price feed),
  and fetches the *opt-in* blocks only when asked: ``dividend`` (already carried
  by the fundamentals call), ``performance`` (the trailing windows), and
  ``metrics`` (the trailing ratios off the fundamentals call plus the stored
  forward consensus, for the forward PEG). Pay-per-use: a block that isn't
  requested costs no provider call. Returns the assembled ``TickerCard``.

Unlike the earnings/recommendations slices there is no sync counterpart, and the
only persistence is one anchor-level fact (the exchange on the ``stocks`` row,
through ``TickerRepository``): the card is built around the *live* quote, so
nothing else slice-owned is worth persisting — the slow-moving half (the FY1/FY2
consensus) is already stored and refreshed by the annual-earnings slice, and the
fast-moving half (the quote) must be fetched fresh anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.stocks.entities import (
    CompanyProfile,
    Quote,
    StockFundamentals,
    StockPerformance,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import (
    AnalystEstimatesProvider,
    CompanyProfileProvider,
    StockDataProvider,
    StockFundamentalsProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.ticker.entities import TickerValuation
from app.stocks.ticker.repository import TickerRepository

# The blocks a caller may opt into. Everything else on the card (ticker, name,
# price + day move, market cap) is always served.
INCLUDABLE = frozenset({"dividend", "performance", "metrics"})


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


def _normalize_includes(include: Sequence[str] | None) -> frozenset[str]:
    """Flatten/lower-case the requested includes and reject unknown ones, once, at
    the edge — the same stance as ``_normalize_symbol``. Accepts both repeated
    params and comma-separated values (``?include=dividend&include=metrics`` or
    ``?include=dividend,metrics``), since both are common client idioms."""
    if not include:
        return frozenset()
    parts = {
        part.strip().lower()
        for raw in include
        for part in raw.split(",")
        if part.strip()
    }
    unknown = parts - INCLUDABLE
    if unknown:
        raise ValueError(
            f"Unknown include(s): {', '.join(sorted(unknown))}. "
            f"Valid includes: {', '.join(sorted(INCLUDABLE))}."
        )
    return frozenset(parts)


@dataclass(frozen=True)
class TickerCard:
    """Everything the ticker endpoint serves, assembled from the ports.

    A composition of shared entities rather than a new domain concept — which is
    why it lives here with the orchestration (like the sync slices' report
    dataclasses) instead of in the slice's ``entities.py``: the slice-local entity
    (``TickerValuation``) owns the forward-PEG rule, and this just bundles it with
    the quote and the enrichment blocks. ``include`` records which opt-in blocks
    the caller asked for, so the presenter can tell "not requested" apart from
    "requested but unavailable" (best-effort blocks are ``None`` either way).
    """

    quote: Quote  # live price + the day's move
    include: frozenset[str]  # the opt-in blocks this card was asked to carry
    valuation: TickerValuation | None  # the forward-PEG read; only with 'metrics'
    profile: CompanyProfile | None  # clean company display name (best-effort)
    fundamentals: StockFundamentals | None  # market cap + dividend + trailing metrics (best-effort)
    performance: StockPerformance | None  # trailing windows; only with 'performance'
    exchange: str | None = None  # listing venue; DB-first, filled once from the feed


class GetTickerCard:
    """Use case: a stock's ticker card — live quote, name, market cap, and the
    opt-in blocks (dividend, performance, forward-PEG metrics).

    The quote is primary, and so is the consensus read *when 'metrics' is
    requested* — so those failing propagates (the endpoint maps it to HTTP). But
    *absent* forward coverage is not a failure: an ``is_empty`` estimates block
    (symbol not cached by the annual slice yet, or no analyst coverage) yields a
    null PEG, the same "no data ≠ error" stance the other slices take. The profile
    (display name), fundamentals and performance are best-effort enrichment,
    mirroring the stock snapshot: unconfigured or failing providers just leave
    their blocks ``None``. Opt-in blocks that aren't requested cost no provider
    call at all ('dividend' rides the fundamentals call the market cap needs
    anyway, so it only gates presentation).
    """

    def __init__(
        self,
        quotes: StockQuoteProvider,
        estimates: AnalystEstimatesProvider,
        fundamentals: StockFundamentalsProvider | None = None,
        performance: StockPerformanceProvider | None = None,
        profile: CompanyProfileProvider | None = None,
        stocks: StockDataProvider | None = None,
        repository: TickerRepository | None = None,
    ) -> None:
        self._quotes = quotes
        self._estimates = estimates
        self._fundamentals = fundamentals
        self._performance = performance
        self._profile = profile
        self._stocks = stocks
        self._repository = repository

    def execute(
        self, symbol: str, include: Sequence[str] | None = None
    ) -> TickerCard:
        normalized = _normalize_symbol(symbol)
        wanted = _normalize_includes(include)
        quote = self._quotes.get_quote(normalized)  # required; errors propagate
        return TickerCard(
            quote=quote,
            include=wanted,
            valuation=(
                self._get_valuation(normalized, quote) if "metrics" in wanted else None
            ),
            profile=self._get_profile(normalized),
            fundamentals=self._get_fundamentals(normalized),
            performance=(
                self._get_performance(normalized) if "performance" in wanted else None
            ),
            exchange=self._get_exchange(normalized),
        )

    def _get_valuation(self, symbol: str, quote: Quote) -> TickerValuation:
        # Primary when requested: the metrics block exists to price the forward
        # PEG, so a failing consensus read propagates rather than degrading.
        estimates = self._estimates.get_estimates(symbol)
        # The estimate entity owns the per-leg rules (positive-EPS guards,
        # FY1→FY2 growth); this just evaluates them at today's price. An empty
        # block naturally yields all-None legs — no special-casing needed.
        return TickerValuation(
            symbol=symbol,
            price=quote.price,
            forward_pe=estimates.forward_pe(quote.price),
            forward_eps_growth=estimates.forward_eps_growth(),
        )

    def _get_exchange(self, symbol: str) -> str | None:
        # DB-first, filled once: a stock's listing exchange effectively never
        # changes, so the first view of a symbol pays one full-snapshot call to
        # learn it and every later view serves it straight from the stocks row.
        # Best-effort like the other always-on enrichment.
        if self._repository is None:
            return None
        stored = self._repository.get_exchange(symbol)
        if stored is not None:
            return stored
        if self._stocks is None:
            return None
        try:
            exchange = self._stocks.get_stock(symbol).exchange
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the card
        if exchange:
            self._repository.save_exchange(symbol, exchange)
        return exchange

    def _get_profile(self, symbol: str) -> CompanyProfile | None:
        # The clean display name ("Micron Technology") — the slim quote carries no
        # name at all, so without this the card has only the ticker to show.
        if self._profile is None:
            return None
        try:
            return self._profile.get_profile(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the card

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
