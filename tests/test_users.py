"""API tests running against an in-memory SQLite database."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app


@pytest.fixture
def client():
    # One in-memory SQLite DB shared across connections for this test.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_and_get_user(client):
    r = client.post("/users", json={"email": "  Alice@Example.com ", "name": "Alice"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"] == 1
    assert data["email"] == "alice@example.com"  # trimmed + lowercased

    r2 = client.get(f"/users/{data['id']}")
    assert r2.status_code == 200
    assert r2.json()["name"] == "Alice"


def test_list_users(client):
    client.post("/users", json={"email": "a@b.com", "name": "A"})
    client.post("/users", json={"email": "c@d.com", "name": "C"})
    r = client.get("/users")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_duplicate_email(client):
    client.post("/users", json={"email": "dup@example.com", "name": "A"})
    r = client.post("/users", json={"email": "dup@example.com", "name": "B"})
    assert r.status_code == 400


def test_user_not_found(client):
    assert client.get("/users/999").status_code == 404
