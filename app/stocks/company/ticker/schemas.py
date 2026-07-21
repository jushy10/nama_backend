from datetime import date, datetime

from pydantic import BaseModel

from app.stocks.schemas import StockPerformanceResponse


class ExtendedHoursResponse(BaseModel):
    session: str  # "pre_market" | "after_hours"
    price: float  # the latest extended-hours print
    change: float | None = None  # extended move: price vs the regular close
    change_percent: float | None = None
    regular_price: float  # the regular-session (16:00 ET) close — the primary number
    regular_change: float | None = None  # the day's move: regular close vs previous close
    regular_change_percent: float | None = None
    as_of: datetime | None = None  # the extended trade's timestamp


class DividendResponse(BaseModel):
    yield_percentage: float | None = None  # percent, rounded to 2 decimals
    per_share: float | None = None  # $ per share annual, rounded to 2 decimals


class TickerMetricsResponse(BaseModel):
    # Valuation
    pe: float | None = None  # trailing: price / TTM EPS (consensus basis, 4 quarters)
    pb: float | None = None  # trailing: price / book value per share
    ps: float | None = None  # trailing: price / sales per share
    peg: float | None = None  # trailing: pe / eps_growth_yoy (consensus basis)
    eps: float | None = None  # trailing TTM EPS (consensus basis), the pe denominator
    forward_pe: float | None = None  # forward: price / FY1 consensus EPS
    forward_ps: float | None = None  # forward: market cap / FY1 consensus revenue
    enterprise_value: float | None = None  # live: price * shares + debt - cash (raw USD)
    ev_ebitda: float | None = None  # live: enterprise value / trailing EBITDA (null if EBITDA <= 0)
    # Cash flow
    price_to_fcf: float | None = None  # trailing: price / FCF per share (null if FCF <= 0)
    fcf_yield: float | None = None  # percent: FCF per share / price (signed)
    ocf_yield: float | None = None  # percent: OCF per share / price (signed; pre-capex)
    # Profitability & health
    gross_margin: float | None = None  # percent
    operating_margin: float | None = None  # percent
    net_margin: float | None = None  # percent
    roe: float | None = None  # percent, return on equity
    current_ratio: float | None = None  # current assets / current liabilities
    debt_to_equity: float | None = None  # total debt / equity (a ratio)
    beta: float | None = None  # volatility vs the market (1.0 = moves with it)
    # Growth
    revenue_growth_yoy: float | None = None  # percent, latest trailing YoY (annual slice)
    eps_growth_yoy: float | None = None  # percent, latest trailing YoY, consensus basis
    fcf_growth_yoy: float | None = None  # percent, latest trailing FCF/share YoY (annual slice)
    forward_revenue_growth_yoy: float | None = None  # percent, forward FY1->FY2 consensus
    forward_eps_growth_yoy: float | None = None  # percent, forward FY1->FY2 consensus


class OptionsMetricsResponse(BaseModel):
    implied_volatility: float | None = None  # ATM IV at the near expiry, percent
    expected_move_percent: float | None = None  # priced-in swing, percent of spot
    expected_move_by: date | None = None  # the ~1-month expiry sampled
    insurance_cost_percent: float | None = None  # ATM protective put, percent of spot
    insurance_expires: date | None = None  # the ~3-month expiry sampled
    put_call_ratio: float | None = None  # today's put volume / call volume


class TickerCardResponse(BaseModel):
    ticker: str
    name: str | None = None  # clean display name ("Micron Technology")
    exchange: str | None = None  # listing venue (e.g. "NASDAQ"); DB-backed
    asset_type: str  # "etf" if in the ETF universe, else "equity" — always present
    price: float
    change: float | None = None  # absolute move vs the previous close
    change_percent: float | None = None  # percent move vs the previous close
    # The extended-hours split (regular close + latest pre/after print), present only outside
    # the regular session; null during it and on the Canadian feed. Lets the FE show the day's
    # move and the after-bell move apart rather than blended into price/change above.
    extended_hours: ExtendedHoursResponse | None = None
    market_cap: float | None = None  # raw USD; from the stocks anchor (universe screen)
    sector: str | None = None  # classification slug; from the stocks anchor
    industry: str | None = None  # classification slug; from the stocks anchor
    dividend: DividendResponse | None = None  # opt-in: ?include=dividend
    performance: StockPerformanceResponse | None = None  # opt-in: ?include=performance
    metrics: TickerMetricsResponse | None = None  # opt-in: ?include=metrics
    options_metrics: OptionsMetricsResponse | None = None  # opt-in: ?include=options_metrics


class PeHistoryPointResponse(BaseModel):
    date: date  # the announcement date the P/E is anchored on
    price: float  # close on/near that date
    ttm_eps: float  # trailing 4 reported quarters' EPS
    pe: float  # price / ttm_eps


class PeHistoryStatsResponse(BaseModel):
    current_pe: float
    median_pe: float
    p25_pe: float
    p75_pe: float
    min_pe: float
    max_pe: float
    current_percentile: float  # 0–100, share of history at or below the current multiple
    discount_to_median_percent: float  # negative = cheaper than its own median
    signal: str  # "cheap" | "fair" | "expensive" | "not_meaningful" (trough earnings)
    sample_size: int


class PeHistoryResponse(BaseModel):
    ticker: str
    count: int  # number of points (may be fewer than the reported quarters)
    points: list[PeHistoryPointResponse]  # oldest first
    stats: PeHistoryStatsResponse | None = None  # valuation-vs-history read; null for a thin series


class TickerTypeResponse(BaseModel):
    ticker: str
    asset_type: str  # "etf" if in the ETF universe, else "equity"