"""The stock-universe sub-slice.

Owns the investable universe: every US-listed company at/above a market-cap floor, screened
out of band and stored on the shared ``stocks`` anchor so the app *knows* the big-cap
universe rather than only whatever symbols it's been asked about. Thin, and table-less — it
writes the screen straight onto ``stocks`` (the ``sector`` / ``market_cap`` / ``screened_at``
columns migration 0012 added). The read/search endpoint over it is **deferred**; for now the
slice is just the sync that populates the anchor.
"""
