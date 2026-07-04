"""The index-membership sub-slice.

Owns which stocks belong to the indices the app tracks — the S&P 500 and the Nasdaq-100 —
reconciled out of band and stored on the shared ``stocks`` anchor (the ``in_sp500`` /
``in_nasdaq100`` columns migration 0014 added). Thin, and table-less — it writes the flags
straight onto ``stocks``, the same way the universe slice writes its screen facts. Membership
is slow-moving reference data (it changes only on the quarterly index rebalances), so the
slice is just the sync that keeps the flags current.
"""
