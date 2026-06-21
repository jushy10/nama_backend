"""A lightweight FastAPI backend backed by SQLite."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import Base, engine
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
    # Create tables on startup (fine for SQLite; swap for migrations later).
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="nama_backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],  # lets the OPTIONS preflight succeed instead of 405
    allow_headers=["*"],
)
app.include_router(stocks_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
