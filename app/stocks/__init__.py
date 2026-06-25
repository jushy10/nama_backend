"""Stocks feature — a clean-architecture vertical slice over the Alpaca SDK.

Layers (dependencies point inward only):
    entities.py                    🟡 Enterprise Business Rules (Stock, Logo,
                                      StockPerformance, StockFundamentals,
                                      Candle, CandleSeries, Timeframe)
    indicators.py                  🟡 Enterprise Business Rules (RSI: pure
                                      technical indicator over close prices)
    exceptions.py                  🟡 domain errors
    ports.py                       🔴 Application ports (StockDataProvider,
                                      StockPerformanceProvider,
                                      StockFundamentalsProvider, LogoProvider,
                                      CandleProvider)
    use_cases.py                   🔴 Application Business Rules (GetStockInfo,
                                      GetStockLogo, GetStockCandles, GetStockRsi)
    alpaca_provider.py             🟢 Interface Adapter (price snapshot,
                                      performance windows + OHLC candles via
                                      alpaca-py)
    finnhub_fundamentals_provider.py 🟢 Interface Adapter (market cap + dividend
                                      via Finnhub)
    logodev_provider.py            🟢 Interface Adapter (logos via Logo.dev —
                                      ticker-keyed, tracks rebrands)
    chart_window.py                🔵 transport helper (chart range -> window)
    schemas.py                     🔵 HTTP DTOs (Pydantic)
    router.py                      🟢/🔵 controller + presenter + DI wiring
"""
