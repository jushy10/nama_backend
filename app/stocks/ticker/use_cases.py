"""Application use case for the ticker slice.

One action, pure orchestration over existing ports so it runs offline in tests
against hand-written fakes and knows nothing of Alpaca, Yahoo, the database, or
HTTP:

- ``GetTickerCard`` — the read path. Normalizes the ticker and the requested
  includes, takes the live quote through the ``StockQuoteProvider``, then serves
  everything else off **one anchor read**: the clean display name and the listing
  exchange (name served straight off the ``stocks`` row where the syncs write it;
  exchange lazily filled once from the feed), the read-only screen facts (market
  cap, sector, industry) the universe sync writes there, the annual slice's
  trailing growth + per-share cash, and the fundamentals slice's margins +
  dividend per share. The *opt-in* blocks are gated on the requested includes —
  ``dividend`` (per share off the anchor, yield priced live on the quote),
  ``performance`` (trailing windows, a live feed call), ``metrics`` (the full
  trailing valuation ladder — P/E off the quarterly slice's stored TTM on the
  consensus basis, plus P/B / P/S / PEG and the fundamentals slice's margins /
  ROE / liquidity / leverage / beta off the anchor, the trailing + forward YoY
  growth, and the forward P/E / P/S priced live off the stored forward
  consensus), and ``options_metrics`` (the options-market
  read: ATM implied volatility, the priced-in expected move, the cost of a
  protective put, and the day's put/call lean). No live *fundamentals* vendor is
  called at all — the margins and dividend ride the same anchor read as everything
  else, so a bare card and the metrics/dividend blocks alike cost zero fundamentals
  calls. Returns the assembled ``TickerCard``.

Unlike the earnings/recommendations slices there is no sync counterpart, and the
only persistence is the exchange lazy fill on the ``stocks`` row (through
``TickerRepository``): the card is built around the *live* quote, so nothing else
slice-owned is worth persisting — the slow-moving half (the anchor facts: name,
the FY1/FY2 consensus, the materialized fundamentals) is already stored and
refreshed by the universe / earnings / fundamentals syncs, and the fast-moving
half (the quote) must be fetched fresh anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Sequence

from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.entities import (
    AnalystEstimates,
    Quote,
    StockPerformance,
    Timeframe,
)
from app.stocks.etfs.repository import EtfLookupRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.charts.ports import CandleProvider
from app.stocks.ports import (
    AnalystEstimatesProvider,
    StockDataProvider,
    StockPerformanceProvider,
    StockQuoteProvider,
)
from app.stocks.ticker.entities import (
    PeHistory,
    ReportedEps,
    TTM_QUARTERS,
    TickerOptionsMetrics,
    TickerValuation,
)
from app.stocks.ticker.ports import EpsHistoryProvider, OptionChainProvider
from app.stocks.ticker.repository import StoredTickerFacts, TickerRepository

# The card's asset-type discriminator: an ETF (in the stored ETF universe) or a plain equity.
# Always one of these two — never null — so the FE can branch on it unconditionally.
ASSET_TYPE_ETF = "etf"
ASSET_TYPE_EQUITY = "equity"

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


def sample_options_metrics(
    options: OptionChainProvider,
    symbol: str,
    price: float,
    today: date,
    *,
    near_window: timedelta = _NEAR_WINDOW,
    insurance_window: timedelta = _INSURANCE_WINDOW,
) -> TickerOptionsMetrics | None:
    """The ticker card's options-market read: sample the ~1-month and ~3-month
    expiries and let the entity derive the four figures at ``price``.

    Nearest-listed wins: options expire on fixed exchange dates, so each window
    picks the future expiry closest to its target — and when the listed dates are
    sparse both windows may land on the same expiry (the entity dedupes the shared
    chain). ``None`` when the symbol has no listed options — "no coverage", not an
    error.

    Propagates the provider's ``StockNotFound``/``StockDataUnavailable`` straight
    through: the read is best-effort enrichment, so the caller wraps this in its
    own try/except rather than the helper deciding to swallow a failure.
    """
    future = [e for e in options.get_expirations(symbol) if e > today]
    if not future:
        return None
    near = min(future, key=lambda e: abs(e - today - near_window))
    far = min(future, key=lambda e: abs(e - today - insurance_window))
    near_chain = options.get_chain(symbol, near)
    far_chain = near_chain if far == near else options.get_chain(symbol, far)
    return TickerOptionsMetrics.from_chains(price, near_chain, far_chain)


@dataclass(frozen=True)
class TickerCard:
    """Everything the ticker endpoint serves, assembled from the ports.

    A composition of shared entities rather than a new domain concept — which is
    why it lives here with the orchestration (like the sync slices' report
    dataclasses) instead of in the slice's ``entities.py``: the slice-local entity
    (``TickerValuation``) owns the trailing-P/E rule, and this just bundles it with
    the quote and the enrichment blocks. ``include`` records which opt-in blocks
    the caller asked for, so the presenter can tell "not requested" apart from
    "requested but unavailable" (best-effort blocks are ``None`` either way).
    """

    quote: Quote  # live price + the day's move
    include: frozenset[str]  # the opt-in blocks this card was asked to carry
    valuation: TickerValuation | None  # the trailing-P/E read; only with 'metrics'
    performance: StockPerformance | None  # trailing windows; only with 'performance'
    # Always served (never null): "etf" when the symbol is in the stored ETF universe, else
    # "equity" — a single indexed lookup off the etfs table, so the FE can branch the card.
    asset_type: str = ASSET_TYPE_EQUITY
    name: str | None = None  # display name; served off the anchor (filled by the syncs)
    exchange: str | None = None  # listing venue; DB-first, filled once from the feed
    # The rest ride the same anchor read, served straight from the DB (never a provider
    # call): the universe screen's facts, the annual slice's trailing snapshot, and the
    # fundamentals slice's margins + dividend per share.
    market_cap: float | None = None  # raw USD, from the universe screen
    sector: str | None = None  # classification slug, from the universe screen
    industry: str | None = None  # classification slug, from the universe screen
    revenue_growth_yoy: float | None = None  # percent, annual slice's latest trailing YoY
    eps_growth_yoy: float | None = None  # percent (consensus basis), annual slice's latest trailing YoY
    fcf_growth_yoy: float | None = None  # percent, annual slice's latest trailing FCF/share YoY
    # Forward (analyst-consensus FY1->FY2) growth off the anchor — the forward mirror of the
    # trailing pair, served directly like it. Only shown with 'metrics'.
    forward_revenue_growth_yoy: float | None = None  # percent, forward consensus (anchor)
    forward_eps_growth_yoy: float | None = None  # percent, forward consensus (anchor)
    # The fundamentals slice's anchor writes (Yahoo .info): the trailing profitability /
    # liquidity / leverage / volatility ratios and the dividend per share (trading currency; the
    # presenter prices it live into a yield). Served off the same anchor read — no live vendor
    # call — and only shown when 'metrics'/'dividend' is requested.
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    roe: float | None = None  # percent, return on equity
    current_ratio: float | None = None  # current assets / current liabilities
    debt_to_equity: float | None = None  # total debt / equity (a ratio)
    beta: float | None = None  # volatility vs the market
    dividend_per_share: float | None = None
    # Forward valuation multiples priced on the live quote from the annual slice's stored
    # forward consensus (FY1 EPS -> forward P/E, FY1 revenue -> forward P/S). Best-effort — an
    # uncovered symbol (no forward estimates cached) leaves them null. Only shown with 'metrics'.
    forward_pe: float | None = None
    forward_ps: float | None = None
    options_metrics: TickerOptionsMetrics | None = None  # only with 'options_metrics'


class GetTickerCard:
    """Use case: a stock's ticker card — live quote, name, market cap, and the
    opt-in blocks (dividend, performance, metrics).

    The quote is the only primary read — it failing propagates (the endpoint maps
    it to HTTP). Everything else — the name, exchange, the margins/dividend/growth,
    performance, options metrics and the trailing TTM read — is best-effort
    enrichment: an anchor the syncs haven't reached yet, or an unconfigured/failing
    live feed, just leaves its block ``None``. The options and TTM reads stay
    best-effort *even when requested* — both can go live to Yahoo (the TTM on a cold
    cache miss), and Yahoo intermittently blocks data-centre IPs; a colored insight
    going missing must not take the quote down with it. The fundamentals (margins +
    dividend per share) now ride the same anchor read as market cap / sector /
    growth — no live fundamentals vendor is called — so the opt-in gating only
    controls which blocks the response carries, not whether a call is made.
    """

    def __init__(
        self,
        quotes: StockQuoteProvider,
        performance: StockPerformanceProvider | None = None,
        stocks: StockDataProvider | None = None,
        repository: TickerRepository | None = None,
        options: OptionChainProvider | None = None,
        earnings: QuarterlyEarningsProvider | None = None,
        estimates: AnalystEstimatesProvider | None = None,
        etfs: EtfLookupRepository | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._quotes = quotes
        self._performance = performance
        self._stocks = stocks
        self._repository = repository
        self._options = options
        self._earnings = earnings
        self._estimates = estimates
        self._etfs = etfs
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
        # The margins + dividend per share ride the same anchor read now (the fundamentals
        # slice materializes them there), so a bare card makes no extra provider call and even
        # the metrics/dividend blocks are served straight from the DB — the presenter just gates
        # which the response carries on the requested includes. The one extra read on the metrics
        # path is the forward estimates (for forward P/E and P/S — the only figures not on the
        # anchor), so it's made only when 'metrics' is asked for.
        wants_metrics = "metrics" in wanted
        forward = (
            self._get_forward_multiples(normalized, quote, stored)
            if wants_metrics
            else (None, None)
        )
        return TickerCard(
            quote=quote,
            include=wanted,
            asset_type=self._get_asset_type(normalized),
            valuation=(
                self._get_valuation(normalized, quote, stored) if wants_metrics else None
            ),
            performance=(
                self._get_performance(normalized) if "performance" in wanted else None
            ),
            name=stored.name,
            exchange=self._get_exchange(normalized, stored.exchange),
            market_cap=stored.market_cap,
            sector=stored.sector,
            industry=stored.industry,
            revenue_growth_yoy=stored.revenue_growth_yoy,
            eps_growth_yoy=stored.eps_growth_yoy,
            fcf_growth_yoy=stored.fcf_growth_yoy,
            forward_revenue_growth_yoy=stored.forward_revenue_growth_yoy,
            forward_eps_growth_yoy=stored.forward_eps_growth_yoy,
            gross_margin=stored.gross_margin,
            operating_margin=stored.operating_margin,
            net_margin=stored.net_margin,
            roe=stored.return_on_equity,
            current_ratio=stored.current_ratio,
            debt_to_equity=stored.debt_to_equity,
            beta=stored.beta,
            dividend_per_share=stored.dividend_per_share,
            forward_pe=forward[0],
            forward_ps=forward[1],
            options_metrics=(
                self._get_options_metrics(normalized, quote)
                if "options_metrics" in wanted
                else None
            ),
        )

    def _get_asset_type(self, symbol: str) -> str:
        # A single indexed membership check against the stored ETF universe: "etf" when the
        # symbol is one of the screened funds, else "equity". Always resolves to one of the two
        # (never null) so the FE can branch unconditionally — with no etfs repository wired
        # (a bare use case in a test) it just reads as an equity.
        if self._etfs is not None and self._etfs.is_etf(symbol):
            return ASSET_TYPE_ETF
        return ASSET_TYPE_EQUITY

    def _get_valuation(
        self, symbol: str, quote: Quote, stored: StoredTickerFacts
    ) -> TickerValuation:
        # The trailing multiples at today's quote: the P/E off the quarterly slice's
        # consensus TTM (the timeline owns the TTM rule), the FCF/OCF multiples off the annual
        # slice's stored per-share cash figures, and P/B / P/S off the fundamentals slice's
        # stored per-share book value / sales — all on the anchor (already read once, no extra
        # call), all priced against the same live quote. PEG rides the trailing EPS growth also
        # on the anchor, so both its legs sit on the consensus basis. Every leg best-effort — a
        # symbol the annual/quarterly/fundamentals slices haven't reached yields null multiples,
        # never a failed card (the entity owns the positivity guards).
        return TickerValuation(
            symbol=symbol,
            price=quote.price,
            ttm_eps=self._get_ttm_eps(symbol),
            fcf_per_share=stored.fcf_per_share,
            ocf_per_share=stored.ocf_per_share,
            book_value_per_share=stored.book_value_per_share,
            sales_per_share=stored.sales_per_share,
            eps_growth_yoy=stored.eps_growth_yoy,
            ebitda=stored.ebitda,
            total_debt=stored.total_debt,
            cash_and_equivalents=stored.cash_and_equivalents,
            shares_outstanding=stored.shares_outstanding,
        )

    def _get_forward_multiples(
        self, symbol: str, quote: Quote, stored: StoredTickerFacts
    ) -> tuple[float | None, float | None]:
        # Forward P/E and P/S at today's quote, off the annual slice's stored forward consensus
        # (the only fundamentals not materialized on the anchor — they need the FY1 *absolute*
        # EPS / revenue, not just the growth). DB-only projection (the same estimates port the
        # AI analysis context uses), best-effort: no provider, an uncovered symbol, or a failed
        # read just leaves both null. The entity owns the positivity guards (a non-positive
        # estimate makes the multiple meaningless).
        if self._estimates is None:
            return None, None
        try:
            estimates = self._estimates.get_estimates(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None, None
        if estimates.is_empty:
            return None, None
        return (
            estimates.forward_pe(quote.price),
            estimates.forward_ps(stored.market_cap),
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

    def _get_performance(self, symbol: str) -> StockPerformance | None:
        if self._performance is None:
            return None
        try:
            return self._performance.get_performance(symbol)
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: never sink the card

    def _get_options_metrics(self, symbol: str, quote: Quote) -> TickerOptionsMetrics | None:
        """The card's options-market read — best-effort, so a Yahoo-blocked read
        leaves the block null rather than sinking the card. The expiry sampling
        lives in ``sample_options_metrics`` (a module-level helper, so the rule for
        which two expiries to read is stated once)."""
        if self._options is None:
            return None
        try:
            return sample_options_metrics(
                self._options, symbol, quote.price, self._today()
            )
        except (StockNotFound, StockDataUnavailable):
            return None  # best-effort: a Yahoo-blocked read never sinks the card


@dataclass(frozen=True)
class TickerClassification:
    """The ``ClassifyTicker`` result: the normalized ticker and its asset type."""

    ticker: str
    asset_type: str


class ClassifyTicker:
    """Classify a ticker as an ETF or an equity — the lightweight counterpart to
    ``GetTickerCard``'s ``asset_type``.

    A single indexed ETF-universe membership check, with no quote or fundamentals
    call, so it stays one cheap DB read (for a caller that only needs to know
    which kind a symbol is, not its whole card). Any *valid* symbol resolves to
    one of the two — ``"equity"`` for a symbol outside the screened ETF set — so
    it never 404s; only a malformed symbol raises ``ValueError`` (a 400 at the
    edge), exactly as the card's normalization does.
    """

    def __init__(self, etfs: EtfLookupRepository) -> None:
        self._etfs = etfs

    def classify(self, symbol: str) -> TickerClassification:
        normalized = _normalize_symbol(symbol)
        asset_type = (
            ASSET_TYPE_ETF if self._etfs.is_etf(normalized) else ASSET_TYPE_EQUITY
        )
        return TickerClassification(ticker=normalized, asset_type=asset_type)


class GetStockPeHistory:
    """Use case: a stock's trailing P/E sampled at each earnings release.

    Derives the walk from two legs the entity combines: the *reported-EPS run* (through
    ``EpsHistoryProvider`` — the deep Yahoo read) rolled into a trailing-twelve-month
    series, and the *daily closes* (through the shared ``CandleProvider``, i.e. Alpaca)
    that price each release. One point per reported quarter, oldest first.

    The two legs split the way the card splits primary from enrichment. The closes are
    the reliable leg — Alpaca serves from data-centre IPs — so a failure there
    propagates (the endpoint maps it to HTTP). The EPS history is the best-effort leg —
    Yahoo intermittently blocks data-centre IPs — so a blocked read degrades to an
    *empty* history: a 200 with no points, the same "no data ≠ error" stance the rest of
    the slice takes. With fewer than a full trailing year of reported quarters there's
    nothing to anchor, so it returns empty without even paying the price fetch.
    """

    def __init__(
        self,
        candles: CandleProvider,
        eps_history: EpsHistoryProvider,
    ) -> None:
        self._candles = candles
        self._eps_history = eps_history

    def execute(self, symbol: str) -> PeHistory:
        normalized = _normalize_symbol(symbol)
        eps = self._get_eps_history(normalized)
        if len(eps) < TTM_QUARTERS:
            # Not enough reported quarters to form even one trailing-year point — skip
            # the price fetch entirely (an uncovered/blocked symbol, or a fresh listing).
            return PeHistory(symbol=normalized, points=())
        closes = self._get_closes(normalized, since=eps[0].report_date)
        return PeHistory.build(normalized, eps, closes)

    def _get_eps_history(self, symbol: str) -> tuple[ReportedEps, ...]:
        # Best-effort leg: a Yahoo-blocked read is an empty history, not a 502 — the P/E
        # walk is a card-adjacent extra, so "no coverage" and "blocked" both degrade the
        # same way (an empty series), never sinking the request.
        try:
            return self._eps_history.get_eps_history(symbol)
        except (StockNotFound, StockDataUnavailable):
            return ()

    def _get_closes(self, symbol: str, *, since: date) -> dict[date, float]:
        # Primary leg: daily closes spanning the reported window (its earliest quarter to
        # now). Errors propagate — Alpaca is the reliable source, so a failure here is a
        # real one worth surfacing rather than silently emptying the chart. Early quarters
        # outside the feed's range simply find no close and drop out in ``build``.
        start = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
        series = self._candles.get_candles(
            symbol, Timeframe.DAY_1, start=start, end=None
        )
        return {candle.timestamp.date(): candle.close for candle in series.candles}
