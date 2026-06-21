"""Stocks feature — a clean-architecture vertical slice over the Alpaca SDK.

Layers (dependencies point inward only):
    entities.py          🟡 Enterprise Business Rules (Stock, Logo entities)
    exceptions.py        🟡 domain errors
    ports.py             🔴 Application ports (StockDataProvider, LogoProvider)
    use_cases.py         🔴 Application Business Rules (GetStockInfo, GetStockLogo)
    alpaca_provider.py   🟢 Interface Adapter (stock data via alpaca-py)
    fmp_logo_provider.py 🟢 Interface Adapter (logos via Financial Modeling Prep)
    schemas.py           🔵 HTTP DTO (Pydantic)
    router.py            🟢/🔵 controller + presenter + dependency wiring
"""
