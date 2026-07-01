# nama_backend — architecture & conventions

A lightweight **FastAPI** backend. Code is organized as a **clean-architecture
vertical slice** (Robert C. Martin's "Clean Architecture"): each feature lives
in its own package under `app/`, split into layers that depend *inward* only.

The stocks feature (`app/stocks/`) is the reference implementation. Every new
feature should mirror its layering. If something here disagrees with the code,
the code wins — fix this file.

---

## The one rule: dependencies point inward

```
            HTTP request
                 │
                 ▼
   ┌──────────────────────────────┐
   │  endpoint  (router.py)       │  controller + presenter + DI wiring
   └──────────────┬───────────────┘
                  │ calls
                  ▼
   ┌──────────────────────────────┐
   │  use case  (use_cases.py)    │  orchestration; one class per action
   └─────┬───────────────────┬────┘
         │ builds/returns     │ asks for data through a
         ▼                    ▼
   ┌─────────────┐     ┌──────────────────┐
   │  entities   │     │  port (ABC)      │  the interface the use case needs
   │ entities.py │     │   ports.py       │
   └─────────────┘     └────────▲─────────┘
                                │ implemented by
                       ┌────────┴─────────┐
                       │  adapter         │  the ONLY code that knows a vendor
                       │ *_provider.py    │  (Alpaca / Finnhub / FMP / Logo.dev / SEC EDGAR / DB)
                       └──────────────────┘
```

**The flow:** `endpoint → use case → entities`, and the use case pulls data by
calling an **adapter through a port**.

**The dependency rule** — an inner layer must never import an outer one:

| Layer | File(s) | May import | Must NOT import |
|-------|---------|-----------|-----------------|
| Entities | `entities.py`, `indicators.py` | stdlib only (`dataclasses`, `datetime`, `enum`) | anything else in `app/`, FastAPI, pydantic, any vendor SDK |
| Ports | `ports.py` | entities, stdlib `abc` | use cases, adapters, framework, vendors |
| Use cases | `use_cases.py` | entities, ports, exceptions, pure-domain helpers (`indicators.py`) | adapters (concrete providers), FastAPI, pydantic, any vendor SDK |
| Adapters | `*_provider.py`, `constituents.py` | entities, ports, exceptions, **+ the vendor SDK / `httpx` / SQLAlchemy** | other adapters, use cases, FastAPI, pydantic |
| DTOs | `schemas.py` | pydantic only | entities, use cases, adapters |
| Router (composition root) | `router.py` | **everything** — use cases, ports, concrete adapters, schemas, exceptions, `db`, FastAPI | — |

> The use case depends on the **port** (an `ABC`), never the concrete adapter.
> That inversion is the whole point: the core never imports a vendor — the
> vendor imports the core. It's also what lets every test run offline against a
> hand-written fake. Never shortcut it by importing a `*_provider` into a use
> case or an entity.

---

## The layers

### 1. Entities — `app/stocks/entities.py`
*Enterprise Business Rules.* Pure domain objects: frozen `@dataclass`es and
`Enum`s that model the concepts (`Stock`, `Quote`, `Candle`, `EarningsHistory`,
`Constituent`, …). They import nothing from the rest of the app.

Business logic that is **a fact about one entity** lives here, as a `@property`
or `@classmethod` — computed on access, not stored:
- `Stock.change` / `change_percent` / `spread`
- `Candle.is_bullish` (the green/red rule)
- `KeyMetrics.peg`, `EarningsHistory.beat_rate`, `EarningsSurprise.beat`
- `EarningsMetrics.from_key_metrics(...)` (a classmethod that *builds* one entity from another)

Entities are vendor-agnostic on purpose: e.g. `Timeframe` defines business-level
granularities; the adapter maps them onto whatever the vendor calls them.

Pure cross-entity calculations with no I/O (e.g. RSI math in `indicators.py`)
are also domain code — they live next to the entities, import only entities, and
never reach out for data.

### 2. Ports — `app/stocks/ports.py`
The abstractions a use case depends on. Each is an `ABC` with `@abstractmethod`s
phrased in domain terms (`get_stock`, `get_quotes`, `get_earnings_history`,
`all`). They return **entities** and document which **domain exceptions** they
raise. One port per capability — keep them small so an adapter can implement
exactly the ones it covers (`AlpacaStockDataProvider` implements six).

Naming: a live feed is a `*Provider`; static reference data is a `*Repository`.

### 3. Use cases — `app/stocks/use_cases.py`
*Application Business Rules.* One class per action, constructor-injected with the
ports it needs, exposing a single `execute(...)`:

```python
class GetStockInfo:
    def __init__(self, provider: StockDataProvider, ...): ...
    def execute(self, symbol: str) -> Stock: ...
```

A use case: validates/normalizes input (`_normalize_symbol`), calls ports,
assembles entities, applies enrichment, and enforces multi-source orchestration
(ranking in `ScreenStocks`, revenue overlay in `GetStockEarnings`). It depends
only on entities + ports — never a framework, never a concrete provider.

### 4. Adapters — `app/stocks/*_provider.py`, `app/stocks/adapters/*_adapter.py`, `app/stocks/constituents.py`
*Interface Adapters.* Each implements a port and is **the only module that knows
a given vendor exists**. It translates the vendor's SDK/HTTP/ORM models into our
entities, and the vendor's failures into our domain exceptions. Swap vendors and
only this one file changes.

> Most adapters still sit flat in `app/stocks/` as `<vendor>_<concern>_provider.py`.
> The analyst-estimates adapters have moved into `app/stocks/adapters/` and are named
> `*_adapter.py` (`yfinance_estimates_adapter.py`, `db_cached_estimates_adapter.py`,
> `caching_estimates_adapter.py`); other features' adapters can migrate there over time.

- `alpaca_provider.py` — Alpaca SDK → price/quote/candles/performance/sectors
- `finnhub_*_provider.py` — Finnhub → fundamentals / earnings / calendar / company name (`/stock/profile2`) / analyst recommendation trends (`/stock/recommendation`)
- `sec_edgar_revenue_provider.py` — SEC EDGAR XBRL **flat API** (`httpx`, free/keyless) → reported quarterly revenue *totals*; resolves ticker→CIK and derives Q4 from the 10-K
- `sec_edgar_segment_revenue_provider.py` — SEC EDGAR **inline XBRL** (the raw 10-Q/10-K documents; the flat API drops the dimensional contexts) → per-quarter revenue split by operating segment and product/service; parses several recent filings and keeps standalone-quarter, member-qualified facts
- `logodev_provider.py` — Logo.dev → logo image
- `caching_company_profile_provider.py` / `caching_revenue_provider.py` / `caching_segment_revenue_provider.py` — decorator adapters (wrap another adapter to add an in-process TTL cache; same port in, same port out)
- `adapters/db_cached_estimates_adapter.py` — decorator on the `AnalystEstimatesProvider` port backed by a **persistent DB cache** (the `AnalystEstimatesRepository`) instead of an in-process map: shared across instances, survives restarts, serves a stale row if the live source is down. Fills lazily on a miss; refreshed out of band by the estimates cron endpoint (`app/stocks/endpoints/cron_estimates_endpoints.py`). This is what the stock endpoint wires for estimates (the in-memory `adapters/caching_estimates_adapter.py` is now unused). `adapters/yfinance_estimates_adapter.py` is the live source it wraps — **Yahoo Finance via `yfinance`** (no API key; free), which replaced FMP because FMP's free tier gated forward estimates to a small symbol allowlist (a 402 for the likes of MU/SNDK). Yahoo is unofficial/best-effort and rate-limits data-centre IPs, so the DB cache in front is what keeps it usable
- `adapters/yfinance_quarterly_earnings_adapter.py` — live source for the quarterly-earnings slice: **Yahoo via `yfinance`**, building the 4-recent + up-to-2-upcoming quarter timeline. **Past** quarters come from `earnings_dates` (reported EPS vs the estimate that preceded it; surprise computed here, not from Yahoo's `Surprise(%)`). **Upcoming** quarters come from the `0q`/`+1q` rows of `earnings_estimate` + `revenue_estimate` — the reliable source of *two* forward quarters (EPS + revenue), so a stock surfaces both even when `earnings_dates` lists only one scheduled future date; a scheduled date is attached when it lines up. **Reported revenue** (`revenue_actual`) is matched onto the past quarters from `quarterly_income_stmt` (Total Revenue, by calendar year+quarter) — best-effort enrichment, so a failure fetching it drops the actual without sinking the timeline. Fiscal labels are derived from the announcement date (calendar best-effort). `adapters/db_cached_quarterly_earnings_adapter.py` — a **read-through** DB cache in front of it: serves stored rows if present, else fetches from Yahoo **once on a miss** and stores. Deliberately simpler than `db_cached_estimates_adapter.py` — **no TTL/staleness or serve-stale**; a populated symbol is always served straight from the DB, and keeping rows current is entirely the cron's job
- `constituents.py` — owns the SQLAlchemy `ConstituentRecord` model **and** `SqlConstituentRepository`; the DB schema lives here, the entity stays ORM-free
- `stocks/models.py` — the shared `stocks` anchor as its own tiny slice (`app/stocks/stocks/`): owns the `StockRecord` model (the `stocks` table) + `get_or_create_stock`. Owned by no single feature; per-feature tables hang off it and import it from here
- The estimates **persistence** is split into three layers in the sub-slice: `estimates/models.py` (the ORM model for `stock_analyst_estimates` + simple query functions; it imports the shared `StockRecord` from `stocks/models.py`), `estimates/db_repository.py` (the concrete `SqlAnalystEstimatesRepository` — maps rows⇄entity and calls the model queries), and `estimates/repository.py` (the abstract `AnalystEstimatesRepository` port the use case is injected with). Same DB-owns-the-schema idea as `constituents.py`, split across the port / concrete / model boundary

Naming: `<vendor>_<concern>_provider.py` for the flat adapters; `<vendor>_<concern>_adapter.py` for those under `app/stocks/adapters/`.

> **The analyst-estimates sub-slice — `app/stocks/estimates/`.** Estimates are broken
> out as a self-contained vertical slice rather than living flat in `app/stocks/`:
> - `ports.py` — the live-source port (`AnalystEstimatesProvider`).
> - `repository.py` — the **abstract** persistence port (`AnalystEstimatesRepository`), injected into the use case.
> - `db_repository.py` — its **concrete** SQLAlchemy implementation.
> - `models.py` — the ORM model for `stock_analyst_estimates` + simple query functions (the shared `stocks` anchor is imported from `app/stocks/stocks/models.py`).
> - `use_cases.py` → `SyncAnalystEstimates` — the out-of-band refresh.
> - `router.py` → `get_estimates_provider` — the provider wiring the snapshot reads through.
>
> The vendor adapters it uses sit in `app/stocks/adapters/`; the HTTP cron entrypoint
> (`app/stocks/endpoints/cron_estimates_endpoints.py`, `POST /internal/estimates/sync`)
> lives in `app/stocks/endpoints/`. The refresh is invoked over HTTP by the
> `sync-estimates` workflow — there is no `scripts/sync_estimates.py` any more. The live
> source is **yfinance (Yahoo)**, which needs no API key — so, unlike the old FMP path,
> the serving task carries no estimates credential and the cron endpoint has no key gate.
> It is still **unauthenticated** and must not be publicly reachable (it writes the DB
> and hits Yahoo).

> **The quarterly-earnings sub-slice — `app/stocks/earnings/quarterly/`.** A fully
> self-contained slice with its **own `entities.py`** (unlike estimates, which reuses the
> shared `app/stocks/entities.py`): `QuarterlyEarnings` + `QuarterlyEarningsTimeline`, plus
> `ports` / `repository` / `db_repository` / `models` / `use_cases` / `schemas` (both HTTP
> endpoints live in `app/stocks/endpoints/`: the read `quarterly_earnings_endpoints.py` and
> the `cron_quarterly_earnings_endpoints.py`, so the slice itself has no `router.py`).
> It serves a stock's 4 most-recent reported quarters (reported EPS + a surprise *computed*
> from actual vs. estimate) and up to **2** upcoming quarters — the `0q`/`+1q` forward EPS +
> revenue estimates, which is as far out as Yahoo publishes structured forward data (so 2 is
> the ceiling, and it's often 1 when only one is estimated) — at
> `GET /stocks/{symbol}/earnings/quarterly`. Live source is **yfinance (Yahoo)** via
> `earnings_dates` (past) + `earnings_estimate`/`revenue_estimate` `0q`/`+1q` (upcoming)
> (`adapters/yfinance_quarterly_earnings_adapter.py`),
> behind a persistent DB cache + out-of-band cron
> (`POST /internal/earnings/quarterly/sync`, driven by the `sync-quarterly-earnings`
> workflow). Three deliberate divergences from estimates: (1) the table
> (`stock_quarterly_earnings`) is a **time series** (many rows per stock, unique on
> `stock_id` + fiscal year + quarter), not one wide row; (2) the read cache is a plain
> **read-through** (DB-first, fetch-on-miss only — **no TTL/staleness or serve-stale**), so
> a populated symbol is always served from the DB and freshness is entirely the cron's job;
> (3) the **sync** skips an **empty** live result rather than persisting it (its upsert
> rewrites a stock's whole window via delete-then-insert, so an empty write would wipe good
> history). Fiscal labels are a calendar best-effort — `earnings_dates` carries only the
> announcement date, so the period end is the most recent calendar quarter-end before it
> (exact for calendar fiscal years, a label offset for others).

### 5. DTOs — `app/stocks/schemas.py`
Pydantic `BaseModel`s for HTTP responses. Pydantic is a serialization detail, so
DTOs live at the edge, deliberately **separate from entities** — that's what
keeps the core framework-agnostic. JSON-shape concerns (field aliases like
`1w`/`3m`) belong here, not on the entity.

### 6. Router — `app/stocks/router.py`
The **composition root**. Three jobs:
- **Controller** — each `@router.get` endpoint unpacks the request, calls
  `use_case.execute(...)`, and maps domain exceptions → HTTP status.
- **Presenter** — `_present_*` functions turn the returned entity into a DTO.
- **Wiring** — `get_*` factory functions read env vars and build providers
  (`@lru_cache` for singletons), injected via FastAPI `Depends`.

### 7. Exceptions — `app/stocks/exceptions.py`
Domain errors in business terms, independent of HTTP and vendors:
`StockNotFound`, `StockDataUnavailable`. Adapters raise them; the router
translates them.

---

## Core patterns (follow these)

**Primary data vs. best-effort enrichment.** Decide which a new data source is:
- *Primary* (the endpoint's reason to exist, e.g. price, earnings history): the
  provider is required, errors **propagate** to the endpoint, and a missing API
  key is a hard **503** in the wiring.
- *Enrichment* (nice-to-have, e.g. market cap, company name, next-report): the
  provider is typed `| None`, the use case wraps the call in
  `try/except (StockNotFound, StockDataUnavailable): return None`, and a missing
  key just makes the provider `None` and silently omits the field. **Enrichment
  must never sink the primary response.**

**Exception → HTTP translation** (done in the endpoint, uniformly):

| Raised | HTTP |
|--------|------|
| `ValueError` (bad/again-normalized input) | 400 |
| `StockNotFound` | 404 |
| `StockDataUnavailable` | 502 |
| missing required API key (in a `get_*` factory) | 503 |

**Config & secrets** come from environment variables, read only in the router's
wiring functions (`APCA_API_KEY_ID`, `FINNHUB_API_KEY`, `FMP_API_KEY`,
`LOGODEV_TOKEN`, `DATABASE_URL`). Build providers lazily so the app boots without
every key. Never hardcode or commit secrets.

**Input normalization** happens once, at the top of the use case
(`_normalize_symbol`), so every layer below sees clean input.

---

## "Where does this go?"

| You're adding… | Put it in |
|----------------|-----------|
| A new concept / a calculation that's a fact about one object | an **entity** (`entities.py`), as a field or `@property` |
| A pure calculation over a price series (no I/O) | a domain helper like `indicators.py` |
| A new action/workflow (validate → fetch → assemble) | a **use case** class in `use_cases.py` |
| A need for data the use case can't compute itself | a new **port** in `ports.py` |
| A call to a third-party API or the database | an **adapter** implementing that port |
| A new field/shape in the JSON response | a **DTO** in `schemas.py` + its `_present_*` mapper |
| A new HTTP route | an **endpoint** + wiring in `router.py` |
| A reusable domain error | `exceptions.py` |

---

## Adding a feature — work inward to outward

1. **Entity** — model the data and its intrinsic rules in `entities.py`.
2. **Port** — declare the interface the use case needs in `ports.py` (returns
   entities, raises domain exceptions).
3. **Use case** — write the `execute()` orchestration in `use_cases.py`, depending
   only on the entity + port.
4. **Adapter** — implement the port against the real vendor/DB in a
   `*_provider.py`; map vendor models → entities and vendor errors → domain
   exceptions.
5. **DTO + presenter** — add the response model in `schemas.py` and a `_present_*`
   in `router.py`.
6. **Endpoint + wiring** — add the route and the `Depends`/`@lru_cache` factory
   in `router.py`; translate exceptions to HTTP.
7. **Test** — drive the use case with a **fake** implementing the port; assert the
   endpoint via `TestClient` with the fake injected through `app.dependency_overrides`.

---

## Testing

Everything runs **offline**. The clean layering is what makes that possible: tests
inject a hand-written `FakeProvider` (implementing the port) instead of mocking
the network or the vendor SDK. Tests use in-memory SQLite and ignore
`DATABASE_URL`. Mirror this — if a test needs the network, the seam is in the
wrong place.

```sh
pytest          # quiet mode is configured in pyproject.toml
```

---

## Commands

```sh
pip install -e ".[dev]"          # install (add ".[postgres]" for the RDS driver)
uvicorn app.main:app --reload    # run locally (docs at /docs)
alembic upgrade head             # apply migrations (schema is Alembic-owned, not create_all)
pytest                           # run the offline test suite
```

To change the DB schema: edit the model in `app/stocks/constituents.py`, then
`alembic revision --autogenerate -m "…"`, review the generated migration, and
`alembic upgrade head`.

---

## Project layout

```
app/
├── main.py                 # FastAPI app: CORS, lifespan, /healthz, include_router
├── db.py                   # engine/session/Base/get_db (DATABASE_URL-driven)
└── stocks/                 # the stocks vertical slice
    ├── entities.py         # ── domain objects + intrinsic rules
    ├── indicators.py       # ── pure domain calc (RSI)
    ├── ports.py            # ── abstract interfaces (ABCs)
    ├── use_cases.py        # ── orchestration (one class per action)
    ├── exceptions.py       # ── domain errors
    ├── *_provider.py       # ── vendor adapters (Alpaca/Finnhub/FMP/Logo.dev/SEC EDGAR)
    ├── adapters/           # ── vendor adapters as *_adapter.py (estimates + quarterly earnings: yfinance + caches)
    ├── stocks/             # ── shared `stocks` anchor slice:
    │   └── models.py            #    StockRecord (the `stocks` table) + get_or_create_stock
    ├── estimates/          # ── analyst-estimates sub-slice:
    │   ├── ports.py             #    live-source port (AnalystEstimatesProvider)
    │   ├── repository.py        #    abstract persistence port (injected into the use case)
    │   ├── db_repository.py     #    concrete repo: maps row⇄entity, calls models
    │   ├── models.py            #    stock_analyst_estimates ORM + query fns (anchor from stocks/)
    │   ├── use_cases.py         #    SyncAnalystEstimates (out-of-band refresh)
    │   └── router.py            #    provider wiring for the snapshot read path
    ├── earnings/quarterly/ # ── quarterly-earnings sub-slice (its OWN entities.py):
    │   ├── entities.py          #    QuarterlyEarnings + QuarterlyEarningsTimeline (slice-local)
    │   ├── ports.py             #    live-source port (QuarterlyEarningsProvider)
    │   ├── repository.py        #    abstract persistence port
    │   ├── db_repository.py     #    concrete repo: maps rows⇄entities, calls models
    │   ├── models.py            #    stock_quarterly_earnings ORM + query fns (anchor from stocks/)
    │   ├── use_cases.py         #    GetQuarterlyEarnings + SyncQuarterlyEarnings
    │   └── schemas.py           #    HTTP response DTOs (the HTTP endpoints live in endpoints/)
    ├── endpoints/          # ── HTTP endpoints outside a read slice:
    │   ├── cron_estimates_endpoints.py           #  POST /internal/estimates/sync
    │   ├── cron_quarterly_earnings_endpoints.py  #  POST /internal/earnings/quarterly/sync
    │   └── quarterly_earnings_endpoints.py       #  GET /stocks/{symbol}/earnings/quarterly
    ├── constituents.py     # ── DB adapter: ORM model + SqlConstituentRepository
    ├── chart_window.py     # ── edge helper: range preset → time window
    ├── schemas.py          # ── HTTP response DTOs (pydantic)
    └── router.py           # ── endpoints + presenters + DI wiring (composition root)
tests/                      # offline; fakes through the ports (mirrors app: tests/stocks, tests/estimates, tests/earnings, tests/adapters)
alembic/                    # database migrations
scripts/sync_constituents.py# ops-time sync (FMP → DB), not called while serving
infra/                      # Terraform (modules + environments)
```

---

## Hard rules

- **Never violate the dependency rule.** No vendor import outside its adapter; no
  adapter/framework import inside an entity or use case.
- **`main` is protected** — branch and open a PR; never push to `main`.
- **Never commit secrets** — keys come from env vars (SSM in AWS).
- **Schema is Alembic-owned** — never `create_all`; migrate.
