"""Application use case for the ticker slice.

One action, pure orchestration over existing ports so it runs offline in tests
against hand-written fakes and knows nothing of Alpaca, Finnhub, the database, or
HTTP:

- ``GetTickerCard`` — the read path. Normalizes the ticker and the requested
  includes, takes the live quote through the ``StockQuoteProvider``, layers the
  always-on enrichment (the clean display name; fundamentals for the market cap),
  and fetches the *opt-in* blocks only when asked: ``dividend`` (already carried
  by the fundamentals call), ``performance`` (the trailing windows), ``metrics``
  (the stored forward consensus, for the forward PEG), and ``options_metrics``
  (the options-market read: ATM implied volatility, the priced-in expected move,
  the cost of a protective put, and the day's put/call lean). Pay-per-use:
  a block that isn't requested costs no provider call. Returns the assembled
  ``TickerCard``.

Unlike the earnings/recommendations slices there is no sync counterpart and no
repository: the card is built around the *live* quote, so nothing slice-owned is
worth persisting — the slow-moving half (the FY1/FY2 consensus) is already stored
and refreshed by the annual-earnings slice, and the fast-moving half (the quote)
must be fetched fresh anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Sequence

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
    StockFundamentalsProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.ticker.entities import TickerOptionsMetrics, TickerValuation
from app.stocks.ticker.ports import OptionChainProvider

# The blocks a caller may opt into. Everything else on the card (ticker, name,
# price + day move, market cap) is always served.
INCLUDABLE = frozenset({"dividend", "performance", "metrics", "options_metrics"})

# The two expiry windows the options read samples: IV and the expected move are
# quoted at ~1 month out (near-dated enough to reflect *current* nerves, far
# enough to dodge same-week lottery-ticket noise), and the protective put at
# ~3 months (a quarter of cover — the horizon a holder actually insures).
_NEAR_WINDOW = timedelta(days=30)
_INSURANCE_WINDOW = timedelta(days=90)


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
    fundamentals: StockFundamentals | None  # market cap + dividend (best-effort)
    performance: StockPerformance | None  # trailing windows; only with 'performance'
    options_metrics: TickerOptionsMetrics | None = None  # only with 'options_metrics'


class GetTickerCard:
    """Use case: a stock's ticker card — live quote, name, market cap, and the
    opt-in blocks (dividend, performance, forward-PEG metrics).

    The quote is primary, and so is the consensus read *when 'metrics' is
    requested* — so those failing propagates (the endpoint maps it to HTTP). But
    *absent* forward coverage is not a failure: an ``is_empty`` estimates block
    (symbol not cached by the annual slice yet, or no analyst coverage) yields a
    null PEG, the same "no data ≠ error" stance the other slices take. The profile
    (display name), fundamentals, performance and options metrics are best-effort
    enrichment, mirroring the stock snapshot: unconfigured or failing providers
    just leave their blocks ``None``. The options read stays best-effort *even
    when requested* — unlike the DB-backed consensus, it's a live Yahoo call, and
    Yahoo intermittently blocks data-centre IPs; a colored insight going missing
    must not take the quote down with it. Opt-in blocks that aren't requested
    cost no provider call at all ('dividend' rides the fundamentals call the
    market cap needs anyway, so it only gates presentation).
    """

    def __init__(
        self,
        quotes: StockQuoteProvider,
        estimates: AnalystEstimatesProvider,
        fundamentals: StockFundamentalsProvider | None = None,
        performance: StockPerformanceProvider | None = None,
        profile: CompanyProfileProvider | None = None,
        options: OptionChainProvider | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._quotes = quotes
        self._estimates = estimates
        self._fundamentals = fundamentals
        self._performance = performance
        self._profile = profile
        self._options = options
        # Injectable clock: the expiry windows are anchored on "today", and the
        # tests pin it the way the yfinance adapters pin theirs.
        self._today = today or date.today

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
            options_metrics=(
                self._get_options_metrics(normalized, quote)
                if "options_metrics" in wanted
                else None
            ),
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

    def _get_options_metrics(self, symbol: str, quote: Quote) -> TickerOptionsMetrics | None:
        """The options-market read: sample the ~1-month and ~3-month expiries and
        let the entity derive the four figures at today's price.

        Nearest-listed wins: options expire on fixed exchange dates, so each
        window picks the future expiry closest to its target — and when the
        listed dates are sparse both windows may land on the same expiry (the
        entity dedupes the shared chain). No listed options at all is "no
        coverage", not an error."""
        if self._options is None:
            return None
        try:
            today = self._today()
            future = [e for e in self._options.get_expirations(symbol) if e > today]
            if not future:
                return None
            near = min(future, key=lambda e: abs(e - today - _NEAR_WINDOW))
            far = min(future, key=lambda e: abs(e - today - _INSURANCE_WINDOW))
            near_chain = self._options.get_chain(symbol, near)
            far_chain = (
                near_chain if far == near else self._options.get_chain(symbol, far)
            )
            return TickerOptionsMetrics.from_chains(quote.price, near_chain, far_chain)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: a Yahoo-blocked read never sinks the card
