"""The fundamentals slice — a stock's trailing valuation/profitability/health figures,
materialized onto the shared ``stocks`` anchor by an out-of-band Yahoo (``.info``) sweep.

Table-less, like the universe and index-membership slices: there is nothing but the anchor
columns to write, so the slice owns a live source (``ports`` / the yfinance adapter), an
abstract persistence port (``repository``) with its SQL implementation writing onto the anchor
(``db_repository``), and one out-of-band populator use case (``use_cases.SyncFundamentals``).
The reads ride the anchor the ticker card and AI analysis already query — there is no
slice-owned read endpoint.
"""
