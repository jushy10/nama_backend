"""Entities: the investable-universe view of a stock.

Slice-local domain objects (this sub-slice keeps its own ``entities`` rather than reaching
into the shared ``app/stocks/entities.py``, the same convention as the earnings and
recommendations sub-slices). Pure and vendor-agnostic — stdlib only.

``ScreenedStock`` is one row of the screened universe: the identity facts the ``stocks``
anchor holds (``ticker`` / ``name`` / ``exchange``) alongside the screen's own figures —
``market_cap`` (the selection criterion) and ``sector``. It is the single shape the
screener returns and the sync persists onto the anchor.

``CompanyClassification`` is the stock's sector + industry, fetched separately (the bulk
screen carries neither) and stored as snake_case slugs by the sync's enrichment pass.

The read side (the ``GET /stocks/ticker`` search + ``GET /stocks/classifications``) adds the
shapes the search flows through: ``StockSearchCriteria`` (a normalized query — free text plus
sector/industry/index-membership filters, a ``StockSort`` field with a ``SortDirection``, and
a limit/offset page), the ``StockSearchResult`` rows it matches wrapped in a
``StockSearchPage`` (carrying the total match count for pagination), and ``Classifications``
(the distinct sector/industry slugs the FE offers as filter menus). All pure value objects —
the SQL that reads them lives in the adapter, the normalization in the use case.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum

from app.stocks.entities import StockPerformance


@dataclass(frozen=True)
class ScreenedStock:
    """One company in the screened universe.

    ``market_cap`` is in whole units of the row's trading ``currency`` (e.g. ``3.01e12`` for a
    $3.01T US company, or whole CAD for a TSX one). Everything but the ``ticker`` is optional:
    ``exchange`` comes from the screen, ``sector`` may be absent (the yfinance screen doesn't
    publish it, so it rides in ``None``), and the name may be missing.

    ``country`` / ``currency`` are the screen's market facts: the ISO-2 listing country
    (``US`` / ``CA``) and the ISO-3 trading currency (``USD`` / ``CAD``) the ``market_cap`` and
    ``price`` are quoted in. They matter because the ≥$1B floor is applied in each market's
    native currency, so a row must carry its unit — the sync persists both onto the anchor.

    ``has_us_listing`` is ``True`` for a Canadian listing that duplicates a US-listed company —
    a CDR or a same-ticker dual-listing (matched by base ticker), or a *rebranded* Cboe Canada
    CDR whose ticker differs from its US line (matched by company name, e.g. ``COLA`` → Coca-Cola).
    The screen adapter always leaves it ``False``; the sync's CA pass computes it (this listing
    against the US universe) and persists it so the search can hide the duplicate.

    ``price`` is the screen-time regular-market price the screen quote carries. It is *not*
    persisted: the sync uses it (over the quarterly slice's TTM consensus EPS) to derive the
    stored ``pe_ratio`` on the anchor, the same way ``market_cap`` is a price-derived screen
    fact — so both value figures on a row come from one screen snapshot.
    """

    ticker: str
    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None
    price: float | None = None  # screen-time price; derives pe_ratio, not itself stored
    country: str | None = None  # ISO-2 listing country (US / CA)
    currency: str | None = None  # ISO-3 trading currency (USD / CAD)
    # True when this Canadian listing duplicates a US-listed company (a CDR or a same-ticker
    # dual-listing, matched by base ticker; or a rebranded .NE CDR, matched by company name) —
    # computed by the sync's CA pass (this listing vs the US universe), not the screen adapter,
    # which always leaves it False. The search hides these by default.
    has_us_listing: bool = False


@dataclass(frozen=True)
class CompanyClassification:
    """A stock's sector + industry (snake_case slugs) plus its issuer domicile (ISO-2).

    The screen (``ScreenedStock``) carries none of these — Yahoo publishes them only on the
    per-ticker ``.info`` surface — so this is the shape the sync's enrichment pass fetches and
    persists off one ``.info`` call. Every side is optional: a symbol Yahoo doesn't classify (or
    only half classifies) yields ``None`` for the missing side, which the sync leaves for a later
    run.

    ``sector`` / ``industry`` are stored as slugs — lower-cased, with every run of non-alphanumeric
    characters collapsed to a single underscore (``"Consumer Electronics"`` → ``consumer_electronics``,
    ``"Oil & Gas E&P"`` → ``oil_gas_e_p``) — a stable, join-friendly key rather than Yahoo's display
    text. ``domicile_country`` is the company's home country as an ISO-2 code (``"United States"`` →
    ``US``, ``"Canada"`` → ``CA``, ``"Switzerland"`` → ``CH``), which the universe search splits the
    US / Canadian screeners on — distinct from the *listing* market a row carries. ``from_labels`` is
    the constructor callers use, so the slug and country-mapping rules live in one place.
    """

    sector: str | None = None
    industry: str | None = None
    domicile_country: str | None = None

    @classmethod
    def from_labels(
        cls, sector: object, industry: object, country: object = None
    ) -> "CompanyClassification":
        """Build a classification from raw vendor labels — sector/industry each slugged to
        snake_case, ``country`` mapped from Yahoo's display name to an ISO-2 code — each dropped
        to ``None`` when blank, non-string, or (for the country) unrecognized."""
        return cls(
            sector=slugify(sector),
            industry=slugify(industry),
            domicile_country=country_to_iso2(country),
        )


@dataclass(frozen=True)
class AnchorMetrics:
    """The fundamentals the app materializes on the ``stocks`` anchor, read in one query so the
    AI analysis serves them DB-only rather than from a live vendor.

    Three groups, all off the anchor:

    - **Annual-earnings slice** — the newest reported year's trailing free cash flow per share
      and the trailing year-over-year revenue/EPS growth (EPS on the analyst-consensus basis).
    - **Fundamentals slice** (Yahoo ``.info``) — the trailing margins / ROE / liquidity /
      leverage / beta, plus the per-share *inputs* the reader prices against the live quote
      (``book_value_per_share`` → P/B, ``sales_per_share`` → P/S, ``dividend_per_share`` →
      yield) and the enterprise-value *inputs* it prices live (``ebitda`` / ``total_debt`` /
      ``cash_and_equivalents`` / ``shares_outstanding`` → EV/EBITDA).
    - **Anchor facts** — ``market_cap`` (the universe screen's figure) and the clean display
      ``name``, which replace the live Finnhub fundamentals/profile calls the analysis used to
      make.

    Read DB-only so every figure is the canonical one the ticker card and universe search show,
    or absent — never a divergent live-vendor number. Every field is nullable (unset until the
    slice's sync reaches the stock). A read-model, the multi-column sibling of
    ``industry_for_ticker`` / ``tier_for_ticker``.
    """

    fcf_per_share: float | None = None
    ocf_per_share: float | None = None
    revenue_growth_yoy: float | None = None
    eps_growth_yoy: float | None = None
    fcf_growth_yoy: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    return_on_equity: float | None = None
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    beta: float | None = None
    book_value_per_share: float | None = None
    sales_per_share: float | None = None
    dividend_per_share: float | None = None
    ebitda: float | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    shares_outstanding: float | None = None
    market_cap: float | None = None
    name: str | None = None


class StockSort(str, Enum):
    """The sortable columns of a universe search.

    A ``str`` enum so FastAPI binds it straight from the ``?sort=`` query param (an unknown
    value is a 422, like ``StockIndex``/``Timeframe``) and it serialises back as its value.
    These name the sortable *columns*; the search applies none of them unless one is asked for
    (omitting ``?sort=`` is a neutral, unsorted ticker order — see ``StockSearchCriteria.sort``),
    so there is no default member. ``MARKET_CAP`` orders biggest-first; ``REVENUE_GROWTH`` /
    ``EPS_GROWTH`` are the annual slice's latest *trailing* year-over-year figures on the anchor
    and ``FORWARD_REVENUE_GROWTH`` / ``FORWARD_EPS_GROWTH`` their *forward* (FY1→FY2 consensus)
    counterparts; ``FCF_GROWTH`` is the trailing FCF-per-share growth; ``GROWTH`` /
    ``FORWARD_GROWTH`` each blend a pair (its equal-weight average) so one control ranks the
    fastest all-round growers, trailing or expected; ``PE`` orders by the stored trailing P/E
    (the consensus-basis figure the universe sync writes) — ascending surfaces the cheapest on
    earnings first; ``FCF_YIELD`` orders by the materialized free-cash-flow yield — descending
    surfaces the cheapest on cash (highest yield) first; ``EV_EBITDA`` orders by the materialized
    EV/EBITDA snapshot — ascending surfaces the cheapest on enterprise value (the
    capital-structure-neutral cousin of ``PE``) first. The value → ORM column/expression
    mapping is the adapter's job — the enum just names the choices in domain terms.
    """

    MARKET_CAP = "market_cap"
    REVENUE_GROWTH = "revenue_growth"
    EPS_GROWTH = "eps_growth"
    FCF_GROWTH = "fcf_growth"
    GROWTH = "growth"
    FORWARD_REVENUE_GROWTH = "forward_revenue_growth"
    FORWARD_EPS_GROWTH = "forward_eps_growth"
    FORWARD_GROWTH = "forward_growth"
    PE = "pe"
    FCF_YIELD = "fcf_yield"
    EV_EBITDA = "ev_ebitda"


class SortDirection(str, Enum):
    """Ascending or descending — the ``?order=`` query param, bound the same way."""

    ASC = "asc"
    DESC = "desc"


class MarketCapTier(str, Enum):
    """A market-capitalization size bucket — the ``?market_cap=`` filter.

    A ``str`` enum bound straight from the query param like ``StockSort`` (an unknown value is a
    422). The four conventional cap tiers — ``MEGA`` ≥ $200B, ``LARGE`` $10–200B, ``MID`` $2–10B,
    ``SMALL`` $250M–$2B — expressed as half-open ranges (lower inclusive, upper exclusive) so
    they don't overlap at the seams. The tier → dollar-bounds mapping is the adapter's job (like
    the sort → column map); the enum only names the choices. (The universe floor is $1B, so
    ``SMALL`` in practice surfaces the $1–2B slice.)
    """

    MEGA = "mega"
    LARGE = "large"
    MID = "mid"
    SMALL = "small"


# Ascending size order — the axis a tier-anchored peer cohort widens along (same tier first,
# then its nearest neighbours). Naming/ordering only; the tier ⇄ dollar-bounds mapping stays
# the adapter's job (see the ``MarketCapTier`` docstring).
_TIER_ORDER_ASC: tuple[MarketCapTier, ...] = (
    MarketCapTier.SMALL,
    MarketCapTier.MID,
    MarketCapTier.LARGE,
    MarketCapTier.MEGA,
)


@dataclass(frozen=True)
class StockSearchResult:
    """One row of a universe search — the anchor facts served straight from the ``stocks``
    table, no live price (a page is a single DB read; the FE fetches a quote/card per row on
    demand via ``GET /stocks/ticker/{ticker}``).

    ``in_sp500`` / ``in_nasdaq100`` are definite yes/no (the anchor stores them ``NOT NULL``);
    everything else is nullable — a screened stock always has a ``market_cap`` (the search
    only returns screened rows) but may still lack a name, a classification, the trailing /
    forward growth, a ``pe_ratio``, or an ``ev_ebitda`` until the enriching sync/annual slice
    reaches it (forward growth the most often, since it needs two upcoming years; ``pe_ratio``
    stays null until the quarterly cache holds four reported quarters, and for a trailing-year
    loss; ``ev_ebitda`` until the fundamentals slice has landed the EBITDA, and on a non-positive
    EBITDA).

    ``performance`` is the stock's trailing-window returns (1W…1Y, YTD), materialized on the
    anchor by the performance sync — ``None`` for a row it hasn't reached yet (or one with too
    little history). It's what lets a page-driven consumer (the heat map) colour a whole board's
    timeframe tiles from one DB read instead of a live year-of-bars computation per index.

    ``country`` / ``currency`` are the row's market facts (ISO-2 / ISO-3): the market the stock
    lists on and the currency its ``market_cap`` is quoted in. Nullable only for a legacy row a
    screen hasn't re-stamped; every freshly screened member carries both. ``currency`` is what a
    client needs to read a CAD ``market_cap`` correctly next to a USD one (the floor is native).
    """

    ticker: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: float | None
    pe_ratio: float | None
    fcf_yield: float | None
    ev_ebitda: float | None
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    fcf_growth_yoy: float | None
    forward_revenue_growth_yoy: float | None
    forward_eps_growth_yoy: float | None
    in_sp500: bool
    in_nasdaq100: bool
    # Market facts default to None so a pre-multi-market builder still constructs; the DB read
    # (`_to_result`) always supplies them for a screened row. has_us_listing is surfaced so a
    # client can label / opt into the interlisted Canadian duplicates the search hides by default.
    country: str | None = None
    currency: str | None = None
    has_us_listing: bool = False
    performance: StockPerformance | None = None


@dataclass(frozen=True)
class StockSearchCriteria:
    """A normalized universe-search request — the shape the use case hands the repository.

    Every field is already cleaned at the use-case edge: ``query`` is trimmed (``None`` when
    blank) and matched as a case-insensitive substring against name *or* ticker; ``sectors`` /
    ``industries`` are slugged to the stored convention (empty = don't filter, else match *any*
    of the given slugs — an OR set, so a client can screen several at once); the index flags
    are tri-state (``None`` = don't filter, else match the boolean); ``market_cap_tiers`` narrows
    to the union of the given cap buckets (empty = every size); ``limit`` is clamped to a sane
    page and ``offset`` floored at zero. The adapter turns this into one SQL query.

    ``sort`` is ``None`` for an unsorted search — the adapter then orders by ticker alone (a
    neutral, stable A→Z), the default when a client omits ``?sort=``; a ``StockSort`` value picks
    a column to order by. ``direction`` only bites once a ``sort`` is chosen (an unsorted page is
    always ascending by ticker).

    ``countries`` narrows to the union of the given ISO-2 *listing* markets (``("US",)`` for US
    only, ``("CA",)`` for Canadian, empty = every market). It's how a client keeps a ``market_cap``
    sort within one currency (the floor is applied natively per market), or shows a single-market
    board.

    ``include_interlisted`` is ``False`` by default, so when a *single* market is chosen the search
    scopes it to that market's **home companies** by issuer domicile: the US market drops
    Canadian-domiciled rows (a Canadian company's US line, ``CNI``) while keeping other foreign
    ADRs, and the Canadian market drops foreign-domiciled rows (the CDRs of US / European / Japanese
    companies) while keeping Canadian companies. A row whose domicile is still unknown is kept
    (shown in its listing market). Set it ``True`` to skip that scoping and see every listing in the
    market, duplicates included. (No effect when zero or several markets are chosen.)
    """

    query: str | None
    sectors: tuple[str, ...]
    industries: tuple[str, ...]
    in_sp500: bool | None
    in_nasdaq100: bool | None
    sort: StockSort | None
    direction: SortDirection
    limit: int
    offset: int
    market_cap_tiers: tuple[MarketCapTier, ...] = ()
    countries: tuple[str, ...] = ()
    include_interlisted: bool = False


@dataclass(frozen=True)
class ScreenIntent:
    """A plain-English screen request translated into the search's own filters.

    The shape an AI translator returns and the ``AiScreenStocks`` use case feeds straight
    into ``SearchStocks.execute`` — every field maps one-to-one onto a search parameter, so
    the AI leg only decides *which filters to set* and the ordinary search does the querying
    (no new query engine, no way to reach a stock outside the screened universe). All fields
    default to "not set" (an empty request is a neutral browse): ``sectors`` / ``industries``
    are OR sets of stored slugs, the index flags tri-state, ``market_cap_tiers`` a union of
    size buckets, ``sort`` optional with ``direction`` only biting once it's set, and ``limit``
    the count the user asked for (``None`` to let the search default apply). The use case
    still runs each field through the search's normalization, so an off-vocabulary slug the
    model invents simply matches nothing rather than erroring."""

    query: str | None = None
    sectors: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    in_sp500: bool | None = None
    in_nasdaq100: bool | None = None
    market_cap_tiers: tuple[MarketCapTier, ...] = ()
    sort: StockSort | None = None
    direction: SortDirection = SortDirection.DESC
    limit: int | None = None


@dataclass(frozen=True)
class StockSearchPage:
    """A page of search results plus the total number of matches.

    ``total`` is the full count *before* ``limit``/``offset`` (so the FE can render pagers);
    ``results`` is just this page. ``limit`` / ``offset`` echo the criteria the page was cut
    with, so a client reading only the response knows where it is.
    """

    results: tuple[StockSearchResult, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class Classifications:
    """The distinct sector and industry slugs present in the universe — the FE's filter menus.

    Two flat, sorted, de-duplicated lists (nulls excluded). The FE offers each independently;
    the search endpoint accepts the same slugs back as its ``sector`` / ``industry`` filters.
    """

    sectors: tuple[str, ...]
    industries: tuple[str, ...]


@dataclass(frozen=True)
class IndustryValuation:
    """A per-industry trailing-P/E benchmark over the screened universe.

    The distribution of one industry's stored consensus-basis trailing P/Es (the same
    figure the search sorts on and the ticker card serves), summarized so a caller can judge
    whether a single stock's multiple is rich or cheap *for its industry* — the one anchor
    that makes an absolute P/E meaningful. ``median_pe`` is the typical multiple and
    ``p25_pe`` / ``p75_pe`` the interquartile range (the middle-half band); ``count`` is how
    many peers had a usable (positive) P/E — the sample the summary rests on, so a thin
    industry reads as low-confidence. All three stats are ``None`` when ``count`` is 0 (an
    unknown industry, or none valued yet): no coverage, not an error. ``industry`` echoes the
    normalized slug.

    ``cohort`` names the size slice the peers were drawn from — ``"industry"`` for the whole
    (mid-cap-and-up) industry, or a size label like ``"mega"`` / ``"large/mega"`` when the
    benchmark was scoped to the anchor stock's own cap tier (see :meth:`for_stock_peers`). It
    keeps the summary honest: a median of the mega-caps only should not read as an
    industry-wide figure.
    """

    # The smallest peer sample a benchmark can rest on and still say something about the
    # *industry* rather than about one or two companies. Below this, the "median" is just
    # a couple of stocks' own multiples (in the worst case the looked-up stock itself —
    # sole-peer industries exist in the live universe), so a comparison against it is
    # noise wearing a verdict. Five keeps ~80% of live industries while dropping every
    # degenerate case a sweep of the deployed data surfaced.
    MIN_REPRESENTATIVE_PEERS = 5

    industry: str
    count: int
    median_pe: float | None
    p25_pe: float | None
    p75_pe: float | None
    cohort: str = "industry"

    @property
    def is_representative(self) -> bool:
        """Whether the sample is large enough to stand for the industry
        (``count >= MIN_REPRESENTATIVE_PEERS``) — the gate consumers comparing one stock
        against the benchmark should apply before treating it as a peer anchor."""
        return self.count >= self.MIN_REPRESENTATIVE_PEERS

    @classmethod
    def from_pe_ratios(
        cls, industry: str, pe_ratios: Sequence[float]
    ) -> "IndustryValuation":
        """Summarize an industry's peer P/Es into the benchmark.

        Callers pass only *usable* (positive) P/Es — the repository filters null and
        non-positive out — so this is pure statistics: sort, then take the median and the
        quartiles by linear interpolation. An empty sample yields all-``None`` stats.
        """
        values = sorted(pe_ratios)
        return cls(
            industry=industry,
            count=len(values),
            median_pe=_percentile(values, 50),
            p25_pe=_percentile(values, 25),
            p75_pe=_percentile(values, 75),
        )

    @classmethod
    def for_stock_peers(
        cls,
        industry: str,
        anchor_tier: "MarketCapTier | None",
        peers: Sequence[tuple[float, "MarketCapTier"]],
    ) -> "IndustryValuation":
        """Build a benchmark scoped to the anchor stock's *own* cap tier, widening as needed.

        A mega-cap is best judged against other mega-caps, not the whole industry — but
        size-tier buckets are thin, so a strict same-tier median would collapse below
        :attr:`MIN_REPRESENTATIVE_PEERS` (worst for the largest tiers, where an industry may
        hold only one or two names). So this starts with the anchor's own tier and **widens to
        the nearest neighbouring tiers** until the cohort is representative, falling back to the
        whole industry if even that isn't enough. ``peers`` are ``(positive_pe, tier)`` pairs
        (the mid-cap-and-up sample the repository already filtered); ``anchor_tier`` is the
        looked-up stock's tier, or ``None`` when its cap is unknown — in which case there is no
        tier to anchor on and the result is the plain whole-industry benchmark.

        The returned ``cohort`` records which slice was used, so a ``"mega"`` median never reads
        as an industry-wide one.
        """
        all_pes = [pe for pe, _ in peers]
        if anchor_tier is None or not peers:
            return cls.from_pe_ratios(industry, all_pes)

        order = _TIER_ORDER_ASC
        anchor_rank = order.index(anchor_tier)
        present = {tier for _, tier in peers}
        # Widen the radius around the anchor tier until the cohort is representative; the last
        # radius (covering every present tier) is the whole-industry fallback, taken even if it
        # still falls short — the caller applies ``is_representative`` to decide whether to use it.
        max_radius = max(abs(order.index(tier) - anchor_rank) for tier in present)
        for radius in range(max_radius + 1):
            allowed = {
                tier
                for i, tier in enumerate(order)
                if abs(i - anchor_rank) <= radius
            }
            cohort_pes = [pe for pe, tier in peers if tier in allowed]
            if len(cohort_pes) >= cls.MIN_REPRESENTATIVE_PEERS or radius == max_radius:
                label = _cohort_label(allowed & present, present)
                return replace(cls.from_pe_ratios(industry, cohort_pes), cohort=label)
        # Unreachable: max_radius always terminates the loop above.
        return cls.from_pe_ratios(industry, all_pes)


@dataclass(frozen=True)
class PeerCompany:
    """One company in a peer comparison — a row of the side-by-side table.

    The valuation/quality columns available DB-only off the ``stocks`` anchor: ``market_cap``
    (raw USD), the two materialized valuation snapshots ``pe_ratio`` (trailing, consensus basis)
    and ``ev_ebitda`` (signed), the ``fcf_yield`` (percent, signed), ``net_margin`` (percent) and
    ``revenue_growth_yoy`` (percent, latest trailing). Every metric is nullable — a peer the
    enriching syncs haven't fully reached shows blanks for what it lacks, so the comparison
    degrades gracefully rather than dropping the row. ``tier`` is the company's market-cap size
    bucket (the axis the cohort is scoped along); ``is_anchor`` marks the looked-up stock so a
    client can highlight it in the table. Deliberately *not* P/B or P/S — those are computed
    live off the quote on the card and aren't materialized as sortable snapshots, so they aren't
    available for a whole cohort in one DB read."""

    ticker: str
    name: str | None
    market_cap: float | None
    pe_ratio: float | None
    ev_ebitda: float | None
    fcf_yield: float | None
    net_margin: float | None
    revenue_growth_yoy: float | None
    tier: MarketCapTier | None = None
    is_anchor: bool = False


@dataclass(frozen=True)
class PeerMedians:
    """The median of each comparison metric over the displayed cohort (the anchor and its peers).

    The reference line a client draws the anchor against — "is this stock's P/E rich or cheap
    *for this peer set*". Each is the median of the non-null values present in the cohort (so a
    metric several peers lack still yields a median from those that carry it), or ``None`` when
    no company in the cohort has it. Includes the anchor in the sample, matching how
    :class:`IndustryValuation` measures a stock against a group it belongs to."""

    pe_ratio: float | None
    ev_ebitda: float | None
    fcf_yield: float | None
    net_margin: float | None
    revenue_growth_yoy: float | None


@dataclass(frozen=True)
class PeerComparison:
    """A stock's valuation compared side-by-side with its industry, cap-tier-scoped peers.

    The named comparable-company table the industry P/E benchmark only ever summarized: the
    looked-up stock (``anchor``) beside the ``peers`` it's most comparable to — same industry,
    scoped to its own size tier and widened only as far as needed (a mega-cap against other
    mega/large-caps, not $2B minnows) — each on the same metrics, with the cohort ``medians`` as
    the reference. ``industry`` is the anchor's stored slug (``None`` when it isn't classified, in
    which case there are no peers to find); ``cohort`` names the size slice the peers were drawn
    from (``"mega"`` / ``"large/mega"`` / ``"industry"``), the same honesty label
    :class:`IndustryValuation.cohort` carries. ``anchor`` is ``None`` when the looked-up stock
    isn't in the screened universe (unscreened, so no row to compare) — the peer list still
    serves. Best-effort throughout: an unclassified or peerless stock is an empty comparison, not
    an error."""

    # The cohort widens (to neighbouring size tiers) until it holds at least this many peers
    # besides the anchor — enough for a median to mean "versus the group" rather than versus one
    # or two names. Deliberately looser than IndustryValuation's benchmark floor (which needs a
    # statistically representative sample); a comparison table is useful with a handful of named
    # peers, and the tier scoping matters more than the count.
    MIN_PEERS = 4
    # The most peers to show beside the anchor — the largest by market cap, so the table stays a
    # readable "you vs the giants of your industry" rather than a full sector dump.
    MAX_PEERS = 11

    ticker: str
    industry: str | None
    cohort: str
    anchor: PeerCompany | None
    peers: tuple[PeerCompany, ...]
    medians: PeerMedians

    @classmethod
    def build(
        cls, ticker: str, industry: str | None, candidates: Sequence[PeerCompany]
    ) -> "PeerComparison":
        """Assemble the comparison from every same-industry screened company (``candidates``,
        which includes the anchor itself).

        Splits the anchor out by ticker, scopes the remaining peers to the anchor's cap tier
        (widening to neighbouring tiers until at least ``MIN_PEERS`` are in the cohort — the same
        size-anchoring :meth:`IndustryValuation.for_stock_peers` uses, counting *rows* rather than
        usable P/Es), takes the ``MAX_PEERS`` largest by market cap, and medians every metric over
        the anchor-plus-peers cohort. An anchor missing from ``candidates`` (unscreened, or an
        unknown ticker) yields the whole-industry cohort with no anchor row."""
        anchor = next((c for c in candidates if c.ticker == ticker), None)
        others = [c for c in candidates if c.ticker != ticker]
        cohort, label = cls._select_cohort(anchor, others)
        cohort = sorted(cohort, key=lambda c: c.market_cap or 0.0, reverse=True)[
            : cls.MAX_PEERS
        ]
        displayed = ([anchor] if anchor is not None else []) + cohort
        return cls(
            ticker=ticker,
            industry=industry,
            cohort=label,
            anchor=replace(anchor, is_anchor=True) if anchor is not None else None,
            peers=tuple(cohort),
            medians=cls._medians(displayed),
        )

    @classmethod
    def _select_cohort(
        cls, anchor: PeerCompany | None, others: list[PeerCompany]
    ) -> tuple[list[PeerCompany], str]:
        """The peers to show and the size-slice label they were drawn from.

        Starts at the anchor's own tier and widens to the nearest neighbouring tiers until the
        cohort holds ``MIN_PEERS`` peers, falling back to the whole industry. The whole industry
        when the anchor has no tier (unscreened / unknown cap) or there are no tiered peers to
        anchor on."""
        order = _TIER_ORDER_ASC
        present = {c.tier for c in others if c.tier is not None}
        if anchor is None or anchor.tier is None or not present:
            return others, "industry"
        anchor_rank = order.index(anchor.tier)
        max_radius = max(abs(order.index(tier) - anchor_rank) for tier in present)
        for radius in range(max_radius + 1):
            allowed = {
                tier for i, tier in enumerate(order) if abs(i - anchor_rank) <= radius
            }
            cohort = [c for c in others if c.tier in allowed]
            if len(cohort) >= cls.MIN_PEERS or radius == max_radius:
                return cohort, _cohort_label(allowed & present, present)
        return others, "industry"  # unreachable: the loop always terminates at max_radius

    @staticmethod
    def _medians(companies: Sequence[PeerCompany]) -> PeerMedians:
        """The median of each metric over ``companies``, non-null values only."""

        def median(values: list[float]) -> float | None:
            return _percentile(sorted(values), 50)

        def column(attr: str) -> float | None:
            return median(
                [
                    value
                    for c in companies
                    if (value := getattr(c, attr)) is not None
                ]
            )

        return PeerMedians(
            pe_ratio=column("pe_ratio"),
            ev_ebitda=column("ev_ebitda"),
            fcf_yield=column("fcf_yield"),
            net_margin=column("net_margin"),
            revenue_growth_yoy=column("revenue_growth_yoy"),
        )


def _cohort_label(chosen: set[MarketCapTier], present: set[MarketCapTier]) -> str:
    """Name the size slice a peer cohort was drawn from, for :class:`IndustryValuation.cohort`.

    ``"industry"`` when the cohort spans every tier the industry actually has (a same-tier
    start that had to widen all the way, or an industry that's one tier deep — either way the
    benchmark *is* the whole industry). Otherwise the chosen tiers joined smallest-first, e.g.
    ``"mega"`` for a same-tier hit or ``"large/mega"`` for a one-step widen."""
    if not chosen or chosen == present:
        return "industry"
    return "/".join(tier.value for tier in _TIER_ORDER_ASC if tier in chosen)


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
    """The ``q``-th percentile (0–100) of an already-sorted sequence, by linear interpolation
    between the two nearest ranks — the common "type 7" definition (what numpy defaults to).

    Computed in Python rather than SQL on purpose: SQLite (the offline tests) has no median /
    percentile function, so the repository fetches the peer list and this summarizes it — one
    definition that behaves identically on SQLite and Postgres. ``None`` for an empty sample;
    the result is rounded to 2 dp, the precision the anchor stores P/Es at."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return round(sorted_values[0], 2)
    rank = (q / 100) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return round(sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo]), 2)


def slugify(label: object) -> str | None:
    """A raw classification label → a snake_case slug, or ``None``.

    Lower-cases, replaces each run of non-alphanumeric characters with a single ``_`` and
    strips leading/trailing underscores, turning display text into a stable key. A non-string
    or a label with no alphanumeric content (``""``, ``"—"``) collapses to ``None``. Idempotent
    on an already-slugged value, so the search use case can run an incoming ``sector`` /
    ``industry`` filter through it whether the client sends the raw label or the stored slug."""
    if not isinstance(label, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or None


# Yahoo `.info['country']` display name → ISO-2 code, for the issuer-domicile column the US /
# Canadian screener split filters on. Only ``US`` and ``CA`` need to be exact (they drive the
# split); the rest are here so a *foreign* issuer (a CDR's underlying) reads as a definite
# non-``CA`` / non-``US`` code rather than an unknown ``None`` — an unmapped country stays
# ``None`` and is treated leniently (shown in its listing market). Covers the domiciles that
# actually appear at ≥$1B on a North-American listing, including every common CDR-issuer home.
_COUNTRY_ISO2 = {
    "united states": "US",
    "canada": "CA",
    "united kingdom": "GB",
    "ireland": "IE",
    "switzerland": "CH",
    "germany": "DE",
    "france": "FR",
    "netherlands": "NL",
    "belgium": "BE",
    "luxembourg": "LU",
    "spain": "ES",
    "italy": "IT",
    "sweden": "SE",
    "denmark": "DK",
    "norway": "NO",
    "finland": "FI",
    "austria": "AT",
    "portugal": "PT",
    "greece": "GR",
    "japan": "JP",
    "china": "CN",
    "hong kong": "HK",
    "taiwan": "TW",
    "south korea": "KR",
    "korea, republic of": "KR",
    "india": "IN",
    "singapore": "SG",
    "australia": "AU",
    "new zealand": "NZ",
    "israel": "IL",
    "brazil": "BR",
    "mexico": "MX",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
    "peru": "PE",
    "south africa": "ZA",
    "united arab emirates": "AE",
    "saudi arabia": "SA",
    "turkey": "TR",
    "indonesia": "ID",
    "thailand": "TH",
    "malaysia": "MY",
    "philippines": "PH",
    "vietnam": "VN",
    "bermuda": "BM",
    "cayman islands": "KY",
    "jersey": "JE",
    "guernsey": "GG",
    "monaco": "MC",
    "cyprus": "CY",
    "panama": "PA",
    "uruguay": "UY",
    "puerto rico": "PR",
}


def country_to_iso2(country: object) -> str | None:
    """A Yahoo ``.info['country']`` display name → its ISO-2 code, or ``None``.

    Case/space-insensitive lookup against :data:`_COUNTRY_ISO2`; also accepts an already-ISO-2
    value (a two-letter string) so the mapping is idempotent. A non-string, a blank, or an
    unrecognized country collapses to ``None`` — an unknown domicile the search shows in its
    listing market rather than wrongly excluding."""
    if not isinstance(country, str):
        return None
    text = country.strip()
    if not text:
        return None
    mapped = _COUNTRY_ISO2.get(text.lower())
    if mapped is not None:
        return mapped
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return None


# Tokens dropped when reducing a company name to its identity key — legal-form suffixes (which
# vary between a listing and its cross-listing, "Corp" vs "Corporation") and depositary-receipt
# markers. Limited to legal-form + DR noise, NOT generic words like "Group"/"Holdings", so two
# genuinely different companies don't collapse onto the same key.
_NAME_NOISE_TOKENS = frozenset(
    {
        "THE",
        "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES",
        "LTD", "LIMITED", "LLC", "LP", "LLP", "PLC", "SA", "AG", "NV", "SE",
        "CDR", "CDRS", "DEPOSITARY", "DEPOSITORY", "RECEIPT", "RECEIPTS",
        "CAD", "USD", "HEDGED", "UNHEDGED", "NONHEDGED",
    }
)


def normalize_company_name(name: object) -> str | None:
    """A company name → a canonical identity key, or ``None``.

    Upper-cases, splits on every run of non-alphanumeric characters, drops the legal-form and
    depositary-receipt noise tokens (:data:`_NAME_NOISE_TOKENS`), and joins the rest — so a
    Canadian listing and its US line reduce to the same key (``"Apple Inc."`` → ``APPLE`` on both
    the US ``AAPL`` row and its ``AAPL.TO`` CDR). The universe sync uses this to spot a Canadian
    listing that duplicates a **US-domiciled** company (a CDR / cross-listing of a US company) and
    keep it out of the Canadian screen. A non-string, or a name that is only noise tokens, is
    ``None`` — nothing to match on."""
    if not isinstance(name, str):
        return None
    tokens = [
        token
        for token in re.split(r"[^A-Z0-9]+", name.upper())
        if token and token not in _NAME_NOISE_TOKENS
    ]
    return "".join(tokens) or None
