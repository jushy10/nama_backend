from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

# Canadian venue suffixes, in Yahoo's convention — the marker that a ticker is a Canadian
# listing (TSX ``.TO`` / TSX Venture ``.V`` / Cboe Canada ``.NE`` / CSE ``.CN``), the form the
# universe screen stores. Two domain rules ride on them: routing a per-symbol price read
# (US → Alpaca / CA → Yahoo), and deriving a listing's *base* (US-equivalent) ticker so an
# interlisted Canadian listing can be matched to its US sibling and deduped.
CANADIAN_SUFFIXES = (".TO", ".V", ".NE", ".CN")

# Cboe Canada (NEO), Yahoo suffix ``.NE`` — the venue Canadian Depositary Receipts list on. A CDR
# wraps a US / foreign company (``INTC.NE`` → Intel, ``ZAAP.NE`` → Apple), not a Canadian one, so
# the universe deliberately keeps ``.NE`` out of the Canadian screen entirely (genuine Canadian
# companies list on TSX ``.TO`` / TSXV ``.V``).
CBOE_CANADA_SUFFIX = ".NE"


def is_canadian(symbol: str) -> bool:
    if not isinstance(symbol, str):
        return False
    upper = symbol.upper()
    return any(upper.endswith(suffix) for suffix in CANADIAN_SUFFIXES)


def is_cboe_canada(symbol: str) -> bool:
    return isinstance(symbol, str) and symbol.upper().endswith(CBOE_CANADA_SUFFIX)


def base_ticker(symbol: str) -> str:
    if not isinstance(symbol, str):
        return symbol
    upper = symbol.upper()
    for suffix in CANADIAN_SUFFIXES:
        if upper.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def normalize_symbol(symbol: str, *, kind: str = "stock", article: str = "A") -> str:
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError(f"{article} {kind} symbol is required.")
    base = base_ticker(normalized)  # strip a Canadian venue suffix, if present
    if not _is_valid_base(base):
        raise ValueError(f"'{symbol}' is not a valid {kind} symbol.")
    return normalized


def _is_valid_base(base: str) -> bool:
    root, dash, series = base.partition("-")
    if not root.isalpha() or len(root) > 5:
        return False
    if not dash:
        return True
    return series.isalpha() and len(series) <= 3


class Timeframe(str, Enum):
    MIN_1 = "1Min"
    MIN_5 = "5Min"
    MIN_15 = "15Min"
    MIN_30 = "30Min"
    HOUR_1 = "1Hour"
    HOUR_4 = "4Hour"
    DAY_1 = "1Day"
    WEEK_1 = "1Week"
    MONTH_1 = "1Month"


@dataclass(frozen=True)
class StockPerformance:
    one_week: float | None
    one_month: float | None
    three_month: float | None
    six_month: float | None
    ytd: float | None
    one_year: float | None


@dataclass(frozen=True)
class KeyMetrics:
    # Valuation
    pe: float | None = None  # price / trailing EPS
    pb: float | None = None  # price / book value
    ps: float | None = None  # price / sales
    ev_to_ebitda: float | None = None  # enterprise value / trailing EBITDA (capital-structure-neutral)
    eps: float | None = None  # trailing earnings per share
    fcf_per_share: float | None = None  # trailing free cash flow per share
    ocf_per_share: float | None = None  # trailing operating cash flow per share (pre-capex)
    # Profitability (percent)
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    roe: float | None = None  # return on equity (percent)
    # Financial health
    current_ratio: float | None = None  # current assets / current liabilities
    debt_to_equity: float | None = None  # total debt / equity
    # Growth (percent, year over year)
    eps_growth_yoy: float | None = None
    revenue_growth_yoy: float | None = None
    fcf_growth_yoy: float | None = None  # trailing free-cash-flow-per-share growth (percent)
    # Market / price
    beta: float | None = None  # volatility vs the market (1.0 = moves with it)
    week_52_high: float | None = None
    week_52_low: float | None = None

    @property
    def peg(self) -> float | None:
        if self.pe is None or self.eps_growth_yoy is None:
            return None
        if self.pe <= 0 or self.eps_growth_yoy <= 0:
            return None
        return round(self.pe / self.eps_growth_yoy, 2)


def _forward_one_year_growth(fy1: float | None, fy2: float | None) -> float | None:
    if fy1 is None or fy2 is None or fy1 <= 0 or fy2 <= 0:
        return None
    return round((fy2 / fy1 - 1) * 100, 2)


@dataclass(frozen=True)
class AnalystEstimates:
    fiscal_year: int | None  # FY1: the nearest forward fiscal year
    period_end: date | None  # FY1 fiscal period-end date
    eps_avg: float | None  # FY1 consensus EPS (mean estimate)
    revenue_avg: float | None  # FY1 consensus revenue (raw, e.g. USD)
    fiscal_year_fy2: int | None = None  # FY2: the fiscal year after FY1
    eps_avg_fy2: float | None = None  # FY2 consensus EPS
    revenue_avg_fy2: float | None = None  # FY2 consensus revenue (raw)

    @property
    def is_empty(self) -> bool:
        return self.eps_avg is None and self.revenue_avg is None

    def forward_pe(self, price: float | None) -> float | None:
        if price is None or self.eps_avg is None or self.eps_avg <= 0:
            return None
        return round(price / self.eps_avg, 2)

    def forward_ps(self, market_cap: float | None) -> float | None:
        if market_cap is None or not self.revenue_avg or self.revenue_avg <= 0:
            return None
        return round(market_cap / self.revenue_avg, 2)

    def forward_eps_growth(self) -> float | None:
        return _forward_one_year_growth(self.eps_avg, self.eps_avg_fy2)

    def forward_revenue_growth(self) -> float | None:
        return _forward_one_year_growth(self.revenue_avg, self.revenue_avg_fy2)


@dataclass(frozen=True)
class GrowthMetrics:
    revenue_yoy: float | None = None  # trailing 1-yr revenue growth (percent)
    eps_yoy: float | None = None  # trailing 1-yr EPS growth (percent)
    forward_revenue_growth: float | None = None  # expected next-yr revenue growth, FY1→FY2 (percent)
    forward_eps_growth: float | None = None  # expected next-yr EPS growth, FY1→FY2 (percent)

    @classmethod
    def build(
        cls,
        metrics: "KeyMetrics | None",
        estimates: "AnalystEstimates | None",
    ) -> "GrowthMetrics | None":
        rev_yoy = metrics.revenue_growth_yoy if metrics else None
        eps_yoy = metrics.eps_growth_yoy if metrics else None
        fwd_rev = estimates.forward_revenue_growth() if estimates else None
        fwd_eps = estimates.forward_eps_growth() if estimates else None
        if all(v is None for v in (rev_yoy, eps_yoy, fwd_rev, fwd_eps)):
            return None
        return cls(
            revenue_yoy=rev_yoy,
            eps_yoy=eps_yoy,
            forward_revenue_growth=fwd_rev,
            forward_eps_growth=fwd_eps,
        )


@dataclass(frozen=True)
class AllTimeHigh:
    price: float  # highest intraday price over the available history
    reached_on: date | None  # the day that high was reached
    since: date | None  # earliest date the underlying history covers (the bound)


@dataclass(frozen=True)
class Stock:
    symbol: str
    name: str | None
    exchange: str | None
    price: float  # latest trade price
    open: float | None
    high: float | None
    low: float | None
    previous_close: float | None
    volume: int | None
    bid: float | None
    ask: float | None
    as_of: datetime | None
    # Enrichment beyond the raw snapshot; optional so the price-only view of a
    # Stock stays valid when these sources are unavailable (best-effort).
    market_cap: float | None = None
    dividend_per_share: float | None = None
    dividend_yield: float | None = None
    performance: StockPerformance | None = None
    metrics: KeyMetrics | None = None  # trailing valuation/health/market ratios
    analyst_estimates: AnalystEstimates | None = None  # forward consensus (FY1/FY2)
    all_time_high: AllTimeHigh | None = None

    @property
    def change(self) -> float | None:
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return round(self.ask - self.bid, 4)

    @property
    def drawdown_from_high(self) -> float | None:
        if self.all_time_high is None or not self.all_time_high.price:
            return None
        high = self.all_time_high.price
        return round((self.price - high) / high * 100, 2)

    @property
    def forward_pe(self) -> float | None:
        if self.analyst_estimates is None:
            return None
        return self.analyst_estimates.forward_pe(self.price)

    @property
    def forward_ps(self) -> float | None:
        if self.analyst_estimates is None:
            return None
        return self.analyst_estimates.forward_ps(self.market_cap)

    @property
    def growth(self) -> GrowthMetrics | None:
        return GrowthMetrics.build(self.metrics, self.analyst_estimates)


# US market-session windows in Eastern Time — the business rule for labelling *which
# session a trade printed in*, so a regular-session close can be told apart from an
# extended-hours (pre/after) print when splitting a quote. Deliberately coarse: the
# standard extended-hours window (04:00 pre, 20:00 after), no half-day/holiday nuance —
# that "is the market open right now" judgement belongs to the client's own live clock
# (the frontend's market.ts), which this only feeds a labelled *price* to complement.
_MARKET_TZ = ZoneInfo("America/New_York")
_PRE_OPEN_MIN = 4 * 60  # 04:00 ET
_REGULAR_OPEN_MIN = 9 * 60 + 30  # 09:30 ET
_REGULAR_CLOSE_MIN = 16 * 60  # 16:00 ET
_AFTER_CLOSE_MIN = 20 * 60  # 20:00 ET


class MarketSession(str, Enum):
    PRE_MARKET = "pre_market"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"
    CLOSED = "closed"


def market_session_at(moment: datetime) -> MarketSession:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    et = moment.astimezone(_MARKET_TZ)
    if et.weekday() >= 5:  # Saturday / Sunday
        return MarketSession.CLOSED
    minutes = et.hour * 60 + et.minute
    if _PRE_OPEN_MIN <= minutes < _REGULAR_OPEN_MIN:
        return MarketSession.PRE_MARKET
    if _REGULAR_OPEN_MIN <= minutes < _REGULAR_CLOSE_MIN:
        return MarketSession.REGULAR
    if _REGULAR_CLOSE_MIN <= minutes < _AFTER_CLOSE_MIN:
        return MarketSession.AFTER_HOURS
    return MarketSession.CLOSED


@dataclass(frozen=True)
class ExtendedHours:
    session: MarketSession  # PRE_MARKET or AFTER_HOURS — the window the print occurred in
    price: float  # the latest extended-hours trade price
    regular_close: float  # the regular-session (16:00 ET) close, the anchor to show as primary
    as_of: datetime | None  # the extended trade's timestamp

    @property
    def change(self) -> float | None:
        if not self.regular_close:
            return None
        return round(self.price - self.regular_close, 4)

    @property
    def change_percent(self) -> float | None:
        if not self.regular_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.regular_close) / self.regular_close * 100, 2)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float  # latest trade price (an extended-hours print when one is more recent)
    previous_close: float | None
    bid: float | None
    ask: float | None
    as_of: datetime | None
    # The regular-session (16:00 ET) close, when the feed carries it. Distinct from
    # ``price`` in pre/after-hours (where ``price`` is the extended print): it's the anchor
    # the extended move is measured against. ``None`` for feeds that don't split the day
    # (the Canadian Yahoo feed) — those simply never surface an extended-hours block.
    regular_close: float | None = None

    @property
    def change(self) -> float | None:
        if self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        if not self.previous_close:  # None or 0 -> undefined
            return None
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)

    @property
    def regular_change(self) -> float | None:
        if self.regular_close is None or self.previous_close is None:
            return None
        return round(self.regular_close - self.previous_close, 4)

    @property
    def regular_change_percent(self) -> float | None:
        if self.regular_close is None or not self.previous_close:
            return None
        return round((self.regular_close - self.previous_close) / self.previous_close * 100, 2)

    @property
    def extended_hours(self) -> "ExtendedHours | None":
        if self.regular_close is None or self.as_of is None:
            return None
        session = market_session_at(self.as_of)
        if session not in (MarketSession.PRE_MARKET, MarketSession.AFTER_HOURS):
            return None
        return ExtendedHours(
            session=session,
            price=self.price,
            regular_close=self.regular_close,
            as_of=self.as_of,
        )

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return round(self.ask - self.bid, 4)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime  # the bar's opening time (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: int | None

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass(frozen=True)
class CandleSeries:
    symbol: str
    timeframe: Timeframe
    candles: tuple[Candle, ...]
