from pydantic import BaseModel, Field


class StockSearchItemResponse(BaseModel):
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
    # True for a Canadian listing that duplicates a US-listed company (a CDR or a same-ticker
    # dual-listing). Hidden from search by default, so this is False on every returned row
    # unless the client opted in with ?include_interlisted=true.
    has_us_listing: bool = False


class StockSearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    count: int
    results: list[StockSearchItemResponse]


class AiScreenInterpretationResponse(BaseModel):
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
    interpreted: AiScreenInterpretationResponse


class ClassificationsResponse(BaseModel):
    sectors: list[str]
    industries: list[str]


class PeerCompanyResponse(BaseModel):
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
    pe_ratio: float | None = None
    ev_ebitda: float | None = None
    fcf_yield: float | None = None
    net_margin: float | None = None
    revenue_growth_yoy: float | None = None


class PeerComparisonResponse(BaseModel):
    ticker: str
    industry: str | None = None
    cohort: str
    count: int  # number of peers shown (excludes the anchor)
    anchor: PeerCompanyResponse | None = None
    peers: list[PeerCompanyResponse]
    medians: PeerMediansResponse


class IndustryValuationResponse(BaseModel):
    industry: str
    count: int
    median_pe: float | None = None
    p25_pe: float | None = None
    p75_pe: float | None = None
