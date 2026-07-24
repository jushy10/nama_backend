"""The ticker slice's composition root — framework-free. The per-symbol price
provider (quote / performance / exchange snapshot / daily closes) is the
market-routing router owned by app/endpoints/wiring.py (get_price_provider,
riding the Alpaca missing-keys 503 gate), and the quarterly-earnings / forward-
estimates providers belong to their own slices — so the builders take those as
parameters. The DB-backed pieces (the anchor facts repository, the ETF-universe
membership lookup) are request-scoped reads this slice does own, so the builders
construct them from the Session; the deep reported-EPS source is the keyless
yfinance singleton, constructed here."""

from functools import lru_cache

from sqlalchemy.orm import Session

from app.adapters.yfinance.eps_history_adapter_impl import EpsHistoryAdapterImpl
from app.domains.etfs.repository_adapter_impl import EtfLookupRepositoryAdapterImpl
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.domains.pricing.charts.interfaces import CandleAdapter
from app.domains.pricing.ticker.db_repository import DbTickerRepository
from app.domains.pricing.ticker.interfaces import EpsHistoryAdapter, OptionChainAdapter
from app.domains.pricing.ticker.use_cases import (
    ClassifyTicker,
    GetStockPeHistory,
    GetTickerCard,
)
from app.domains.shared.interfaces import (
    AnalystEstimatesAdapter,
    StockPerformanceAdapter,
    StockQuoteAdapter,
)


@lru_cache(maxsize=1)
def get_eps_history_provider() -> EpsHistoryAdapter:
    # Keyless yfinance singleton (like the options provider): it shares the module-level
    # pacing state and is best-effort at read, so it's always constructable — no key gate.
    return EpsHistoryAdapterImpl()


def build_get_ticker_card(
    db: Session,
    provider: StockQuoteAdapter,
    options: OptionChainAdapter,
    earnings: QuarterlyEarningsAdapter,
    estimates: AnalystEstimatesAdapter,
) -> GetTickerCard:
    # The market-routing provider backs the quote, the trailing performance windows, and the
    # one-time exchange lookup — a US symbol goes to the Alpaca singleton (real-time), a
    # Canadian-suffixed one (.TO/.V/…) to the keyless Yahoo feed (delayed, best-effort). The
    # repository serves the anchor read — the stored name, exchange, screen facts, and the
    # annual/fundamentals slices' materialized figures (growth, cash, margins, ratios,
    # dividend) — off the stocks row, so the card needs no live fundamentals/profile vendor.
    # The options chain is the keyless yfinance singleton — always wired, best-effort at read —
    # the quarterly-earnings provider is the same DB cache the earnings endpoint reads (backing
    # the trailing P/E's TTM sum), and the estimates provider is the DB-only annual-forward
    # projection (backing forward P/E and P/S — the only fundamentals not on the anchor), read
    # best-effort only when 'metrics' is requested.
    performance = provider if isinstance(provider, StockPerformanceAdapter) else None
    return GetTickerCard(
        provider,
        performance=performance,
        stocks=provider,
        repository=DbTickerRepository(db),
        options=options,
        earnings=earnings,
        estimates=estimates,
        # The card's asset_type is a single indexed membership check against the etfs
        # table (same request-scoped session as the anchor read) — "etf" for a screened
        # fund, else "equity".
        etfs=EtfLookupRepositoryAdapterImpl(db),
    )


def build_get_stock_pe_history(candles: CandleAdapter) -> GetStockPeHistory:
    # The market-routing provider supplies the daily closes (it implements CandleAdapter — the
    # same instance the candle chart uses, US→Alpaca / CA→Yahoo), and the deep reported-EPS
    # history rides the keyless yfinance adapter. The card's Alpaca 503 gate is inherited for a
    # US symbol (the closes are primary here); the EPS leg is best-effort, so no key to gate on.
    return GetStockPeHistory(candles, get_eps_history_provider())


def build_classify_ticker(db: Session) -> ClassifyTicker:
    # Pure DB read: a single indexed membership check against the etfs table — no
    # vendor, no key, request-scoped session — so it's always constructable.
    return ClassifyTicker(EtfLookupRepositoryAdapterImpl(db))
