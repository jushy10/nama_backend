"""HTTP endpoints for the stocks feature that aren't part of a single read slice.

Currently home to the analyst-estimates cron entrypoint (``cron_estimates_endpoints``),
which drives the out-of-band refresh use case. Ops / cron endpoints live here rather
than inside a feature slice so each slice stays focused on its own read path.
"""
