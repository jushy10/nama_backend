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
| Adapters | `*_provider.py`, `adapters/*_adapter.py` | entities, ports, exceptions, **+ the vendor SDK / `httpx` / SQLAlchemy** | other adapters, use cases, FastAPI, pydantic |
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
…). They import nothing from the rest of the app.

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
(the earnings context in `GetStockAnalysis`). It depends
only on entities + ports — never a framework, never a concrete provider.

### 4. Adapters — `app/stocks/*_provider.py`, `app/stocks/adapters/*_adapter.py`
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
- `adapters/annual_earnings_estimates_adapter.py` — implements the `AnalystEstimatesProvider` port by **projecting the annual-earnings slice's stored forward years** into an `AnalystEstimates` block (first upcoming year → FY1, next → FY2); it feeds the enriched stock snapshot (`GetStockInfo`, now the AI analysis context — the standalone `GET /stocks/{symbol}` endpoint was removed) and the ticker card's forward PEG. **DB-only, no live fall-through**: estimates are best-effort enrichment, so an uncached symbol just omits the forward metrics until the annual read path (lazy fill) or its cron populates the rows. This replaced the dedicated `stock_analyst_estimates` table + its own Yahoo fetch and cron — the annual slice stores the *same* `earnings_estimate`/`revenue_estimate` consensus, so the forward consensus has one source of truth (the FY1 low/high range and analyst counts were dropped with the table; the entities keep the full block, feeding `forward_pe`, the growth block, and the Bedrock analysis context)
- `adapters/yfinance_recommendations_adapter.py` — live source for the recommendations slice: **Yahoo via `yfinance`** (`Ticker.recommendations`), the sell-side buy/hold/sell split as monthly snapshots (the same recommendation-trend data Finnhub serves, but keyless — this replaced `finnhub_recommendation_provider.py` and the `FINNHUB_API_KEY` gate on the endpoint). Yahoo labels the rows *relatively* (`0m` = this month, `-1m`, …), so the adapter anchors them on today's month into first-of-month `period` dates — the identity the DB cache keys on. `adapters/db_cached_recommendations_adapter.py` — the same **read-through** DB cache as the earnings slices (DB-first, fetch-on-miss, no TTL/serve-stale)
- `adapters/yfinance_options_adapter.py` — live source for the ticker card's `options_metrics` block: **Yahoo via `yfinance`** (`Ticker.options` for the expiration list, `Ticker.option_chain(date)` for one expiry's calls/puts), keyless, implementing the ticker slice's `OptionChainProvider` port. Maps chain rows → `OptionContract` entities (strike, bid/ask/last, volume, open interest, IV); every *derived* figure (ATM IV, expected move, insurance cost, put/call) is entity logic, not adapter logic. **No DB cache or cron** — options prices decay by the hour, so the no-TTL read-through pattern doesn't fit; the read is live per request (the endpoint's 5-min Cache-Control is the only damping) and best-effort even when requested, since Yahoo intermittently blocks data-centre IPs
- `adapters/wikipedia_index_membership_adapter.py` — live source for the index-membership slice: **Wikipedia** (`List_of_S&P_500_companies` + `Nasdaq-100`) via `httpx` + `pandas.read_html`, implementing `IndexMembershipSource`. **Keyless** — this replaced Finnhub's `/index/constituents`, which is a **paid** capability the deployed key `403`'d on; Wikipedia welcomes data-centre-IP reads (works from Fargate where Yahoo/Nasdaq/ETF-issuer endpoints block us), so the wiring is now always-constructable like the universe sweep (no `FINNHUB_API_KEY`, no 503 gate). Parses by **column signature** — reads every table and keeps the one whose flat `Symbol`/`Ticker` column yields the most tickers — so each page's *changes* log (S&P "Selected changes", Nasdaq "Component changes") is ignored, directly fixing the bug that sank the **earlier** Wikipedia attempt (it grabbed the Nasdaq-100 change-log table). Sends a descriptive `User-Agent` (Wikipedia asks). Fetches each page independently, normalizes tickers to the anchor's convention (`BRK.B` → `BRK-B`), and returns the two ticker sets; a single page's failure (transport / non-200 / unparseable body) degrades to empty (the other still syncs), both failing raises `StockDataUnavailable`. Same fake-`_http` seam the other adapters use for offline tests. (The earlier abandoned attempt is what the docstring's caution refers to; the issuer-ETF/Yahoo routes remain blocked from data-centre IPs — Wikipedia is the one that isn't.)
- `adapters/yfinance_screener_adapter.py` — live source for the universe slice: **Yahoo via `yfinance`** (`yf.screen` + `EquityQuery`), the ≥$1B US screen (`ScreenedStock` per row) written onto the `stocks` anchor. Each screen quote also carries the `regularMarketPrice`, which the adapter keeps (positive only) on `ScreenedStock.price` — not persisted itself, but the price leg the sync's valuation pass pairs with the quarterly TTM to derive the stored `pe_ratio`
- `adapters/yfinance_etf_screener_adapter.py` — live source for the ETF slice's bulk screen: **Yahoo via `yfinance`** (`yf.screen` with a *custom* `ETFQuery` — `region == us` and `fundnetassets >= min_net_assets`, ranked by AUM), the US ETFs at/above an AUM floor (`ScreenedEtf` per row — AUM + expense ratio) written into the slice's own `etfs` table. Screens the full US ETF universe by AUM the way `yfinance_screener_adapter` screens stocks by market cap — the floor (`SyncEtfs.MIN_NET_ASSETS`, **$1B**, ~1,000 funds) is a use-case constant passed into the port, the exact `MIN_MARKET_CAP` pattern. (This replaced Yahoo's *predefined* `top_etfs_us` screen — a fixed curated ~540-fund list that couldn't be widened; the old "`FundQuery` has no net-assets field" limitation was the *mutual-fund* query, but the ETF query carries `fundnetassets`, so it filters **and** ranks by AUM.) Every row carries `netAssets`, so the read side sorts by AUM. Carries no category or profile — that's the per-ticker enrichment pass's job (`EtfProfileProvider`). Drops a stray non-fund row (`quoteType` present and not `ETF`) the broad US screen can surface, so the table holds only funds. Folds `PCX` (NYSE Arca — the primary ETF venue the stock screen never sees) into `NYSE`, its parent, so `exchange` stays inside the same `NASDAQ`/`NYSE`/`AMEX`/`BATS` vocabulary the stock screen uses (migration 0018 backfilled the earlier `NYSEARCA` rows, since `exchange` is written fill-once)
- `adapters/yfinance_etf_profile_adapter.py` — the ETF slice's per-ticker **profile** enrichment, implementing `EtfProfileProvider`: **Yahoo via `yfinance`**, reading `Ticker.info` (category, `fundFamily`, `navPrice`, `yield`, the trailing-return ladder) + `Ticker.funds_data` (description, `top_holdings`, `sector_weightings`), keyless. The bulk screen carries none of this (Yahoo publishes it only per-ticker), so the sync fetches it a fund at a time and **persists** it — the scalars onto the `etfs` row, the two lists into the `etf_sector_weightings` / `etf_top_holdings` child tables — and the detail endpoint serves that stored profile from the DB. **One exception: the trailing-return ladder (ytd/3y/5y) is fetched but no longer persisted** (migration 0021 dropped those columns) — only the detail card's `performance` block surfaces the 3y/5y, so the read path fetches them **live** from this same adapter when that block is requested (best-effort, the sole live Yahoo call on the ETF read path), rather than storing a snapshot that drifts between syncs. One fetch per fund covers everything, so this **subsumed the old single-column category adapter** (`yfinance_etf_category_adapter`, removed) — category rides the same `.info` blob. Per-field unit normalization to human percent (Yahoo mixes fractions and already-percent numbers; verified against VOO), and the shared `yfinance_session` crumb-401 retry like the stock classifier. Contract: **raises `StockDataUnavailable` on a hard `.info` read** (a raised error or an empty-after-retry `.info` — the block signal, so the sync skips + retries the fund and leaves its stored profile intact), best-effort past that (a served-but-sparse fund, or a failed `funds_data`, yields a partial profile)
- `stocks/models.py` — the shared `stocks` anchor as its own tiny slice (`app/stocks/stocks/`): owns the `StockRecord` model (the `stocks` table — `ticker` (unique lookup; the column was renamed from `symbol` by migration 0010 — the domain layers still say "symbol"), the fill-once identity facts `name` and `exchange`, and the mutable `revenue_growth_yoy` / `eps_growth_yoy` **latest trailing YoY snapshot** (migration 0011 — percent; EPS on the analyst-consensus/adjusted basis; **overwritten** every refresh by the annual-earnings slice as the newest reported year rolls forward, unlike the fill-once facts) and their **forward** counterparts `forward_revenue_growth_yoy` / `forward_eps_growth_yoy` (migration 0018 — the analyst-consensus FY1→FY2 change, the same figures the ticker card's forward PEG is built on; written the same way by the annual slice from its stored forward years, both legs on the consensus basis; more often null since they need *two* upcoming years), the universe screen facts `sector` / `industry` / `market_cap` / `screened_at` (migration 0012; `industry` added by 0013), the `in_sp500` / `in_nasdaq100` index-membership flags (migration 0014 — `NOT NULL`, default `False`; reconciled by the index-membership slice: current members marked, drop-outs cleared), and the `pe_ratio` trailing-P/E snapshot (migration 0017 — the consensus-basis figure the ticker card computes live, materialized for search sorting; **overwritten** every run by the universe sync's valuation pass, like `market_cap`, and null until four quarters are cached or on a trailing loss)) and its helpers `get_or_create_stock`, `anchor_facts`, `fill_exchange`. Owned by no single feature; per-feature tables hang off it and import it from here

Naming: `<vendor>_<concern>_provider.py` for the flat adapters; `<vendor>_<concern>_adapter.py` for those under `app/stocks/adapters/`.

> **Analyst estimates (the forward consensus).** There is deliberately **no
> estimates slice or table any more** (the `app/stocks/estimates/` sub-slice, its
> `stock_analyst_estimates` table, and the `sync-estimates` workflow were removed by
> migration 0006). The `AnalystEstimatesProvider` port lives in `app/stocks/ports.py`
> beside the other snapshot-enrichment ports, and the wiring
> (`get_estimates_provider` in `app/stocks/router.py`) builds
> `adapters/annual_earnings_estimates_adapter.py`, which projects the annual-earnings
> slice's stored forward years into the `AnalystEstimates` entity. It backs the AI
> analysis context (via `GetStockInfo`) and the ticker card's forward PEG. Freshness
> therefore rides entirely on the annual slice: lazy fill on the earnings read + the
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
> The slice also computes the stock's **latest trailing YoY growth** — `revenue_growth_yoy`
> and `eps_growth_yoy` (percent), the newest reported year over the one before it — as
> `@property`s on `AnnualEarningsTimeline` (`revenue_actual` for revenue; `eps_actual_consensus`
> on *both* legs for EPS, so it's real growth and not a GAAP-vs-adjusted artifact; the same
> positive-prior guard as the PEGs). These are *trailing* (reported actuals, the backward-looking
> cousin of `AnalystEstimates.forward_*_growth`). Served top-level on the read endpoint **and**
> persisted as a moving snapshot on the shared `stocks` anchor — the single write point,
> `SqlAnnualEarningsRepository.upsert`, overwrites the pair on every refresh (cron sync *and*
> lazy fill both funnel through it), so a stock carries just the current pair (dropping to
> `null` if a degraded window leaves fewer than two reported years). One figure per stock, not
> a per-year history — the anchor is one row per stock.

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
> `GET /stocks/ticker/{ticker}`. Always served: the live quote
> (`price`/`change`/`change_percent`, same rules as every other price view), the two
> **DB-first identity facts** — `name` (from the Finnhub profile) and `exchange` (from
> the Alpaca full snapshot) — each lazily filled **once** per symbol into the `stocks`
> anchor (`name` was always on it; `exchange` came with migration 0009) and served from
> the row forever after, since neither effectively ever changes (a rebrand needs a
> manual row update; the slice's `repository.py`/`db_repository.py` is that anchor-level
> read/fill, no slice-owned table), and the **read-only anchor facts** the card just
> serves off the same row — `market_cap` / `sector` / `industry` (the universe screen's
> facts) — all but the quote best-effort and `null` until their sync reaches the stock
> (e.g. a symbol not yet screened has no market cap; unlike the old behaviour, the card
> no longer falls back to Finnhub for it). One anchor read (`models.anchor_facts`, a
> `Row` mapped into `StoredTickerFacts`) serves all of them plus the growth pair below.
> **Opt-in blocks** via `?include=` (repeated or comma-separated;
> unknown values are a 400; unrequested blocks are `null` and — pay-per-use — cost no
> provider call — and with market cap now off the anchor, the **fundamentals call itself
> is opt-in**: only `dividend`/`metrics` pull it, so a bare card makes zero Finnhub
> calls): `dividend` (`yield_percentage` + `per_share`, rounded to 2 decimals; rides the
> fundamentals call that `metrics` also needs), `performance` (trailing windows from
> Alpaca), `options_metrics` (the **options-market read**, below), and `metrics` — the trailing `pe` on the
> **analyst-consensus (adjusted) EPS basis**: live price ÷ the quarterly-earnings slice's
> `ttm_eps` (a timeline `@property` — the sum of the 4 newest reported quarters'
> consensus-basis `eps_actual`; `null` until 4 quarters are cached, or when the trailing
> year is a loss). Deliberately *not* Finnhub's GAAP-ish `peTTM`, so the trailing and
> forward multiples sit on **one EPS basis** (the same reason the annual slice carries
> `eps_actual_consensus`); the TTM read reuses the quarterly slice's read-through DB cache
> through its `QuarterlyEarningsProvider` port (lazy fill on a cold miss) and is
> best-effort even when requested — a Yahoo-blocked fetch nulls the multiple, never the
> card. Beside it: `peg` + `gross_margin`/`operating_margin`/`net_margin` (off that same
> fundamentals call — the `peg` stays Finnhub's internally-consistent trailing read) and
> `forward_peg`, the **forward PEG**, the one valuation figure
> no other endpoint serves: forward P/E (live
> price ÷ FY1 consensus EPS) divided by expected FY1→FY2 EPS growth (a `@property` on the
> slice-local `TickerValuation` entity, with the same positive-legs guard as the trailing
> `KeyMetrics.peg` — it exists because a trailing PEG divides by *already-reported*
> growth, which a cyclical rebound can inflate into the hundreds of percent and pin the
> ratio near zero; the card serves both PEGs side by side for exactly that contrast). The
> forward PEG's *legs* (`forward_pe`, the forward EPS growth) are deliberately not
> serialized on the card — and since the standalone `GET /stocks/{symbol}` snapshot
> endpoint was removed, no endpoint serves them; they live on the shared entities and
> feed the Bedrock analysis context; the entity's `symbol` is renamed `ticker` at the DTO.
> The `metrics` block also carries the **latest trailing YoY growth** —
> `revenue_growth_yoy` + `eps_growth_yoy` (percent, EPS on the consensus basis) — read
> straight off the `stocks` anchor where the annual slice writes them (so they ride the
> one anchor read, not Finnhub, and survive a keyless/blocked fundamentals call); `null`
> until the annual slice has two reported years cached. They pair the backward-looking
> growth with the forward PEG's forward-looking one, in the block already about growth.
> `options_metrics` is what the options market *believes* about the stock, for a buyer
> sizing an entry — four derived figures, deliberately not a chain browser: ATM implied
> volatility (percent, ~1-month expiry), the priced-in `expected_move_percent` (the ATM
> straddle over spot, by `expected_move_by`), `insurance_cost_percent` (an ATM protective
> put ~3 months out, over spot), and the day's `put_call_ratio` (volume across the two
> sampled expiries, deduped when sparse listings land both windows on one expiry). The
> derivations are pure entity logic (`OptionContract` + `TickerOptionsMetrics.from_chains`
> in the slice's `entities.py`); the chain arrives through the slice-local
> `OptionChainProvider` port (`ticker/ports.py` — expirations first, then only the two
> needed expiries) implemented by `adapters/yfinance_options_adapter.py` (Yahoo via
> `yfinance`, keyless). Unlike `metrics`, this block is **best-effort even when
> requested** — it's a live Yahoo call and Yahoo intermittently blocks data-centre IPs,
> so a blocked read is a 200 with a null block, never a failed card. Built
> on the same skeleton as the other sub-slices (own `entities.py` / `ports.py` /
> `use_cases.py` / `schemas.py`, endpoint in `app/stocks/endpoints/ticker_endpoints.py`)
> but deliberately
> **thinner: no table of its own, no cron** — the card is built around
> the live quote, so nothing beyond the exchange is worth persisting. The use case pulls
> everything else through *existing* ports — `StockQuoteProvider` + `StockPerformanceProvider`
> + `StockDataProvider` (the Alpaca singleton, whose missing-keys 503 gate it inherits —
> the quote is primary; the full-snapshot port only backs the one-time exchange fill),
> `StockFundamentalsProvider` + `CompanyProfileProvider` (Finnhub, `None` without a key),
> and `AnalystEstimatesProvider` (the annual-earnings projection, DB-only) — wired by reusing
> the composition root's factories from `router.py`; the composite result (`TickerCard`)
> is a dataclass beside the use case, not a slice entity, since it just bundles shared
> entities around the slice's domain rules (it also carries the `include` set so the
> presenter can tell "not requested" from "requested but unavailable"). The quote — and
> the consensus read *when `metrics` is requested* — are primary (errors propagate);
> name/exchange/fundamentals/performance/options are enrichment and never sink the card. Consensus
> freshness rides entirely on the annual slice (lazy fill + `sync-annual-earnings` cron);
> an uncached symbol is a **200 with a null `metrics.forward_peg`**, not a 404 — no data ≠
> error. Caveats: the growth denominator is a single FY1→FY2 leg (Yahoo's forward ceiling),
> not the classic five-year rate, so one boom-year estimate can distort the ratio — and
> because a boom *current* year (the `0y` the forward P/E anchors on) can leave that leg
> near zero and blow the ratio up (GOOGL mid-2026: a 25.8 forward P/E over 2.1% growth is
> an arithmetically-correct-but-useless PEG of ~12), `forward_peg` is **suppressed to
> `null` below a growth floor** (`_MIN_FORWARD_EPS_GROWTH`, 5%) where the division is too
> unstable to inform; and the `put_call_ratio` pools only the two sampled expiries (not the
> whole board), so thin sessions read noisier than a market-wide ratio.

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
`DATABASE_URL`, `CRON_SYNC_TOKEN`). The `/internal/*/sync` cron endpoints are guarded
by a shared bearer token: each `@router.post` depends on `require_cron_token`
(`app/stocks/endpoints/cron_auth.py`), which requires `Authorization: Bearer
$CRON_SYNC_TOKEN` (constant-time compared) and is **fail-closed** — a `503` when the token is
unset, a `401` on a missing/wrong token. The GitHub sync workflows don't hit this HTTP surface
(they run the sweeps as one-off ECS tasks via `python -m app.sync`, which call the `run_*_sync`
runners directly), so the guard only gates a manual/HTTP trigger. Build
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

To change the DB schema: edit the relevant model (e.g. the shared anchor
`app/stocks/stocks/models.py`, or a slice's `models.py`), then
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
    ├── adapters/           # ── vendor adapters as *_adapter.py (earnings: yfinance + caches; estimates projection;
    │                       #    universe screen + ETF screen/profile: yfinance; index membership: wikipedia)
    ├── stocks/             # ── shared `stocks` anchor slice:
    │   └── models.py            #    StockRecord (the `stocks` table: ticker/name/exchange + trailing YoY growth +
    │                            #    universe facts + in_sp500/in_nasdaq100 flags) + get_or_create_stock, anchor_facts, fill_exchange
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
    ├── ticker/             # ── ticker-card sub-slice (its OWN entities.py; no table/cron —
    │   │                   #    computed per request from live quote + stored consensus + live chain):
    │   ├── entities.py          #    TickerValuation (trailing_pe + forward_peg properties); OptionContract +
    │   │                        #    TickerOptionsMetrics.from_chains (the options-market read)
    │   ├── ports.py             #    OptionChainProvider (expirations + one expiry's chain)
    │   ├── repository.py        #    abstract persistence port (exchange on the stocks anchor)
    │   ├── db_repository.py     #    concrete repo: anchor-level exchange read/fill
    │   ├── use_cases.py         #    GetTickerCard + TickerCard composite (quote/estimates/fundamentals/performance/options/quarterly-earnings ports)
    │   └── schemas.py           #    HTTP response DTO (quote + enrichment + opt-in dividend/performance/metrics/options_metrics; endpoint in endpoints/)
    ├── universe/           # ── universe sub-slice (table-less; screens the ≥$1B US universe onto the stocks anchor AND reads it back):
    │   ├── entities.py          #    ScreenedStock + slugify; read-side shapes (StockSearchCriteria/Result/Page, StockSort/SortDirection, Classifications)
    │   ├── ports.py             #    live-source port (StockScreener)
    │   ├── repository.py        #    abstract persistence ports: UniverseRepository (write) + StockSearchRepository (read)
    │   ├── db_repository.py     #    SqlUniverseRepository (upsert_screen + set_pe_ratios) + SqlStockSearchRepository (search/classifications; screened-only)
    │   ├── use_cases.py         #    SyncUniverse (write: screen + classify + value pe, from quarterly TTM × screen price) + SearchStocks / ListClassifications (read)
    │   └── schemas.py           #    HTTP DTOs for the read endpoints (search page + classifications; endpoints in endpoints/ticker_endpoints.py)
    ├── etfs/               # ── ETF sub-slice (owns its OWN `etfs` table + 2 child tables — an ETF is not a company; screens the top US ETFs, enriches each with its full profile, reads them back, AND serves one fund's detail card):
    │   ├── entities.py          #    ScreenedEtf (AUM/expense) + EtfProfile (category/family/dividend/NAV/description/returns) + EtfHolding + EtfSectorWeight + EtfDetail (quote+facts+profile composite, carries the requested `include` set + best-effort performance) + slugify; read-side shapes (EtfSearchCriteria/Result/Page, EtfSort/SortDirection, EtfCategories)
    │   ├── ports.py             #    live-source ports: EtfScreener (bulk screen, no criteria) + EtfProfileProvider (per-ticker full profile — the screen carries none; raises on a hard read)
    │   ├── repository.py        #    abstract persistence ports: EtfRepository (write: screen upsert + profile enrichment) + EtfSearchRepository (read: search + categories) + EtfLookupRepository (read: membership + stored facts + stored profile)
    │   ├── models.py            #    EtfRecord (`etfs`: AUM/expense/category + profile scalars) + EtfSectorWeightingRecord (`etf_sector_weightings`) + EtfTopHoldingRecord (`etf_top_holdings`) + get_or_create_etf (standalone anchor, not a stocks child)
    │   ├── db_repository.py     #    SqlEtfRepository (upsert_screen additive + profile_refresh_targets/upsert_profile merge-preserving) + SqlEtfSearchRepository (search/categories) + SqlEtfLookupRepository (is_etf/get/get_stored_profile)
    │   ├── use_cases.py         #    SyncEtfs (write — screen+upsert then per-ticker profile enrichment) + SearchEtfs / ListEtfCategories (read) + GetEtfDetail (one fund's card: membership-gated, quote-primary, DB-read profile + live-Yahoo 3y/5y returns overlaid for the performance block, opt-in metrics/dividends/performance)
    │   └── schemas.py           #    HTTP DTOs for the read endpoints (search page + categories menu) + the detail card (base + stored profile enrichment + opt-in EtfMetrics/EtfDividends/EtfPerformance blocks); endpoints in endpoints/etf_endpoints.py
    ├── index_membership/   # ── index-membership sub-slice (table-less; reconciles in_sp500/in_nasdaq100 on the anchor):
    │   ├── entities.py          #    IndexMembershipSnapshot (the two ticker sets, slice-local)
    │   ├── ports.py             #    live-source port (IndexMembershipSource)
    │   ├── repository.py        #    abstract persistence port (+ IndexMembershipSyncCounts)
    │   ├── db_repository.py     #    SqlIndexMembershipRepository: reconcile (mark members / clear drop-outs) onto stocks
    │   └── use_cases.py         #    SyncIndexMembership (per-index plausibility floor)
    ├── endpoints/          # ── HTTP endpoints outside a read slice:
    │   ├── cron_quarterly_earnings_endpoints.py  #  POST /internal/earnings/quarterly/sync
    │   ├── quarterly_earnings_endpoints.py       #  GET /stocks/{symbol}/earnings/quarterly
    │   ├── cron_annual_earnings_endpoints.py     #  POST /internal/earnings/annual/sync
    │   ├── annual_earnings_endpoints.py          #  GET /stocks/{symbol}/earnings/annual
    │   ├── cron_recommendations_endpoints.py     #  POST /internal/recommendations/sync
    │   ├── recommendations_endpoints.py          #  GET /stocks/{symbol}/recommendations
    │   ├── ticker_endpoints.py                   #  GET /stocks/ticker/{symbol} (card) + GET /stocks/ticker (search) + GET /stocks/classifications
    │   ├── etf_endpoints.py                      #  GET /stocks/etfs (top-ETF search/filter/sort) + GET /stocks/etfs/categories (filter menu) + GET /stocks/etf/{ticker} (one fund's card: quote + facts + DB-read profile, 3y/5y returns fetched live for the performance block + opt-in ?include=metrics/dividends/performance)
    │   ├── cron_etf_endpoints.py                 #  POST /internal/etfs/sync (fire-and-forget: screen + profile enrichment)
    │   ├── cron_universe_endpoints.py            #  POST /internal/universe/sync (fire-and-forget)
    │   ├── cron_index_membership_endpoints.py    #  POST /internal/index-membership/sync (fire-and-forget)
    │   └── background_sync.py                    #  shared fire-and-forget helper (202 + per-slice single-flight)
    ├── chart_window.py     # ── edge helper: range preset → time window
    ├── progress.py         # ── shared helper: iter_with_progress logs a cron sweep's % done (CloudWatch)
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
