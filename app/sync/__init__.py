"""Batch / CLI entrypoints for the app — run as one-off tasks, not the web server.

Currently just the data-sync sweeps: ``python -m app.sync <slice> [limit]`` (see
``__main__.py``), launched as one-off ECS tasks off the always-on API task so a heavy sweep
can't OOM it.
"""
