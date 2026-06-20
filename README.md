# nama_backend

A lightweight Python **FastAPI** backend. Runs on **SQLite** locally and for
tests, and on **PostgreSQL (Amazon RDS)** in production — selected entirely by
the `DATABASE_URL` environment variable.

## Layout

```
app/
├── db.py       # Engine/session/Base + get_db; DATABASE_URL-driven
├── models.py   # SQLAlchemy User model
└── main.py     # FastAPI app, schemas, and endpoints
migrations/     # Alembic migrations (schema source of truth for Postgres)
infra/          # Terraform for a secure RDS PostgreSQL instance
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

## PostgreSQL / Amazon RDS

The app picks its backend from `DATABASE_URL` (see [`.env.example`](.env.example)).
Unset → local SQLite. To point at Postgres:

```sh
pip install -e ".[postgres]"   # adds the psycopg driver
export DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/nama?sslmode=verify-full&sslrootcert=/path/to/rds-ca.pem"
```

Provision a secure RDS instance (private subnets, encrypted, no public access,
password in Secrets Manager, TLS enforced) with the Terraform in
[`infra/`](infra/README.md).

### Migrations (Alembic)

SQLite auto-creates tables on startup. **Postgres does not** — its schema is
owned by Alembic migrations in `migrations/`:

```sh
pip install -e ".[dev]"        # adds alembic
alembic upgrade head           # apply migrations to $DATABASE_URL

# After changing a model:
alembic revision --autogenerate -m "describe change"   # review the file, then:
alembic upgrade head
```

## CI

Two path-filtered GitHub Actions workflows (one repo, two pipelines):

- **`backend`** — runs `pytest` when `app/`, `tests/`, or `pyproject.toml` change.
- **`terraform`** — runs `fmt`/`init`/`validate` when `infra/**` changes (no AWS
  credentials needed). A `terraform plan` job is **opt-in**: it stays skipped until
  you set an `AWS_ROLE_ARN` repository variable (an OIDC role — no long-lived keys).
  See comments in [`.github/workflows/terraform.yml`](.github/workflows/terraform.yml).

## Contributing

`main` is protected — push to a feature branch and open a pull request.
