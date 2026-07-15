"""HTTP response DTOs for the universe read endpoints.

Pydantic models at the edge, deliberately separate from the slice ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic (the same split the
other slices keep). These back ``GET /stocks/ticker`` (the search list) and
``GET /stocks/classifications`` (the filter menus).
"""

from pydantic import BaseModel, Field


class StockSearchItemResponse(BaseModel):
    """One row of a universe search — anchor facts only, no live price.

    ``market_cap`` is raw USD; ``pe_ratio`` is the trailing P/E on the analyst-consensus
    (adjusted) basis — the same figure the ticker card serves, materialized here for sorting;
    ``ev_ebitda`` is the materialized EV/EBITDA snapshot (enterprise value at the screen-time
    market cap over trailing EBITDA — the capital-structure-neutral cousin of ``pe_ratio``,
    signed so a net-cash name reads negative); ``revenue_growth_yoy`` / ``eps_growth_yoy`` are
    the annual slice's latest trailing year-over-year growth and ``forward_revenue_growth_yoy`` /
    ``forward_eps_growth_yoy`` its forward (FY1→FY2 consensus) counterparts (all percent, EPS on
    the analyst-consensus basis); ``in_sp500`` / ``in_nasdaq100`` are definite booleans.
    Everything but the flags and the ticker can be ``null`` until the enriching sync / annual
    slice reaches the stock (the forward pair the most often, since it needs two upcoming years;
    ``pe_ratio`` also until four quarters are cached, and for a trailing-year loss; ``ev_ebitda``
    until the fundamentals slice has landed the EBITDA, and on a non-positive EBITDA). The FE
    fetches a live quote or the full card per row on demand via ``GET /stocks/ticker/{ticker}``.

    ``country`` / ``currency`` are the row's market (ISO-2 / ISO-3): the listing market and the
    currency ``market_cap`` is quoted in. ``market_cap`` is raw whole units of that ``currency``
    (USD for a US row, CAD for a TSX one) — the ≥$1B floor is applied in each market's native
    currency, so a client reads a CAD cap against ``currency``, and keeps a market-cap sort within
    one currency by filtering ``?country=``.
    """

    ticker: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None  # raw, in the row's trading `currency`
    pe_ratio: float | None = None  # trailing P/E, consensus basis (matches the card)
    fcf_yield: float | None = None  # percent, materialized FCF yield (signed; sortable)
    ev_ebitda: float | None = None  # materialized EV/EBITDA snapshot (signed; sortable)
    revenue_growth_yoy: float | None = None  # percent, latest trailing YoY
    eps_growth_yoy: float | None = None  # percent, latest trailing YoY, consensus basis
    fcf_growth_yoy: float | None = None  # percent, latest trailing FCF/share YoY
    forward_revenue_growth_yoy: float | None = None  # percent, forward FY1→FY2 consensus
    forward_eps_growth_yoy: float | None = None  # percent, forward FY1→FY2 consensus
    in_sp500: bool
    in_nasdaq100: bool
    country: str | None = None  # ISO-2 listing market (US / CA)
    currency: str | None = None  # ISO-3 trading currency (USD / CAD)


class StockSearchResponse(BaseModel):
    """A page of search results plus the pagination envelope.

    ``total`` is the full match count before the window (so the FE can size its pager),
    ``count`` the number of rows in ``results`` this page, and ``limit`` / ``offset`` echo the
    window the page was cut with — so a client reading only the response knows where it is.
    """

    total: int
    limit: int
    offset: int
    count: int
    results: list[StockSearchItemResponse]


class AiScreenInterpretationResponse(BaseModel):
    """The filters an AI screen resolved a plain-English request into — echoed back so the FE
    can show the user *what was applied* and let them tweak it in the manual controls (an AI
    screen isn't a black box). Every field is a plain, serializable value that maps one-to-one
    onto the manual search's query params: ``sort`` / ``direction`` and the ``market_cap_tiers``
    are the same slug strings ``GET /stocks/ticker`` accepts, ``sectors`` / ``industries`` the
    stored slugs. All optional/empty when the request didn't call for them (an all-unset
    interpretation is a neutral browse)."""

    query: str | None = None
    sectors: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    in_sp500: bool | None = None
    in_nasdaq100: bool | None = None
    market_cap_tiers: list[str] = Field(default_factory=list)
    sort: str | None = None
    direction: str
    limit: int | None = None


class AiScreenResponse(BaseModel):
    """The response to ``GET /stocks/ai-search``: the AI's reading of the request as filters.

    Just the ``interpreted`` filters — the endpoint does not run the search itself. The client
    applies these to the manual ``GET /stocks/ticker`` search (the same query params) to fetch
    the rows, so the AI leg only ever decides *which filters to set* and the FE can surface and
    edit them.
    """

    interpreted: AiScreenInterpretationResponse


class ClassificationsResponse(BaseModel):
    """The distinct sector and industry slugs present in the universe — the FE's filter menus.

    Two flat, sorted lists; the search endpoint accepts the same slugs back as its ``sector`` /
    ``industry`` filters.
    """

    sectors: list[str]
    industries: list[str]


class PeerCompanyResponse(BaseModel):
    """One row of a peer comparison — a company on the shared metric columns.

    ``market_cap`` is raw USD; ``pe_ratio`` the trailing P/E (consensus basis) and ``ev_ebitda``
    the EV/EBITDA snapshot (signed) — the same materialized figures the search sorts on;
    ``fcf_yield`` / ``net_margin`` / ``revenue_growth_yoy`` are percent. Every metric is ``null``
    until the enriching syncs reach the company, so a sparse peer shows blank cells rather than
    dropping out. ``is_anchor`` is ``true`` on the looked-up stock so a client can highlight it.
    """

    ticker: str
    name: str | None = None
    market_cap: float | None = None  # raw USD
    pe_ratio: float | None = None  # trailing P/E, consensus basis
    ev_ebitda: float | None = None  # EV/EBITDA snapshot (signed)
    fcf_yield: float | None = None  # percent, signed
    net_margin: float | None = None  # percent
    revenue_growth_yoy: float | None = None  # percent, latest trailing
    is_anchor: bool = False


class PeerMediansResponse(BaseModel):
    """The median of each metric over the displayed cohort (the anchor and its peers).

    The reference a client draws the anchor against — where its multiple sits versus the peer
    set. Each is ``null`` when no company in the cohort carries that metric.
    """

    pe_ratio: float | None = None
    ev_ebitda: float | None = None
    fcf_yield: float | None = None
    net_margin: float | None = None
    revenue_growth_yoy: float | None = None


class PeerComparisonResponse(BaseModel):
    """A stock compared side-by-side with its industry, cap-tier-scoped peers.

    ``ticker`` echoes the looked-up (normalized) symbol; ``industry`` is its stored slug
    (``null`` when it isn't classified — then there are no peers). ``cohort`` names the size slice
    the peers were drawn from (``"mega"`` / ``"large/mega"`` / ``"industry"``), so a peer set
    scoped to the mega-caps doesn't read as the whole industry. ``anchor`` is the looked-up stock's
    own row (``null`` when it isn't a screened member), ``peers`` the comparables (largest by
    market cap first), and ``medians`` the cohort reference line. ``count`` is the number of peers
    shown. An unclassified or peerless stock is an empty comparison (a 200), not a 404.
    """

    ticker: str
    industry: str | None = None
    cohort: str
    count: int  # number of peers shown (excludes the anchor)
    anchor: PeerCompanyResponse | None = None
    peers: list[PeerCompanyResponse]
    medians: PeerMediansResponse


class IndustryValuationResponse(BaseModel):
    """A per-industry trailing-P/E benchmark over the screened universe.

    ``median_pe`` is the industry's typical trailing P/E (consensus basis — the same figure
    the search sorts on and the ticker card serves) and ``p25_pe`` / ``p75_pe`` the
    interquartile range, so a client can see where a given stock's multiple sits relative to
    its peers — the anchor that makes an absolute P/E meaningful. ``count`` is how many peers
    had a usable (positive) P/E; all three stats are ``null`` when it's 0 (an unknown industry,
    or none valued yet). ``industry`` echoes the normalized slug.
    """

    industry: str
    count: int
    median_pe: float | None = None
    p25_pe: float | None = None
    p75_pe: float | None = None
