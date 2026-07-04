"""The stock-universe sub-slice.

Owns the searchable investable universe: every US-listed company at/above a market-cap
floor, screened out of band and stored so the app can *discover* stocks (search by ticker
or name) rather than only serve a symbol you already know. Thin, like the ticker slice —
it writes into the shared ``stocks`` anchor plus its own ``stock_universe`` child table.
"""
