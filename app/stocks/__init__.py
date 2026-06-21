"""Stocks feature — a clean-architecture vertical slice over the Alpaca SDK.

Layers (dependencies point inward only):
    entities.py        🟡 Enterprise Business Rules (the Stock entity)
    exceptions.py      🟡 domain errors
    ports.py           🔴 Application port (StockDataProvider)
    use_cases.py       🔴 Application Business Rules (GetStockInfo)
    alpaca_provider.py 🟢 Interface Adapter (implements the port via alpaca-py)
    schemas.py         🔵 HTTP DTO (Pydantic)
    router.py          🟢/🔵 controller + presenter + dependency wiring
"""
