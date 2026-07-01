"""The analyst-estimates vertical slice.

Everything specific to a stock's forward analyst consensus lives here:

- ``ports`` — the live-source port (``AnalystEstimatesProvider``).
- ``repository`` — the abstract persistence port (``AnalystEstimatesRepository``) the
  use case depends on, plus its value types.
- ``db_repository`` — the concrete SQLAlchemy implementation of that port.
- ``models`` — the ORM models for the tables + simple query functions the repository
  calls.
- ``use_cases`` — the out-of-band refresh (``SyncAnalystEstimates``).
- ``router`` — the provider wiring the stock snapshot reads through.

The vendor adapters that implement the provider port live in ``app/stocks/adapters``;
the HTTP cron entrypoint that drives the refresh lives in ``app/stocks/endpoints``.
"""
