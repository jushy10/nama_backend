"""The analyst-estimates vertical slice.

Everything specific to a stock's forward analyst consensus lives here: the ports the
use cases depend on (``estimates_ports``), the database repository + cache table
(``stock_estimates_repository``), the out-of-band refresh use case (``use_cases``),
the provider wiring the stock snapshot reads through (``router``), and the HTTP cron
endpoint that drives the refresh (``cron_estimates_endpoints``). The vendor adapters
that implement the ports live one level up in ``app/stocks/adapters``.
"""
