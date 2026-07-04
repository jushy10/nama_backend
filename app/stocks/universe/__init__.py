"""The stock-universe sub-slice.

Owns the searchable investable universe: every US-listed company at/above a market-cap
floor, screened out of band and stored so the app can *discover* stocks (search by ticker
or name) rather than only serve a symbol you already know. Thin, and table-less — it writes
the screen straight onto the shared ``stocks`` anchor (the ``sector`` / ``market_cap`` /
``screened_at`` columns migration 0011 added), so search is a single-table read.
"""
