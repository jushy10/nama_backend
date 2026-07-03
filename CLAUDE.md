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
                       │ *_provider.py    │  (Alpaca / Finnhub / Logo.dev / Yahoo / DB)
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
`Enum`s that model the concepts (`Stock`, `Quote`, `Candle`, `AnalystEstimates`,
`Constituent`, …). They import nothing from the rest of the app.

Business logic that is **a fact about one entity** lives here, as a `@property`
or `@classmethod` — computed on access, not stored:
- `Stock.change` / `change_percent` / `spread`
- `Candle.is_bullish` (the green/red rule)
- `KeyMetrics.peg`, `AnalystEstimates.forward_pe(price)`
- the slices' `QuarterlyEarnings.beat` and `*Timeline.filled_from(...)` (pure merge logic)

Entities are vendor-agnostic on purpose: e.g. `Timeframe` defines business-level
granularities; the adapter maps them onto whatever the vendor calls them.

Pure cross-entity calculations with no I/O (e.g. RSI math in `indicators.py`)
are also domain code — they live next to the entities, import only entities, and
never reach out for data.

### 2. Ports — `app/stocks/ports.py`
The abstractions a use case depends on. Each is an `ABC` with `@abstractmethod`s
phrased in domain terms (`get_stock`, `get_quotes`, `get_estimates`,
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
(ranking in `ScreenStocks`, the earnings context in `GetStockAnalysis`). It depends
only on entities + ports — never a framework, never a concrete provider.

### 4. Adapters — `app/stocks/*_provider.py`, `app/stocks/adapters/*_adapter.py`, `app/stocks/constituents.py`
*Interface Adapters.* Each implements a port and is **the only module that knows
a given vendor exists**. It translates the vendor's SDK/HTTP/ORM models into our
entities, and the vendor's failures into our domain exceptions. Swap vendors and
only this one file changes.

> Most adapters still sit flat in `app/stocks/` as `<vendor>_<concern>_provider.py`.
> The earnings adapters live in `app/stocks/adapters/` and are named `*_adapter.py`
> (the yfinance live sources, their DB-cache decorators, and the estimates projection);
> other features' adapters can migrate there over time.

- `alpaca_provider.py` — Alpaca SDK → price/quote/candles/performance/sectors
- `finnhub_*_provider.py` — Finnhub → fundamentals (market cap, dividend, ratios, margins) / company name (`/stock/profile2`)
- `logodev_provider.py` — Logo.dev → logo image
- `caching_company_profile_provider.py` — decorator adapter (wraps another adapter to add an in-process TTL cache; same port in, same port out)
- `adapters/yfinance_quarterly_earnings_adapter.py` — live source for the quarterly-earnings slice: **Yahoo via `yfinance`**, building the 4-recent + up-to-2-upcoming quarter timeline. **Past** quarters come from `earnings_dates` (reported EPS vs the estimate that preceded it; surprise computed here, not from Yahoo's `Surprise(%)`). **Upcoming** quarters come from the `0q`/`+1q` rows of `earnings_estimate` + `revenue_estimate` — the reliable source of *two* forward quarters (EPS + revenue), so a stock surfaces both even when `earnings_dates` lists only one scheduled future date; a scheduled date is attached when it lines up. **Reported revenue** (`revenue_actual`) is matched onto the past quarters from `quarterly_income_stmt` (Total Revenue, whose columns carry the *true* fiscal period-end dates: each quarter takes the column most recently preceding its announcement date — never the calendar-derived label, which for off-calendar filers like MU names a different fiscal quarter than the EPS) — best-effort enrichment, so a failure fetching it drops the actual without sinking the timeline. Fiscal labels are derived from the announcement date (calendar best-effort; the offset is cosmetic — a row's EPS and revenue always belong to the same fiscal quarter). `adapters/db_cached_quarterly_earnings_adapter.py` — a **read-through** DB cache in front of it: serves stored rows if present, else fetches from Yahoo **once on a miss** and stores. **No TTL/staleness or serve-stale**; a populated symbol is always served straight from the DB, and keeping rows current is entirely the cron's job
- `adapters/yfinance_annual_earnings_adapter.py` — live source for the annual-earnings slice: **Yahoo via `yfinance`**, building the 4-recent + up-to-2-upcoming *fiscal-year* timeline (the yearly analogue of the quarterly adapter). **Past** years come from `income_stmt` (annual) — `Diluted EPS` (falling back to `Basic EPS`) as the actual, plus `Total Revenue` and `Net Income`. **Upcoming** years come from the `0y`/`+1y` rows of `earnings_estimate` + `revenue_estimate` (EPS + revenue) — Yahoo's forward ceiling (so ≤2). Forward years are labelled by `info['nextFiscalYearEnd']` (0y), falling back to one year past the latest reported year. **No annual surprise/beat** — Yahoo's estimate-vs-actual history is per-quarter, so a reported year carries an actual with no estimate. Reported years also carry `eps_actual_consensus` — the year's actual EPS on the **analyst-consensus (adjusted) basis**, i.e. the sum of its four quarterly "Reported EPS" values from a deeper `get_earnings_dates` fetch (quarters assigned to a fiscal year by their derived calendar quarter-end falling within the year ending at the true fiscal-year-end; summed only when all four slots are filled, else `None`). It exists because `eps_actual` (GAAP diluted) and the forward `eps_estimate` (adjusted consensus) are on different bases — a client anchoring a P/E walk needs both ends on one basis. Best-effort enrichment, like revenue. Key caveat: `income_stmt` is the **fundamentals endpoint Yahoo IP-gates hardest from data-centre IPs** (intermittently — prod has fetched it successfully), so it's fetched best-effort: a blocked fetch drops the reported years but leaves the forward ones, and the **merge-preserving sync** keeps the stored reported rows when that happens. `adapters/db_cached_annual_earnings_adapter.py` — the same **read-through** DB cache as quarterly (DB-first, fetch-on-miss, no TTL/serve-stale)
- `adapters/annual_earnings_estimates_adapter.py` — implements the `AnalystEstimatesProvider` port for the stock snapshot by **projecting the annual-earnings slice's stored forward years** into an `AnalystEstimates` block (first upcoming year → FY1, next → FY2). **DB-only, no live fall-through**: estimates are best-effort enrichment, so an uncached symbol just omits the forward metrics until the annual read path (lazy fill) or its cron populates the rows. This replaced the dedicated `stock_analyst_estimates` table + its own Yahoo fetch and cron — the annual slice stores the *same* `earnings_estimate`/`revenue_estimate` consensus, so the snapshot's `forward_pe`/forward growth now have one source of truth (the FY1 low/high range and analyst counts were dropped with the table; the serialized `analyst_estimates` block, `forward_ps`, and `metrics.ps`/`metrics.beta` were later trimmed off the HTTP response — the entities keep them, feeding `forward_pe`, the growth block, and the Bedrock analysis context)
- `adapters/yfinance_recommendations_adapter.py` — live source for the recommendations slice: **Yahoo via `yfinance`** (`Ticker.recommendations`), the sell-side buy/hold/sell split as monthly snapshots (the same recommendation-trend data Finnhub serves, but keyless — this replaced `finnhub_recommendation_provider.py` and the `FINNHUB_API_KEY` gate on the endpoint). Yahoo labels the rows *relatively* (`0m` = this month, `-1m`, …), so the adapter anchors them on today's month into first-of-month `period` dates — the identity the DB cache keys on. `adapters/db_cached_recommendations_adapter.py` — the same **read-through** DB cache as the earnings slices (DB-first, fetch-on-miss, no TTL/serve-stale)
- `constituents.py` — owns the SQLAlchemy `ConstituentRecord` model **and** `SqlConstituentRepository`; the DB schema lives here, the entity stays ORM-free
- `stocks/models.py` — the shared `stocks` anchor as its own tiny slice (`app/stocks/stocks/`): owns the `StockRecord` model (the `stocks` table) + `get_or_create_stock`. Owned by no single feature; per-feature tables hang off it and import it from here

Naming: `<vendor>_<concern>_provider.py` for the flat adapters; `<vendor>_<concern>_adapter.py` for those under `app/stocks/adapters/`.

> **Analyst estimates (the snapshot's forward consensus).** There is deliberately **no
> estimates slice or table any more** (the `app/stocks/estimates/` sub-slice, its
> `stock_analyst_estimates` table, and the `sync-estimates` workflow were removed by
> migration 0006). The `AnalystEstimatesProvider` port lives in `app/stocks/ports.py`
> beside the other snapshot-enrichment ports, and the wiring
> (`get_estimates_provider` in `app/stocks/router.py`) builds
> `adapters/annual_earnings_estimates_adapter.py`, which projects the annual-earnings
> slice's stored forward years into the `AnalystEstimates` entity. Freshness therefore
> rides entirely on the annual slice: lazy fill on the earnings read + the
> `sync-annual-earnings` cron.

> **The quarterly-earnings sub-slice — `app/stocks/earnings/quarterly/`.** A fully
> self-contained slice with its **own `entities.py`** (rather than reusing the
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
> workflow). Three deliberate design choices: (1) the table
> (`stock_quarterly_earnings`) is a **time series** (many rows per stock, unique on
> `stock_id` + fiscal year + quarter), not one wide row; (2) the read cache is a plain
> **read-through** (DB-first, fetch-on-miss only — **no TTL/staleness or serve-stale**), so
> a populated symbol is always served from the DB and freshness is entirely the cron's job;
> (3) the **sync is merge-preserving** — an **empty** live result is skipped outright, and a
> *degraded* one is filled from the stored rows before the upsert (`filled_from` on the
> timeline entity: field-level carry-forward per fiscal key, stored reported rows retained
> when the fresh window drops them, reported never downgraded, window capped so it doesn't
> grow) — because the upsert rewrites a stock's whole window via delete-then-insert, and a
> Yahoo-blocked fetch must not wipe good history (revenue actuals especially).
> Fiscal labels are a calendar best-effort — `earnings_dates` carries only the
> announcement date, so the period end is the most recent calendar quarter-end before it
> (exact for calendar fiscal years, a label offset for others).

> **The annual-earnings sub-slice — `app/stocks/earnings/annual/`.** The yearly analogue of
> the quarterly slice, built to mirror it: a fully self-contained slice with its **own
> `entities.py`** (`AnnualEarnings` + `AnnualEarningsTimeline`), plus
> `ports` / `repository` / `db_repository` / `models` / `use_cases` / `schemas` (both HTTP
> endpoints live in `app/stocks/endpoints/`: the read `annual_earnings_endpoints.py` and the
> `cron_annual_earnings_endpoints.py`, so the slice has no `router.py`). It serves a stock's
> 4 most-recent reported fiscal years (reported diluted EPS + revenue + **net income**, plus
> `eps_actual_consensus` — the year's actual on the analyst-consensus/adjusted basis, summed
> from its four quarterly "Reported EPS" announcements so a client can anchor a P/E walk on
> the same basis the forward estimates are quoted on; best-effort, `None` when the history
> can't fill all four quarters) and
> up to **2** upcoming years (the `0y`/`+1y` forward EPS + revenue estimates — Yahoo's forward
> ceiling, so 2 is the max, often 1) at `GET /stocks/{symbol}/earnings/annual`, in a single
> **chronological** run (oldest reported → furthest upcoming). Live source is **yfinance
> (Yahoo)** via `income_stmt` (past) + `earnings_estimate`/`revenue_estimate` `0y`/`+1y`
> (upcoming) (`adapters/yfinance_annual_earnings_adapter.py`), behind the same persistent
> **read-through** DB cache + out-of-band cron (`POST /internal/earnings/annual/sync`, driven
> by the `sync-annual-earnings` workflow); table `stock_annual_earnings` (migration 0005), a
> time series unique on `stock_id` + fiscal year. **Two divergences from the quarterly slice:**
> (1) **no surprise/beat** — Yahoo publishes no historical *annual* estimate, so a reported
> year carries an actual with no estimate; (2) the reported half is sourced from Yahoo's
> **fundamentals endpoint (`income_stmt`), which it IP-gates hardest**, so it's best-effort
> and the gating is **intermittent** — a blocked fetch yields a forward-only timeline. The
> **merge-preserving sync** (the same `filled_from` guard the quarterly slice uses) is what
> makes that survivable: stored reported years are retained when a refresh comes back
> without them, so a bad Yahoo day delays new data but never erases existing rows.
> Fiscal-year labels are more exact than quarterly's — `income_stmt` reports the
> true fiscal-year-end date, so the label is that date's calendar year.

> **The recommendations sub-slice — `app/stocks/recommendations/`.** Analyst
> recommendation trends (the sell-side buy/hold/sell split by month), built on the same
> skeleton as the earnings sub-slices: its **own `entities.py`** (`RecommendationTrend` +
> `AnalystRecommendations`, which carry the consensus `score`/`consensus` bands and the
> month-over-month `direction` as entity properties), plus
> `ports` / `repository` / `db_repository` / `models` / `use_cases` / `schemas` (both HTTP
> endpoints live in `app/stocks/endpoints/`: the read `recommendations_endpoints.py` and the
> `cron_recommendations_endpoints.py`). Serves `GET /stocks/{symbol}/recommendations`,
> newest snapshot first. Live source is **yfinance (Yahoo)** via `Ticker.recommendations`
> (`adapters/yfinance_recommendations_adapter.py`) — this replaced Finnhub's
> `/stock/recommendation`, dropping the endpoint's `FINNHUB_API_KEY` 503 gate — behind the
> same persistent **read-through** DB cache + out-of-band cron
> (`POST /internal/recommendations/sync`, driven by the **daily** `sync-recommendations`
> workflow — daily rather than weekly because the current month's counts drift as analysts
> revise and the read cache has no TTL); table `stock_recommendation_trends` (migration
> 0007), a time series unique on `stock_id` + `period` (first-of-month). **One deliberate
> divergence from the earnings slices: the upsert *merges* instead of rewriting** — it
> replaces the months the source served and keeps earlier stored months, because a past
> month's split is a frozen fact and Yahoo serves only ~4 months at once, so the table
> accumulates a longer history than the source. Consequently `refresh_targets` orders
> staleness by the **max** `fetched_at` per stock (the last refresh), not the min — the
> merge keeps ancient stamps on old months forever. The sync still skips an empty live
> result (nothing to merge; the stock's refresh stamp must not stall the stale queue).
> Caveat: the derived `period` is only as true as the relative labels — a symbol fetched
> near a month boundary can label a snapshot one month off; cosmetic, same spirit as the
> earnings slices' calendar-derived fiscal labels.

> **The ticker sub-slice — `app/stocks/ticker/`.** A stock's **ticker card** at
> `GET /stocks/ticker/{symbol}`: the live quote (`price`/`change`/`change_percent`, same
> rules as every other price view), best-effort enrichment (`market_cap` + dividend from
> Finnhub, `performance` trailing windows from Alpaca), and `metrics.forward_peg` — the
> **forward PEG**, the one valuation figure no other endpoint serves: forward P/E (live
> price ÷ FY1 consensus EPS) divided by expected FY1→FY2 EPS growth (a `@property` on the
> slice-local `TickerValuation` entity, with the same positive-legs guard as the trailing
> `KeyMetrics.peg` — it exists because a trailing PEG divides by *already-reported*
> growth, which a cyclical rebound can inflate into the hundreds of percent and pin the
> ratio near zero). The PEG's *legs* deliberately stay snapshot-only (`forward_pe`,
> `growth.forward_eps_growth` on `GET /stocks/{symbol}`) so the same numbers don't get two
> homes that could disagree; the entity's `symbol` is renamed `ticker` at the DTO. Built
> on the same skeleton as the other sub-slices (own `entities.py` / `use_cases.py` /
> `schemas.py`, endpoint in `app/stocks/endpoints/ticker_endpoints.py`) but deliberately
> **thinner: no table, repository, cron, or vendor adapter** — the card is built around
> the live quote, so nothing slice-owned is worth persisting. The use case pulls
> everything through *existing* ports — `StockQuoteProvider` + `StockPerformanceProvider`
> (the Alpaca singleton, whose missing-keys 503 gate it inherits — the quote is primary),
> `StockFundamentalsProvider` (Finnhub, `None` without a key), and
> `AnalystEstimatesProvider` (the annual-earnings projection, DB-only) — wired by reusing
> the composition root's factories from `router.py`; the composite result (`TickerCard`)
> is a dataclass beside the use case, not a slice entity, since it just bundles shared
> entities around the slice's one domain rule. Quote + estimates are primary (errors
> propagate); fundamentals/performance are enrichment and never sink the card. Consensus
> freshness rides entirely on the annual slice (lazy fill + `sync-annual-earnings` cron);
> an uncached symbol is a **200 with a null `metrics.forward_peg`**, not a 404 — no data ≠
> error. Caveat: the growth denominator is a single FY1→FY2 leg (Yahoo's forward ceiling),
> not the classic five-year rate, so one boom-year estimate can still flatter the ratio.

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
wiring functions (`APCA_API_KEY_ID`, `FINNHUB_API_KEY`, `LOGODEV_TOKEN`,
`DATABASE_URL`). The `/internal/*/sync` cron endpoints are currently
**unauthenticated** (an auth-token guard is planned, deferred for now). Build
providers lazily so the app boots without every key. Never hardcode or commit
secrets.

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

> **Keep the migration `revision` id ≤ 32 chars.** Alembic's `alembic_version.version_num`
> column is `VARCHAR(32)`. SQLite ignores the length so an over-long id passes the local
> tests, but Postgres (RDS) enforces it and the deploy's `alembic upgrade head` fails with
> `value too long for type character varying(32)`. Follow the existing short ids
> (`000N_<concern>`), not the verbose file name.

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
    ├── *_provider.py       # ── vendor adapters (Alpaca/Finnhub/Logo.dev)
    ├── adapters/           # ── vendor adapters as *_adapter.py (quarterly/annual earnings: yfinance + caches; estimates projection)
    ├── stocks/             # ── shared `stocks` anchor slice:
    │   └── models.py            #    StockRecord (the `stocks` table) + get_or_create_stock
    ├── earnings/quarterly/ # ── quarterly-earnings sub-slice (its OWN entities.py):
    │   ├── entities.py          #    QuarterlyEarnings + QuarterlyEarningsTimeline (slice-local)
    │   ├── ports.py             #    live-source port (QuarterlyEarningsProvider)
    │   ├── repository.py        #    abstract persistence port
    │   ├── db_repository.py     #    concrete repo: maps rows⇄entities, calls models
    │   ├── models.py            #    stock_quarterly_earnings ORM + query fns (anchor from stocks/)
    │   ├── use_cases.py         #    GetQuarterlyEarnings + SyncQuarterlyEarnings
    │   └── schemas.py           #    HTTP response DTOs (the HTTP endpoints live in endpoints/)
    ├── earnings/annual/    # ── annual-earnings sub-slice (its OWN entities.py; mirrors quarterly):
    │   ├── entities.py          #    AnnualEarnings + AnnualEarningsTimeline (slice-local, no surprise)
    │   ├── ports.py             #    live-source port (AnnualEarningsProvider)
    │   ├── repository.py        #    abstract persistence port
    │   ├── db_repository.py     #    concrete repo: maps rows⇄entities, calls models
    │   ├── models.py            #    stock_annual_earnings ORM + query fns (anchor from stocks/)
    │   ├── use_cases.py         #    GetAnnualEarnings + SyncAnnualEarnings
    │   └── schemas.py           #    HTTP response DTOs (the HTTP endpoints live in endpoints/)
    ├── recommendations/    # ── recommendations sub-slice (its OWN entities.py; merge-upsert cache):
    │   ├── entities.py          #    RecommendationTrend + AnalystRecommendations (slice-local)
    │   ├── ports.py             #    live-source port (RecommendationProvider)
    │   ├── repository.py        #    abstract persistence port
    │   ├── db_repository.py     #    concrete repo: maps rows⇄entities, calls models
    │   ├── models.py            #    stock_recommendation_trends ORM + query fns (anchor from stocks/)
    │   ├── use_cases.py         #    GetStockRecommendations + SyncRecommendations
    │   └── schemas.py           #    HTTP response DTOs (the HTTP endpoints live in endpoints/)
    ├── ticker/             # ── ticker-card sub-slice (its OWN entities.py; no DB/cron —
    │   │                   #    computed per request from live quote + stored consensus):
    │   ├── entities.py          #    TickerValuation (forward P/E + growth legs, forward_peg property)
    │   ├── use_cases.py         #    GetTickerCard + TickerCard composite (quote/estimates/fundamentals/performance ports)
    │   └── schemas.py           #    HTTP response DTO (quote + enrichment + metrics.forward_peg; endpoint in endpoints/)
    ├── endpoints/          # ── HTTP endpoints outside a read slice:
    │   ├── cron_quarterly_earnings_endpoints.py  #  POST /internal/earnings/quarterly/sync
    │   ├── quarterly_earnings_endpoints.py       #  GET /stocks/{symbol}/earnings/quarterly
    │   ├── cron_annual_earnings_endpoints.py     #  POST /internal/earnings/annual/sync
    │   ├── annual_earnings_endpoints.py          #  GET /stocks/{symbol}/earnings/annual
    │   ├── cron_recommendations_endpoints.py     #  POST /internal/recommendations/sync
    │   ├── recommendations_endpoints.py          #  GET /stocks/{symbol}/recommendations
    │   └── ticker_endpoints.py                   #  GET /stocks/ticker/{symbol}
    ├── constituents.py     # ── DB adapter: ORM model + SqlConstituentRepository
    ├── chart_window.py     # ── edge helper: range preset → time window
    ├── schemas.py          # ── HTTP response DTOs (pydantic)
    └── router.py           # ── endpoints + presenters + DI wiring (composition root)
tests/                      # offline; fakes through the ports (mirrors app: tests/stocks, tests/earnings, tests/recommendations, tests/ticker, tests/adapters, tests/endpoints)
alembic/                    # database migrations
infra/                      # Terraform (modules + environments)
```

---

## Hard rules

- **Never violate the dependency rule.** No vendor import outside its adapter; no
  adapter/framework import inside an entity or use case.
- **`main` is protected** — branch and open a PR; never push to `main`.
- **Never commit secrets** — keys come from env vars (SSM in AWS).
- **Schema is Alembic-owned** — never `create_all`; migrate.
