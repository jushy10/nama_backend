"""Application use case for the ticker slice.

One action, pure orchestration over existing ports so it runs offline in tests
against hand-written fakes and knows nothing of Alpaca, Finnhub, the database, or
HTTP:

- ``GetTickerCard`` — the read path. Normalizes the ticker and the requested
  includes, takes the live quote through the ``StockQuoteProvider``, layers the
  always-on enrichment — all served off **one anchor read**: the two DB-first
  identity facts (the clean display name and the listing exchange, each lazily
  filled **once** from its vendor into the ``stocks`` row) plus the read-only
  screen facts (market cap, sector, industry) the universe sync writes there —
  and fetches the *opt-in* blocks only when asked: ``dividend`` and
  ``performance`` (the trailing windows), ``metrics`` (the trailing P/E off the
  quarterly-earnings slice's stored TTM — consensus basis, so it pairs with the
  forward legs — the margins/PEG off the fundamentals call, the stored forward
  consensus for the forward PEG, and the annual slice's stored trailing YoY
  growth off the same anchor read), and ``options_metrics`` (the options-market
  read: ATM implied volatility, the priced-in expected move, the cost of a
  protective put, and the day's put/call lean). Pay-per-use: a block that isn't
  requested costs no provider call — and market cap now riding the anchor, the
  fundamentals call itself is opt-in (only ``dividend``/``metrics`` need it).
  Returns the assembled ``TickerCard``.

Unlike the earnings/recommendations slices there is no sync counterpart, and the
only persistence is the pair of anchor-level facts (name + exchange on the
``stocks`` row, through ``TickerRepository``): the card is built around the
*live* quote, so nothing else slice-owned is worth persisting — the slow-moving
half (the FY1/FY2 consensus) is already stored and refreshed by the
annual-earnings slice, and the fast-moving half (the quote) must be fetched
fresh anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Sequence

from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
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
from app.stocks.ticker.entities import TickerOptionsMetrics, TickerValuation
from app.stocks.ticker.ports import OptionChainProvider
from app.stocks.ticker.repository import StoredTickerFacts, TickerRepository

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
    fundamentals: StockFundamentals | None  # dividend + trailing metrics (best-effort; only with 'dividend'/'metrics')
    performance: StockPerformance | None  # trailing windows; only with 'performance'
    name: str | None = None  # display name; DB-first, filled once from the profile
    exchange: str | None = None  # listing venue; DB-first, filled once from the feed
    # The rest ride the same anchor read, served straight from the DB (never a
    # provider call): the universe screen's facts and the annual slice's trailing snapshot.
    market_cap: float | None = None  # raw USD, from the universe screen
    sector: str | None = None  # classification slug, from the universe screen
    industry: str | None = None  # classification slug, from the universe screen
    revenue_growth_yoy: float | None = None  # percent, annual slice's latest trailing YoY
    eps_growth_yoy: float | None = None  # percent (consensus basis), annual slice's latest trailing YoY
    options_metrics: TickerOptionsMetrics | None = None  # only with 'options_metrics'


class GetTickerCard:
    """Use case: a stock's ticker card — live quote, name, market cap, and the
    opt-in blocks (dividend, performance, forward-PEG metrics).

    The quote is primary, and so is the consensus read *when 'metrics' is
    requested* — so those failing propagates (the endpoint maps it to HTTP). But
    *absent* forward coverage is not a failure: an ``is_empty`` estimates block
    (symbol not cached by the annual slice yet, or no analyst coverage) yields a
    null PEG, the same "no data ≠ error" stance the other slices take. The name,
    exchange, fundamentals, performance, options metrics and the trailing TTM
    read are best-effort enrichment, mirroring the stock snapshot: unconfigured
    or failing providers just leave their blocks ``None``. The options and TTM
    reads stay best-effort *even when requested* — unlike the DB-backed
    consensus, both can go live to Yahoo (the TTM on a cold cache miss), and
    Yahoo intermittently blocks data-centre IPs; a colored insight going missing
    must not take the quote down with it. Opt-in blocks that aren't requested
    cost no provider call at all — and market cap now served off the anchor row
    (with sector, industry, and the trailing growth), the fundamentals call is
    itself opt-in: only 'dividend' and 'metrics' pull it.
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
        options: OptionChainProvider | None = None,
        earnings: QuarterlyEarningsProvider | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._quotes = quotes
        self._estimates = estimates
        self._fundamentals = fundamentals
        self._performance = performance
        self._profile = profile
        self._stocks = stocks
        self._repository = repository
        self._options = options
        self._earnings = earnings
        # Injectable clock: the expiry windows are anchored on "today", and the
        # tests pin it the way the yfinance adapters pin theirs.
        self._today = today or date.today

    def execute(
        self, symbol: str, include: Sequence[str] | None = None
    ) -> TickerCard:
        normalized = _normalize_symbol(symbol)
        wanted = _normalize_includes(include)
        quote = self._quotes.get_quote(normalized)  # required; errors propagate
        # One anchor read serves every DB-first fact: name + exchange (each falls back
        # to its vendor, and stores what it learns, only when the row lacks it) plus the
        # read-only screen facts (market cap, sector, industry) and the annual slice's
        # trailing growth — all served straight from the row, no provider call.
        stored = (
            self._repository.get_facts(normalized)
            if self._repository is not None
            else StoredTickerFacts()
        )
        return TickerCard(
            quote=quote,
            include=wanted,
            valuation=(
                self._get_valuation(normalized, quote) if "metrics" in wanted else None
            ),
            # Fundamentals is opt-in now that market cap comes off the anchor: it's
            # fetched only for the blocks that still need it (dividend, and the metrics'
            # PEG + margins) — a bare card costs no fundamentals call.
            fundamentals=(
                self._get_fundamentals(normalized)
                if wanted & {"dividend", "metrics"}
                else None
            ),
            performance=(
                self._get_performance(normalized) if "performance" in wanted else None
            ),
            name=self._get_name(normalized, stored.name),
            exchange=self._get_exchange(normalized, stored.exchange),
            market_cap=stored.market_cap,
            sector=stored.sector,
            industry=stored.industry,
            revenue_growth_yoy=stored.revenue_growth_yoy,
            eps_growth_yoy=stored.eps_growth_yoy,
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
            ttm_eps=self._get_ttm_eps(symbol),
        )

    def _get_ttm_eps(self, symbol: str) -> float | None:
        # The trailing leg of the metrics block, on the consensus basis: the
        # quarterly slice's timeline owns the TTM rule (sum of the 4 newest
        # reported quarters). Best-effort, unlike the consensus read above — the
        # read-through cache goes live to Yahoo on a cold symbol, and a blocked
        # fetch must degrade to a null multiple, not sink the card.
        if self._earnings is None:
            return None
        try:
            return self._earnings.get_quarterly_earnings(symbol).ttm_eps
        except (StockNotFound, StockDataUnavailable):
            return None

    def _get_name(self, symbol: str, stored: str | None) -> str | None:
        # DB-first, filled once: the clean display name ("Micron Technology") is
        # near-static — a rebrand is rare enough that whichever request first
        # learns it (from the profile vendor) settles it for every later view.
        # The slim quote carries no name at all, so without this the card has
        # only the ticker to show. Best-effort like the other always-on enrichment.
        if stored is not None:
            return stored
        profile = self._get_profile(symbol)
        name = profile.name if profile else None
        if name and self._repository is not None:
            self._repository.save_name(symbol, name)
        return name

    def _get_exchange(self, symbol: str, stored: str | None) -> str | None:
        # DB-first, filled once: a stock's listing exchange effectively never
        # changes, so the first view of a symbol pays one full-snapshot call to
        # learn it and every later view serves it straight from the stocks row.
        if stored is not None:
            return stored
        if self._stocks is None:
            return None
        try:
            exchange = self._stocks.get_stock(symbol).exchange
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the card
        if exchange and self._repository is not None:
            self._repository.save_exchange(symbol, exchange)
        return exchange

    def _get_profile(self, symbol: str) -> CompanyProfile | None:
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
