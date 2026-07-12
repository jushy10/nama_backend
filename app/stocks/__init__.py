"""Stocks feature — a clean-architecture vertical slice.

Layers depend inward only: entities ← ports ← use cases, with the vendor
adapters (Alpaca, Yahoo via yfinance, SEC EDGAR, Logo.dev, the DB) implementing
the ports and the router acting as composition root. See CLAUDE.md at the repo
root for the full layer map and conventions — kept there, and not duplicated
here, so it can't drift out of date in two places.
"""
