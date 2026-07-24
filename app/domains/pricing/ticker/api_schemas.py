from datetime import date, datetime

from pydantic import BaseModel

from app.domains.shared.entities import Quote
from app.domains.shared.schemas import StockPerformanceResponse
from app.domains.pricing.ticker.entities import (
    PeHistory,
    PeHistoryPoint,
    PeHistoryStats,
    TickerCard,
    TickerClassification,
    TickerOptionsMetrics,
)


def _round2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _dividend_yield(
    dividend_per_share: float | None, price: float | None
) -> float | None:
    if dividend_per_share is None or not price or price <= 0:
        return None
    return round(dividend_per_share / price * 100, 2)


class ExtendedHoursResponse(BaseModel):
    session: str  # "pre_market" | "after_hours"
    price: float  # the latest extended-hours print
    change: float | None = None  # extended move: price vs the regular close
    change_percent: float | None = None
    regular_price: float  # the regular-session (16:00 ET) close — the primary number
    regular_change: float | None = None  # the day's move: regular close vs previous close
    regular_change_percent: float | None = None
    as_of: datetime | None = None  # the extended trade's timestamp

    @classmethod
    def from_quote(cls, quote: Quote) -> "ExtendedHoursResponse | None":
        ext = quote.extended_hours
        if ext is None:
            return None
        return cls(
            session=ext.session.value,
            price=ext.price,
            change=ext.change,
            change_percent=ext.change_percent,
            regular_price=ext.regular_close,
            regular_change=quote.regular_change,
            regular_change_percent=quote.regular_change_percent,
            as_of=ext.as_of,
        )


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

    @classmethod
    def from_card(cls, card: TickerCard) -> "TickerMetricsResponse":
        # The trailing P/E rides the valuation (the quarterly slice's TTM sum on the
        # adjusted EPS basis, deliberately NOT a GAAP-ish TTM read); the margins and every
        # other figure here ride the same anchor read — no live vendor call.
        valuation = card.valuation
        return cls(
            # The price-anchored multiples (P/E, P/B, P/S, PEG, the FCF/OCF reads) all ride
            # the valuation — live price / the anchor's stored per-share inputs — so they sit
            # on one live quote. The entity owns the positivity guards; the presenter just reads.
            pe=valuation.trailing_pe if valuation else None,
            pb=valuation.pb if valuation else None,
            ps=valuation.ps if valuation else None,
            peg=valuation.peg if valuation else None,
            eps=_round2(valuation.ttm_eps) if valuation else None,
            # Forward multiples off the annual slice's stored forward consensus (already
            # rounded by the entity's forward_pe/forward_ps).
            forward_pe=card.forward_pe,
            forward_ps=card.forward_ps,
            # Enterprise value + EV/EBITDA, priced live off the quote (the entity rounds
            # ev_ebitda; EV is a large dollar figure served raw for the FE to scale).
            enterprise_value=valuation.enterprise_value if valuation else None,
            ev_ebitda=valuation.ev_to_ebitda if valuation else None,
            price_to_fcf=valuation.price_to_fcf if valuation else None,
            fcf_yield=valuation.fcf_yield if valuation else None,
            ocf_yield=valuation.ocf_yield if valuation else None,
            # The trailing ratios ride the anchor read; margins/ROE rounded here at the edge.
            gross_margin=_round2(card.gross_margin),
            operating_margin=_round2(card.operating_margin),
            net_margin=_round2(card.net_margin),
            roe=_round2(card.roe),
            current_ratio=_round2(card.current_ratio),
            debt_to_equity=_round2(card.debt_to_equity),
            beta=_round2(card.beta),
            # The YoY figures (trailing + forward) ride the anchor read (already rounded percent).
            revenue_growth_yoy=card.revenue_growth_yoy,
            eps_growth_yoy=card.eps_growth_yoy,
            fcf_growth_yoy=card.fcf_growth_yoy,
            forward_revenue_growth_yoy=card.forward_revenue_growth_yoy,
            forward_eps_growth_yoy=card.forward_eps_growth_yoy,
        )


class OptionsMetricsResponse(BaseModel):
    implied_volatility: float | None = None  # ATM IV at the near expiry, percent
    expected_move_percent: float | None = None  # priced-in swing, percent of spot
    expected_move_by: date | None = None  # the ~1-month expiry sampled
    insurance_cost_percent: float | None = None  # ATM protective put, percent of spot
    insurance_expires: date | None = None  # the ~3-month expiry sampled
    put_call_ratio: float | None = None  # today's put volume / call volume

    @classmethod
    def from_metrics(
        cls, metrics: TickerOptionsMetrics | None
    ) -> "OptionsMetricsResponse | None":
        if metrics is None:
            return None
        # Rounded here at the edge like the dividend: these are display figures
        # (percents, a ratio) and the chain arithmetic carries float noise.
        return cls(
            implied_volatility=_round2(metrics.implied_volatility),
            expected_move_percent=_round2(metrics.expected_move_percent),
            expected_move_by=metrics.expected_move_by,
            insurance_cost_percent=_round2(metrics.insurance_cost_percent),
            insurance_expires=metrics.insurance_expires,
            put_call_ratio=_round2(metrics.put_call_ratio),
        )


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

    @classmethod
    def from_card(cls, card: TickerCard) -> "TickerCardResponse":
        # The card carries the include set so this presenter can tell "not requested"
        # (a null block) from "requested but unavailable" (a block with null fields).
        dividend = None
        if "dividend" in card.include:
            # The dividend per share rides the anchor read (fundamentals slice); the yield is
            # priced here on the live quote (annual dividend / price), the same "store the
            # input, price it live" split the P/E and FCF yield use. Rounded here at the edge —
            # a dividend card shows cents / basis-point-ish precision.
            dividend = DividendResponse(
                yield_percentage=_dividend_yield(
                    card.dividend_per_share, card.quote.price
                ),
                per_share=_round2(card.dividend_per_share),
            )
        return cls(
            ticker=card.quote.symbol,
            name=card.name,
            exchange=card.exchange,
            asset_type=card.asset_type,
            price=card.quote.price,
            change=card.quote.change,
            change_percent=card.quote.change_percent,
            extended_hours=ExtendedHoursResponse.from_quote(card.quote),
            market_cap=card.market_cap,
            sector=card.sector,
            industry=card.industry,
            dividend=dividend,
            performance=StockPerformanceResponse.from_performance(card.performance),
            metrics=(
                TickerMetricsResponse.from_card(card)
                if "metrics" in card.include
                else None
            ),
            options_metrics=OptionsMetricsResponse.from_metrics(card.options_metrics),
        )


class PeHistoryPointResponse(BaseModel):
    date: date  # the announcement date the P/E is anchored on
    price: float  # close on/near that date
    ttm_eps: float  # trailing 4 reported quarters' EPS
    pe: float  # price / ttm_eps

    @classmethod
    def from_point(cls, point: PeHistoryPoint) -> "PeHistoryPointResponse":
        return cls(
            date=point.report_date,
            price=round(point.price, 2),
            ttm_eps=round(point.ttm_eps, 2),
            pe=point.pe,
        )


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

    @classmethod
    def from_stats(
        cls, stats: PeHistoryStats | None
    ) -> "PeHistoryStatsResponse | None":
        if stats is None:
            return None
        return cls(
            current_pe=stats.current_pe,
            median_pe=stats.median_pe,
            p25_pe=stats.p25_pe,
            p75_pe=stats.p75_pe,
            min_pe=stats.min_pe,
            max_pe=stats.max_pe,
            current_percentile=stats.current_percentile,
            discount_to_median_percent=stats.discount_to_median_percent,
            signal=stats.signal.value,
            sample_size=stats.sample_size,
        )


class PeHistoryResponse(BaseModel):
    ticker: str
    count: int  # number of points (may be fewer than the reported quarters)
    points: list[PeHistoryPointResponse]  # oldest first
    stats: PeHistoryStatsResponse | None = None  # valuation-vs-history read; null for a thin series

    @classmethod
    def from_history(cls, history: PeHistory) -> "PeHistoryResponse":
        return cls(
            ticker=history.symbol,
            count=len(history.points),
            points=[
                PeHistoryPointResponse.from_point(point) for point in history.points
            ],
            stats=PeHistoryStatsResponse.from_stats(history.stats),
        )


class TickerTypeResponse(BaseModel):
    ticker: str
    asset_type: str  # "etf" if in the ETF universe, else "equity"

    @classmethod
    def from_classification(
        cls, classification: TickerClassification
    ) -> "TickerTypeResponse":
        return cls(
            ticker=classification.ticker, asset_type=classification.asset_type
        )
