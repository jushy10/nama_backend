import os

from slowapi import Limiter
from starlette.requests import Request


def client_ip(request: Request) -> str:
    """The caller's identity for rate limiting AND the AI generation quota — the
    LB-stamped header when present, else the socket peer."""
    stamped = request.headers.get("x-client-ip")
    if stamped:
        return stamped.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "anonymous"


# The counter defaults to in-process ("memory://"), which is exact for a single
# task. Under autoscaling the service can run several tasks, and an in-process
# counter is then per-task — a single IP can reach up to (task count) * the limit,
# with the API Gateway throttle as the hard global backstop. Set
# RATE_LIMIT_STORAGE_URI to a shared store (e.g. redis://host:6379) to make the
# count exact across tasks; it's a one-env-var flip, no code change.
_rate_limit_storage = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")

# Per-client (per-IP) rate limiting so one abusive caller can't exhaust the
# service — a token bucket per client IP; over it, SlowAPI raises
# RateLimitExceeded and the handler (installed in app/main.py) returns HTTP 429.
# These limits sit under API Gateway's global throttle: that caps total
# load/cost, this stops any single IP from consuming it. The ``default_limits``
# apply to every route; expensive routes layer a tighter ``@limiter.limit(...)``
# on top (their own bucket, checked in addition to these). Tune as traffic grows.
limiter = Limiter(
    key_func=client_ip,
    default_limits=["20/second", "600/minute"],
    storage_uri=_rate_limit_storage,
)
