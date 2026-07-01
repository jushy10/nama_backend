"""The quarterly-earnings sub-slice.

A self-contained vertical slice: its own ``entities`` (rather than reusing the shared
``app/stocks/entities.py``), ``ports``, persistence (``repository`` / ``db_repository``
/ ``models``), ``use_cases``, and HTTP ``schemas``. The vendor adapters live in
``app/stocks/adapters`` and the HTTP endpoints (read + cron) in
``app/stocks/endpoints`` — the same layout the annual slice follows.
"""
