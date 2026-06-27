# nama_backend — architecture & conventions

A lightweight FastAPI backend built as a clean-architecture vertical slice. Each
feature is a package under `app/` split into layers that depend **inward only**.
`app/stocks/` is the reference implementation — mirror it. If this doc and the
code disagree, the code wins; fix this file.

## The rule: dependencies point inward

Flow: `endpoint → use case → entities`, and the use case fetches data by calling
an **adapter through a port** (an ABC). The core never imports a vendor — the
vendor imports the core. That inversion is what lets every test run offline.

| Layer | File(s) | Holds | May import |
|-------|---------|-------|-----------|
| Domain core | `entities.py`, `indicators.py` | frozen dataclasses + intrinsic rules (`change`, `beat_rate`, `is_bullish`); pure calcs (RSI) | stdlib only |
| Errors | `exceptions.py` | domain errors (`StockNotFound`, `StockDataUnavailable`) | stdlib only |
| Ports | `ports.py` | abstract interfaces (ABCs) the use cases depend on | entities |
| Use cases | `use_cases.py` | one class per action: validate → call ports → assemble entities | entities, ports, exceptions, indicators |
| Adapters | `*_provider.py`, `constituents.py` | vendor/DB implementations of ports; map vendor models→entities, vendor errors→domain errors | entities, ports, exceptions, **+ vendor SDK / httpx / SQLAlchemy** |
| DTOs | `schemas.py` | pydantic HTTP response models | pydantic only |
| Router (composition root) | `router.py` | endpoints, entity→DTO presenters, env-keyed DI wiring | everything |

Never shortcut the rule: no vendor import outside its adapter; no adapter or
framework import inside an entity, use case, or port.

## Patterns

**Primary data vs. best-effort enrichment.** Primary data (price, earnings
history): the provider is required, errors **propagate**, a missing key is a hard
**503**. Enrichment (market cap, description, next report): the provider is typed
`| None`, the use case wraps it in `try/except (StockNotFound,
StockDataUnavailable): return None`, and a missing key just omits the field.
Enrichment must never sink the primary response.

**Exception → HTTP** (done uniformly in each endpoint):

| Raised | HTTP |
|--------|------|
| `ValueError` (bad input) | 400 |
| `StockNotFound` | 404 |
| `StockDataUnavailable` | 502 |
| missing required API key (in wiring) | 503 |

**Config & secrets** come from env vars, read only in the router's `get_*`
factories (`@lru_cache` singletons, injected via `Depends`). Build providers
lazily so the app boots without every key. Never commit secrets.

**Input** is normalized once, at the top of the use case (`_normalize_symbol`).

## Adding a feature — work inward to outward

1. **Entity** — model the data + its intrinsic rules in `entities.py`.
2. **Port** — declare the interface the use case needs in `ports.py`.
3. **Use case** — write `execute()` in `use_cases.py` (entities + ports only).
4. **Adapter** — implement the port against the real vendor/DB in a `*_provider.py`.
5. **DTO + presenter** — response model in `schemas.py`, `_present_*` in `router.py`.
6. **Endpoint + wiring** — route + `Depends`/`@lru_cache` factory in `router.py`.
7. **Test** — inject a fake implementing the port; assert via `TestClient`.

## Testing

Everything runs **offline** — inject a hand-written fake of the port instead of
mocking the network. Tests use in-memory SQLite and ignore `DATABASE_URL`. If a
test needs the network, the seam is in the wrong place.

## Commands

```sh
pip install -e ".[dev]"          # install (".[postgres]" for the RDS driver)
uvicorn app.main:app --reload    # run locally (docs at /docs)
alembic upgrade head             # apply migrations (schema is Alembic-owned)
pytest                           # offline test suite
```

## Layout

```
app/
├── main.py            # FastAPI app: CORS, lifespan, /healthz
├── db.py              # engine/session/Base/get_db (DATABASE_URL-driven)
└── stocks/            # the vertical slice
    ├── entities.py  indicators.py  exceptions.py   # domain core
    ├── ports.py                                    # interfaces
    ├── use_cases.py                                # orchestration
    ├── *_provider.py  constituents.py              # adapters
    ├── schemas.py  chart_window.py                 # edge helpers
    └── router.py                                   # endpoints + wiring
tests/                 # offline; fakes injected through ports
alembic/               # migrations    scripts/     # ops-time sync (FMP→DB)
```

## Hard rules

- **Never violate the dependency rule** (no vendor import outside its adapter).
- **`main` is protected** — branch and open a PR.
- **Never commit secrets** — env vars only (SSM in AWS).
- **Schema is Alembic-owned** — never `create_all`; migrate.
