# nama_backend

A very lightweight Python **FastAPI** backend backed by **SQLite**.

## Layout

```
app/
├── db.py       # SQLite engine, session, Base, get_db dependency
├── models.py   # SQLAlchemy User model
└── main.py     # FastAPI app, schemas, and endpoints
tests/
└── test_users.py   # API tests against in-memory SQLite
```

## Setup

```sh
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# macOS/Linux:         source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```sh
uvicorn app.main:app --reload
```

Creates a local `nama.db` on first run. Interactive docs at
<http://localhost:8080/docs>.

## Endpoints

| Method | Path          | Description      |
| ------ | ------------- | ---------------- |
| GET    | `/healthz`    | Liveness check   |
| POST   | `/users`      | Create a user    |
| GET    | `/users`      | List users       |
| GET    | `/users/{id}` | Get a user by ID |

```sh
curl -X POST localhost:8080/users \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","name":"Alice"}'
```

## Test

```sh
pytest
```

Tests run against an in-memory SQLite database — no setup, no files.

## Contributing

`main` is protected — push to a feature branch and open a pull request.
