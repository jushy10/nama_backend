"""A lightweight FastAPI backend backed by SQLite."""

import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db
from app.models import User
from app.stocks.router import router as stocks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup (fine for SQLite; swap for migrations later).
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="nama_backend", lifespan=lifespan)

# CORS: let the SPA (served on a different origin, e.g. namainsights.com) call
# this API from the browser. Override with the CORS_ALLOW_ORIGINS env var
# (comma-separated); the default covers the public site and the local Vite
# dev/preview servers.
_default_cors_origins = (
    "https://namainsights.com,"
    "https://www.namainsights.com,"
    "http://localhost:5173,"
    "http://localhost:4173"
)
cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", _default_cors_origins).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stocks_router)


class UserIn(BaseModel):
    email: str
    name: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    created_at: datetime


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserIn, db: Session = Depends(get_db)) -> User:
    email = payload.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(400, "email already registered")
    user = User(email=email, name=payload.name.strip())
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return list(db.scalars(select(User).order_by(User.id)))


@app.get("/users/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    return user
