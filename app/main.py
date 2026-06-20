"""A lightweight FastAPI backend backed by SQLite."""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base, engine, get_db, should_auto_create_tables
from app.models import User


@asynccontextmanager
async def lifespan(app: FastAPI):
    # SQLite (local dev + tests) auto-creates tables. On Postgres/RDS the schema
    # is owned by Alembic migrations — run `alembic upgrade head` instead.
    if should_auto_create_tables():
        Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="nama_backend", lifespan=lifespan)


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
