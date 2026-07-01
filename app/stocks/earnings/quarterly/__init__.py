"""The quarterly-earnings sub-slice.

A self-contained vertical slice: its own ``entities`` (unlike the estimates slice, which
reuses the shared ``app/stocks/entities.py``), ``ports``, persistence (``repository`` /
``db_repository`` / ``models``), ``use_cases``, HTTP ``schemas`` + ``router``. The vendor
adapters live in ``app/stocks/adapters`` and the cron entrypoint in
``app/stocks/endpoints`` — the same layout the estimates slice follows.
"""
