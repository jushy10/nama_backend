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


@dataclass(frozen=True)
class ScreenedStock:
    """One company in the screened universe.

    ``market_cap`` is in whole dollars (e.g. ``3.01e12`` for a $3.01T company). Everything
    but the ``ticker`` is optional: ``exchange`` comes from the screen, ``sector`` may be
    absent (the yfinance screen doesn't publish it, so it rides in ``None``), and the name
    may be missing.

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


@dataclass(frozen=True)
class CompanyClassification:
    """A stock's sector + industry, as canonical snake_case slugs.

    The screen (``ScreenedStock``) carries neither — Yahoo publishes sector/industry only on
    the per-ticker ``.info`` surface — so this is the shape the sync's enrichment pass fetches
    and persists. Both sides are optional: a symbol Yahoo doesn't classify (or only half
    classifies) yields ``None`` for the missing side, which the sync leaves for a later run.

    Labels are stored as slugs — lower-cased, with every run of non-alphanumeric characters
    collapsed to a single underscore (``"Consumer Electronics"`` → ``consumer_electronics``,
    ``"Oil & Gas E&P"`` → ``oil_gas_e_p``) — a stable, join-friendly key rather than Yahoo's
    display text. ``from_labels`` is the constructor callers use, so the slug rule lives in
    one place.
    """

    sector: str | None = None
    industry: str | None = None

    @classmethod
    def from_labels(cls, sector: object, industry: object) -> "CompanyClassification":
        """Build a classification from raw vendor labels, each slugged to snake_case (and
        dropped to ``None`` when blank or non-string)."""
        return cls(sector=slugify(sector), industry=slugify(industry))


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
      yield).
    - **Anchor facts** — ``market_cap`` (the universe screen's figure) and the clean display
      ``name``, which replace the live Finnhub fundamentals/profile calls the analysis used to
      make.

    Read DB-only so every figure is the canonical one the ticker card and universe search show,
    or absent — never a divergent live-vendor number. Every field is nullable (unset until the
    slice's sync reaches the stock). A read-model, the multi-column sibling of
    ``industry_for_ticker`` / ``tier_for_ticker``.
    """

    fcf_per_share: float | None = None
    revenue_growth_yoy: float | None = None
    eps_growth_yoy: float | None = None
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
    surfaces the cheapest on cash (highest yield) first. The value → ORM column/expression
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
    forward growth, or a ``pe_ratio`` until the enriching sync/annual slice reaches it (forward
    growth the most often, since it needs two upcoming years; ``pe_ratio`` stays null until the
    quarterly cache holds four reported quarters, and for a trailing-year loss).
    """

    ticker: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: float | None
    pe_ratio: float | None
    fcf_yield: float | None
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    fcf_growth_yoy: float | None
    forward_revenue_growth_yoy: float | None
    forward_eps_growth_yoy: float | None
    in_sp500: bool
    in_nasdaq100: bool


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
