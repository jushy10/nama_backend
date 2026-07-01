"""A lightweight FastAPI backend backed by SQLite."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.stocks.estimates.cron_estimates_endpoints import (
    router as estimates_cron_router,
)
from app.stocks.router import router as stocks_router

# Browser origins allowed to call this API (cross-origin). Comma-separated env
# var so prod and local dev differ without a code change; defaults to the
# namainsights site. Without this, a browser on namainsights.com is blocked.
_DEFAULT_ORIGINS = "https://namainsights.com,https://www.namainsights.com"
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database schema is owned by Alembic migrations (`alembic upgrade head`),
    # not created here — deploys manage the database explicitly, so there's
    # nothing to do on startup for now.
    yield


app = FastAPI(title="nama_backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],  # lets the OPTIONS preflight succeed instead of 405
    allow_headers=["*"],
)
app.include_router(stocks_router)
# The analyst-estimates refresh cron endpoint (POST /internal/estimates/sync); it
# drives the SyncAnalystEstimates use case out of band. See
# app/stocks/estimates/cron_estimates_endpoints.py.
app.include_router(estimates_cron_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
