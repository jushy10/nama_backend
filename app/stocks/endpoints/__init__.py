"""HTTP endpoints for the stocks feature that aren't part of a single read slice.

Currently home to the earnings read endpoints and their cron entrypoints
(``*_earnings_endpoints`` / ``cron_*_earnings_endpoints``), which drive the out-of-band
refresh use cases. Ops / cron endpoints live here rather than inside a feature slice so
each slice stays focused on its own read path.
"""
