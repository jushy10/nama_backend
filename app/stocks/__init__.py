"""Stocks feature — a clean-architecture vertical slice over the Alpaca SDK.

Layers (dependencies point inward only):
    entities.py                    🟡 Enterprise Business Rules (Stock, Logo,
                                      StockPerformance, StockFundamentals)
    exceptions.py                  🟡 domain errors
    ports.py                       🔴 Application ports (StockDataProvider,
                                      StockPerformanceProvider,
                                      StockFundamentalsProvider, LogoProvider)
    use_cases.py                   🔴 Application Business Rules (GetStockInfo,
                                      GetStockLogo)
    alpaca_provider.py             🟢 Interface Adapter (price snapshot +
                                      performance windows via alpaca-py)
    finnhub_fundamentals_provider.py 🟢 Interface Adapter (market cap + dividend
                                      via Finnhub)
    fmp_logo_provider.py           🟢 Interface Adapter (logos via Financial
                                      Modeling Prep)
    schemas.py                     🔵 HTTP DTO (Pydantic)
    router.py                      🟢/🔵 controller + presenter + DI wiring
"""
