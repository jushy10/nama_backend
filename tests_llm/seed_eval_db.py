"""Seed a migrated eval DB with a handful of mega-cap rows so the screener questions
have data. Run with DATABASE_URL pointing at the eval DB, after `alembic upgrade head`:

    DATABASE_URL=sqlite:///./eval.db python tests_llm/seed_eval_db.py
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import engine
from app.domains.listings.anchor.models import StockRecord

NOW = datetime.now(timezone.utc)

# ticker, name, exchange, sector, industry, mktcap, rev_g, eps_g, pe
ROWS = [
    ("NVDA", "NVIDIA Corporation", "NASDAQ", "Technology", "Semiconductors", 3.4e12, 114.2, 147.1, 45.3),
    ("AAPL", "Apple Inc.", "NASDAQ", "Technology", "Consumer Electronics", 3.2e12, 2.0, 10.9, 33.1),
    ("MSFT", "Microsoft Corporation", "NASDAQ", "Technology", "Software - Infrastructure", 3.1e12, 15.7, 12.1, 36.4),
    ("GOOGL", "Alphabet Inc.", "NASDAQ", "Communication Services", "Internet Content & Information", 2.1e12, 13.9, 38.6, 22.8),
    ("AMZN", "Amazon.com, Inc.", "NASDAQ", "Consumer Cyclical", "Internet Retail", 2.0e12, 11.0, 90.5, 41.2),
    ("META", "Meta Platforms, Inc.", "NASDAQ", "Communication Services", "Internet Content & Information", 1.4e12, 21.9, 60.5, 27.5),
    ("AVGO", "Broadcom Inc.", "NASDAQ", "Technology", "Semiconductors", 8.0e11, 44.0, 24.0, 38.9),
    ("AMD", "Advanced Micro Devices, Inc.", "NASDAQ", "Technology", "Semiconductors", 2.6e11, 13.7, 25.6, 48.7),
    ("TSLA", "Tesla, Inc.", "NASDAQ", "Consumer Cyclical", "Auto Manufacturers", 1.0e12, 0.9, -23.1, 88.0),
]

with Session(engine) as db:
    for ticker, name, exchange, sector, industry, cap, rev_g, eps_g, pe in ROWS:
        db.add(
            StockRecord(
                ticker=ticker, name=name, exchange=exchange, sector=sector,
                industry=industry, market_cap=cap, revenue_growth_yoy=rev_g,
                eps_growth_yoy=eps_g, pe_ratio=pe, in_sp500=True, in_nasdaq100=True,
                country="US", currency="USD", domicile_country="US", screened_at=NOW,
            )
        )
    db.commit()
    print(f"seeded {db.query(StockRecord).count()} stocks")
