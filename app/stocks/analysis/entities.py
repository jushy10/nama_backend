"""Enterprise Business Rules: the AI-analysis slice's own entities.

Every AI-generated read the API serves — the per-stock buy/hold/sell analysis
(shared with the ETF analyser), the earnings story, the analyst-coverage
review, the sector rotation read, and the market summary. Pure domain objects:
the model fills in the substance, the presenter attaches the disclaimer at the
edge, and ``model``/``generated_at`` keep every read traceable.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.stocks.entities import StockPerformance


class Recommendation(str, Enum):
    """The headline call of an AI stock analysis, on the five-point sell-side scale.

    ``STRONG_BUY`` … ``STRONG_SELL`` mirror the analyst-consensus vocabulary, with
    ``HOLD`` the neutral middle — the 'strong' calls are reserved for when the figures
    line up especially clearly one way. The string values double as the JSON the model
    returns and the API serves, the same convention as ``Timeframe``. (Shared with the
    ETF analysis's ``InvestmentAnalysis``.)
    """

    STRONG_BUY = "strong_buy"
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


class Confidence(str, Enum):
    """How firmly an analysis holds its recommendation, given the data."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class InvestmentAnalysis:
    """An AI-generated, balanced read on whether a stock looks like a buy.

    Produced by a language model from the figures the rest of the slice already
    gathers — the price snapshot, trailing performance, the valuation/health
    metrics, and the recent earnings beat history — never from outside data the
    model happens to recall. It is informational, not personalized financial
    advice: the model fills in the substance below, and the edge (the presenter)
    is what attaches the disclaimer.

    ``recommendation`` is the headline call and ``confidence`` how firmly it's
    held; ``thesis`` is a few sentences of reasoning, with ``strengths`` (the
    bull case) and ``risks`` (the bear case) as short bullet points. ``model``
    records which model produced it and ``generated_at`` when, so a cached or
    stored analysis stays traceable.
    """

    symbol: str
    recommendation: Recommendation
    confidence: Confidence
    thesis: str
    strengths: tuple[str, ...]
    risks: tuple[str, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        """Whether the read carries its full substance — both the bull case
        (``strengths``) and the bear case (``risks``). The AI-analysis use cases
        refuse to cache an analysis that isn't complete, so a rare empty-list model
        result is never frozen for the cache TTL; the next view regenerates."""
        return bool(self.strengths and self.risks)


class SectionStance(str, Enum):
    """How one scorecard section reads *for the stock* — the favorability signal the
    card colours on.

    ``POSITIVE`` is a point in the stock's favour (a cheap valuation, strong margins,
    a bullish analyst consensus), ``NEGATIVE`` a point against, ``NEUTRAL`` mixed or
    unremarkable. Deliberately one shared scale across every section so the client
    colours consistently (green / amber / red), while each section's own ``label``
    carries the human read ("Exceptional", "Expensive"). The string values double as
    the JSON the model returns and the API serves, the same convention as
    ``Recommendation``.
    """

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


@dataclass(frozen=True)
class SectionMetric:
    """One supporting figure rendered as a chip under a scorecard section — a
    ``label`` and a pre-formatted display ``value`` (e.g. ``("Net margin", "25%")``).

    The values are attached from the figures the use case already gathered, **never
    authored by the model**, so a chip can never carry a hallucinated number — the
    same split the analysis has always drawn between the model's words and the
    service's numbers.
    """

    label: str
    value: str


@dataclass(frozen=True)
class ScorecardSection:
    """One graded facet of a ``StockScorecard``.

    ``key`` is a stable id the client renders off (``business_quality`` /
    ``valuation`` / ``earnings`` / ``analyst_view``) and ``title`` its display name.
    ``stance`` is the favourability signal (colours the card), ``label`` a
    one-to-few-word human tag ("Exceptional", "Expensive"), and ``summary`` a plain,
    everyday-language read of a sentence or two. ``metrics`` are the supporting chips,
    attached from gathered data. The model authors only ``stance`` / ``label`` /
    ``summary``; everything numeric comes from the service.
    """

    key: str
    title: str
    stance: SectionStance
    label: str
    summary: str
    metrics: tuple[SectionMetric, ...] = ()


@dataclass(frozen=True)
class StockScorecard:
    """An AI-generated, sectioned buy / hold / sell read on a stock.

    The section-based successor to ``InvestmentAnalysis`` for the stock endpoint
    (the ETF analysis still returns ``InvestmentAnalysis``). Rather than one flat
    thesis with bull/bear bullet lists, it grades a handful of facets — business
    quality, valuation, earnings, and the analyst view — each with its own
    plain-language read and supporting figures, over one overall verdict.

    ``recommendation`` is the headline call and ``confidence`` how firmly it's held;
    ``thesis`` is a one-line headline. ``sections`` are the graded facets.
    ``model`` records which model produced it and ``generated_at`` when, so a cached
    read stays traceable — the same as ``InvestmentAnalysis``.
    """

    symbol: str
    recommendation: Recommendation
    confidence: Confidence
    thesis: str
    sections: tuple[ScorecardSection, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        """Whether the read carries its full substance — at least one section, every
        one of them with a non-empty label *and* summary (the two fields the card
        shows in words). The use case refuses to cache an incomplete read, so a rare
        model miss (a section left blank) is never frozen for the cache TTL; the next
        view regenerates."""
        return bool(self.sections) and all(s.label and s.summary for s in self.sections)


class MarketTone(str, Enum):
    """The risk posture the day's sector rotation implies.

    A day where cyclical/growth sectors (tech, discretionary) lead is ``risk_on``
    (appetite for risk); one where defensives (staples, utilities, health care)
    lead is ``risk_off`` (a flight to safety); no clear rotation is ``mixed``. The
    string values double as the JSON the model returns and the API serves, the
    same convention as ``Recommendation``.
    """

    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    MIXED = "mixed"


@dataclass(frozen=True)
class SectorMover:
    """One constituent stock that drove a sector's move today — the grounded 'why'.

    A sector rises or falls because of the stocks inside it; this names one of them.
    ``change_percent`` is the stock's real day move (joined from the live quote board,
    never authored by the model), and ``market_cap`` its size — together they set
    ``weighted_move``, the cap-weighted contribution the movers are ranked by, so a
    mega-cap's 2% outranks a small-cap's 9% (the ETF the sector is read through is
    cap-weighted, so that ranking mirrors what actually moved it). ``name`` is the
    display name off the ``stocks`` anchor.
    """

    ticker: str
    name: str | None
    change_percent: float | None
    market_cap: float | None

    @property
    def weighted_move(self) -> float | None:
        """Approximate contribution to the sector's cap-weighted move — ``market_cap``
        times the day's percent change. The ranking key (not a displayed figure): its
        magnitude orders the movers, its sign splits gainers from losers. ``None`` when
        either input is missing (an unquoted or uncapped member can't be ranked)."""
        if self.change_percent is None or self.market_cap is None:
            return None
        return self.market_cap * self.change_percent


@dataclass(frozen=True)
class SectorBreadth:
    """How broad a sector's move is: advancers vs. decliners among its constituents.

    A single ETF percent hides whether a move was broad (most names participating) or
    narrow (one mega-cap dragging the tape); this restores that. ``advancers`` /
    ``decliners`` count the members that closed up / down on the day, and ``total`` the
    members that had a usable quote — so a client (or the model) can read "23 of 30 up"
    as a broad rally rather than a lopsided one.
    """

    advancers: int
    decliners: int
    total: int


@dataclass(frozen=True)
class SectorHeadline:
    """A recent headline from one of a sector's movers — a candidate catalyst.

    The deepest 'why': *why* the driving stock moved. Carried straight from the news
    slice (DB-only, never a live fetch on the analysis path), so ``title`` /
    ``published_at`` / ``publisher`` / ``link`` are the stored article's own facts —
    the model may reference the headline as a reason but never authors it. ``ticker``
    is the mover the headline belongs to.
    """

    ticker: str
    title: str
    published_at: datetime | None = None
    publisher: str | None = None
    link: str | None = None


@dataclass(frozen=True)
class SectorContext:
    """One sector's day board row enriched with what's driving it — the analyzer's input.

    Where the old sector analysis saw only each sector's ETF move, this bundles that row
    (``sector`` / ``symbol`` / ``change_percent`` / trailing ``performance``, all carried
    from the market board) with the grounded drivers behind it: the top ``movers`` in the
    sector's own day-direction (its biggest gainers when it's up, biggest losers when
    it's down), the ``breadth`` of the move, and recent ``headlines`` from those movers.
    The model reasons over all of it but authors only words — every number and headline
    here is service-supplied, so the 'why' it writes can be checked against the receipts.

    ``performance`` is the shared-kernel trailing-window block (not the market slice's
    ``SectorPerformance``), so this entity stays decoupled from the market slice — the use
    case maps a board row onto these primitives.
    """

    sector: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None
    performance: StockPerformance | None = None
    movers: tuple[SectorMover, ...] = ()
    breadth: SectorBreadth | None = None
    headlines: tuple[SectorHeadline, ...] = ()

    @classmethod
    def from_constituents(
        cls,
        *,
        sector: str,
        symbol: str,
        change_percent: float | None,
        performance: StockPerformance | None,
        constituents: "tuple[SectorMover, ...]",
        top_n: int = 3,
    ) -> "SectorContext":
        """Build the context from the sector's board row and its constituent movers.

        Pure attribution: split ``constituents`` into gainers (day change > 0) and losers
        (< 0), rank each by ``weighted_move`` magnitude, and keep the top ``top_n`` in the
        sector's *own* day-direction — its gainers when the sector closed up, its losers
        when it closed down (an unmoved sector defaults to gainers). Members with no usable
        quote are ignored for the movers but still absent from breadth. ``headlines`` are
        attached separately (they're an I/O read), so this stays a pure function of its
        inputs.
        """
        advancers = tuple(m for m in constituents if (m.change_percent or 0) > 0)
        decliners = tuple(m for m in constituents if (m.change_percent or 0) < 0)
        quoted = tuple(m for m in constituents if m.change_percent is not None)
        # Rank by cap-weighted contribution magnitude; a missing weight sorts last.
        gainers = sorted(
            advancers, key=lambda m: m.weighted_move or 0.0, reverse=True
        )
        losers = sorted(decliners, key=lambda m: m.weighted_move or 0.0)
        leading = change_percent is None or change_percent >= 0
        movers = tuple((gainers if leading else losers)[:top_n])
        # No quoted members (attribution unavailable, or the sector had no readable
        # constituents) -> no breadth, rather than a meaningless "0 of 0".
        breadth = (
            SectorBreadth(len(advancers), len(decliners), len(quoted)) if quoted else None
        )
        return cls(
            sector=sector,
            symbol=symbol,
            change_percent=change_percent,
            performance=performance,
            movers=movers,
            breadth=breadth,
        )


@dataclass(frozen=True)
class SectorHighlight:
    """One sector called out in a market analysis, with the model's plain note.

    ``change_percent`` is *not* authored by the model — it's joined back from the
    day's board (matched to the sector the model named) so the number on the card
    stays a real quote, never a figure the model invented. ``note`` is the model's
    one-line, plain-language read on why the sector is leading or lagging.

    ``movers`` and ``headlines`` are the grounded receipts behind that note, joined
    from the sector's :class:`SectorContext` (not authored either): the constituent
    stocks that drove the move and the recent headlines from them, so a client can
    render the driver chips and the catalyst link the note refers to.
    """

    sector: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None
    note: str
    movers: tuple[SectorMover, ...] = ()
    headlines: tuple[SectorHeadline, ...] = ()


@dataclass(frozen=True)
class SectorAnalysis:
    """An AI-generated read on how the market's sectors are moving today.

    The market-wide sibling of ``InvestmentAnalysis``: produced by a language
    model from the day's ranked sector board (each sector's move + trailing
    returns) and nothing else — never outside data the model happens to recall.
    ``summary`` is the plain-language headline of which corners of the market are
    leading and lagging; ``tone`` is the risk posture that rotation implies;
    ``leaders`` and ``laggards`` name the standout sectors with a short note each
    (their ``change_percent`` joined back from the board, not authored). It is
    informational, not personalized advice — the model fills in the substance and
    the presenter attaches the disclaimer. ``model``/``generated_at`` keep a
    cached read traceable, as with ``InvestmentAnalysis``.
    """

    summary: str
    tone: MarketTone
    leaders: tuple[SectorHighlight, ...]
    laggards: tuple[SectorHighlight, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        """Whether the read carries its substance — a headline ``summary`` and at
        least one standout sector (a ``leader`` or a ``laggard``). The AI-analysis
        use case refuses to cache an incomplete read, so a rare empty model result is
        never frozen for the cache TTL; the next view regenerates (mirrors
        ``InvestmentAnalysis.is_complete``)."""
        return bool(self.summary and (self.leaders or self.laggards))


class MarketPeriod(str, Enum):
    """A trailing timeframe the market summary reads over — the past week, month,
    or year.

    The string values double as the JSON the model returns and the API serves,
    the same convention as ``MarketTone``.
    """

    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


@dataclass(frozen=True)
class MarketIndexReturn:
    """One index's return over a single timeframe, carried on a period highlight.

    ``change_percent`` is joined from the day's board (a real quote), never
    authored by the model — the same discipline ``SectorHighlight`` follows.
    """

    name: str
    symbol: str  # the proxy ETF ticker, carried through from the board
    change_percent: float | None


@dataclass(frozen=True)
class MarketPeriodHighlight:
    """One timeframe (past week/month/year) in the market summary.

    ``note`` is the model's one-line, plain-language read of how that stretch
    went; ``indexes`` carries each index's real return over the window, joined
    from the board (real quotes) rather than authored by the model.
    """

    period: MarketPeriod
    note: str
    indexes: tuple[MarketIndexReturn, ...]


@dataclass(frozen=True)
class MarketSummary:
    """An AI-generated overview of how the US market has moved lately.

    The market-wide sibling of ``SectorAnalysis``: produced by a language model
    from the day's index board (the S&P 500 and the Nasdaq, each with its
    trailing-window returns) and nothing else — never outside data the model
    happens to recall. ``summary`` is the plain-language headline; ``tone`` is the
    risk posture the recent moves imply; ``periods`` breaks the read down by
    timeframe (the past year, month and week), each with a short note and the
    indexes' real returns (joined from the board, not authored). It is
    informational, not personalized advice — the model fills in the substance and
    the presenter attaches the disclaimer. ``model``/``generated_at`` keep a read
    traceable, as with ``SectorAnalysis``.
    """

    summary: str
    tone: MarketTone
    periods: tuple[MarketPeriodHighlight, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        """Whether the read carries its substance — a headline ``summary`` and at
        least one ``period`` highlight. The AI-analysis use case refuses to cache an
        incomplete read, so a rare empty model result is never frozen for the cache
        TTL; the next view regenerates (mirrors ``InvestmentAnalysis.is_complete``)."""
        return bool(self.summary and self.periods)


class EarningsTrend(str, Enum):
    """Where a company's earnings story is heading.

    ``accelerating`` when profit/sales growth is picking up (or its beats are
    getting bigger), ``slowing`` when growth is fading or it's starting to miss,
    ``steady`` when it's holding a consistent pace. The string values double as
    the JSON the model returns and the API serves, the same convention as
    ``Recommendation`` and ``MarketTone``.
    """

    ACCELERATING = "accelerating"
    STEADY = "steady"
    SLOWING = "slowing"


@dataclass(frozen=True)
class EarningsAnalysis:
    """An AI-generated, plain-language read of a company's earnings story.

    The earnings-focused sibling of ``InvestmentAnalysis``: produced by a language
    model from the earnings figures the slice already gathers — the recent
    quarterly and annual timelines (beats/misses, EPS and revenue, and the forward
    consensus) — never from outside data the model happens to recall. ``summary``
    is the plain-language headline of how earnings have gone and where they look
    headed; ``trend`` is the direction; ``highlights`` are a few short takeaways.
    It is informational, not personalized advice — the model fills in the substance
    and the presenter attaches the disclaimer. ``model``/``generated_at`` keep a
    read traceable, as with ``InvestmentAnalysis``.
    """

    symbol: str
    summary: str
    trend: EarningsTrend
    highlights: tuple[str, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        """Whether the read carries its substance — a headline ``summary`` and at
        least one ``highlight``. The AI-analysis use case refuses to cache an
        incomplete read, so a rare empty model result is never frozen for the cache
        TTL; the next view regenerates (mirrors ``InvestmentAnalysis.is_complete``)."""
        return bool(self.summary and self.highlights)


class RatingsVerdict(str, Enum):
    """The overall read of a stock's analyst coverage.

    ``bullish`` when the sell-side leans clearly positive (a lopsided Buy/Overweight split,
    rising targets, upgrades), ``cautious`` when it leans negative or is deteriorating (Holds and
    Sells, downgrades, falling targets), ``mixed`` when it's split or sending conflicting signals.
    The string values double as the JSON the model returns and the API serves, the same
    convention as ``EarningsTrend``.
    """

    BULLISH = "bullish"
    MIXED = "mixed"
    CAUTIOUS = "cautious"


@dataclass(frozen=True)
class RatingsAnalysis:
    """An AI-generated, plain-language read of a stock's *analyst coverage*.

    The analyst-ratings sibling of ``EarningsAnalysis``: produced by a language model from the
    coverage the recommendations slice already gathers — the buy/hold/sell consensus and how it's
    shifting, the consensus price target and its spread, and the most credible covering firms'
    current stances — never from outside data the model happens to recall. ``verdict`` is the
    overall read and ``confidence`` how firmly it's held; ``summary`` is the plain-language
    headline and ``findings`` a few short, concrete takeaways (e.g. a lopsided Buy split, a wide
    target spread, the most credible firms sitting below consensus). Informational, not advice —
    the model fills in the substance and the presenter attaches the disclaimer.
    ``model``/``generated_at`` keep a read traceable, as with ``EarningsAnalysis``.
    """

    symbol: str
    verdict: RatingsVerdict
    confidence: Confidence
    summary: str
    findings: tuple[str, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        """Whether the read carries its substance — a headline ``summary`` and at
        least one ``finding``. The AI-analysis use case refuses to cache an
        incomplete read, so a rare empty model result is never frozen for the cache
        TTL; the next view regenerates (mirrors ``InvestmentAnalysis.is_complete``)."""
        return bool(self.summary and self.findings)


class FundamentalsVerdict(str, Enum):
    """The overall read of a company's *fundamentals*.

    A holistic quality read of the business behind the price — how profitable it is, whether
    revenue and earnings are growing, how sound the balance sheet looks, and whether the shares
    are reasonably priced against all that. ``strong`` when the fundamentals clearly hold up
    (healthy margins and growth, a solid balance sheet, a valuation the numbers support),
    ``weak`` when they clearly don't (thin or falling margins, shrinking growth, heavy debt, or
    a valuation the business can't justify), ``mixed`` when the picture is uneven or the signals
    conflict. The string values double as the JSON the model returns and the API serves, the
    same convention as ``RatingsVerdict`` and ``EarningsTrend``.
    """

    STRONG = "strong"
    MIXED = "mixed"
    WEAK = "weak"


@dataclass(frozen=True)
class FundamentalsAnalysis:
    """An AI-generated, plain-language read of a company's *fundamentals*.

    The fundamentals-focused sibling of ``EarningsAnalysis`` and ``RatingsAnalysis``: produced by
    a language model from the fundamentals the slice already gathers — the trailing and forward
    valuation multiples (P/E, P/B, P/S, PEG, forward P/E), the profitability ladder (gross /
    operating / net margins, ROE), balance-sheet health (current ratio, debt-to-equity), the
    trailing and forward growth in revenue and earnings, the dividend, the market cap, and how the
    stock's P/E sits against its industry peers — never from outside data the model happens to
    recall. ``verdict`` is the overall read and ``confidence`` how firmly it's held; ``summary`` is
    the plain-language headline and ``findings`` a few short, concrete takeaways (e.g. a fat net
    margin, a stretched forward multiple versus peers, growth that is fading). Informational, not
    advice — the model fills in the substance and the presenter attaches the disclaimer.
    ``model``/``generated_at`` keep a read traceable, as with ``RatingsAnalysis``.
    """

    symbol: str
    verdict: FundamentalsVerdict
    confidence: Confidence
    summary: str
    findings: tuple[str, ...]
    model: str
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        """Whether the read carries its substance — a headline ``summary`` and at
        least one ``finding``. The AI-analysis use case refuses to cache an
        incomplete read, so a rare empty model result is never frozen for the cache
        TTL; the next view regenerates (mirrors ``InvestmentAnalysis.is_complete``)."""
        return bool(self.summary and self.findings)
