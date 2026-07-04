"""Application logging setup.

Under the production entrypoint — bare ``uvicorn app.main:app`` (see the Dockerfile) — uvicorn
configures only its own ``uvicorn*`` loggers and leaves the root logger at its default WARNING.
Our ``app.*`` loggers inherit that level, so every ``logger.info(...)`` — the sync-sweep
progress heartbeats and the end-of-run summaries — is filtered out *before* it is emitted and
never reaches the container logs (→ CloudWatch on Fargate). Only WARNING/ERROR slip through, via
logging's last-resort handler. That is why, until now, the carefully-written "sync done" summary
lines were effectively invisible in production.

``configure_logging`` fixes that by attaching a single stdout handler to the ``app`` logger and
setting its level (``LOG_LEVEL`` env, default ``INFO``). It touches only the ``app`` tree — not
the root, not uvicorn's loggers — so third-party chatter (yfinance / httpx / SQLAlchemy) stays at
its default and uvicorn's access log is not duplicated. It is idempotent, so importing the app
more than once (e.g. in tests) does not stack handlers.
"""

from __future__ import annotations

import logging
import os
import sys

# Marks the handler this module owns, so repeat calls can find it and not add a second one.
_HANDLER_NAME = "nama-app-stdout"


def configure_logging() -> None:
    """Send ``app.*`` logs to stdout at ``LOG_LEVEL`` (default ``INFO``). Idempotent."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if any(getattr(h, "name", None) == _HANDLER_NAME for h in app_logger.handlers):
        return  # already configured (repeat import) — don't add a second handler
    handler = logging.StreamHandler(sys.stdout)
    handler.name = _HANDLER_NAME
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    app_logger.addHandler(handler)
