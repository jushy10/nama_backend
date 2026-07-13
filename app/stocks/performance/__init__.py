"""The stock trailing-performance slice.

Materializes each screened stock's trailing price-return over the standard windows
(1W / 1M / 3M / 6M / YTD / 1Y) onto the shared ``stocks`` anchor, refreshed out of band by
the ``sync-stock-performance`` cron. Table-less, like the fundamentals / universe /
index-membership slices — the windows are denormalized columns on ``stocks``.

Why it exists: the heat map used to recompute these windows live on every request — a year of
daily bars for a whole index (~500 names), its heaviest read by far. This slice moves that
work to a cron and leaves the read a single anchor query, the same "get it from the DB, not
the live vendor" division of labour the rest of the app already uses.
"""
