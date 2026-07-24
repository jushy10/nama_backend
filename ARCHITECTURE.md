# ARCHITECTURE.md — the target slice architecture

This document defines the **canonical shape of a feature slice**, generalized from
`app/domains/research/agent/` — the reference implementation, refactored to this shape
deliberately (PRs #280/#282/#283). Use it two ways:

1. **Building a new slice** — copy this layout.
2. **Refactoring an existing slice** — converge it file by file (checklist at the bottom).

`CLAUDE.md` describes the system *as it largely is today*; this describes where slices
are *headed*. Where the two disagree (noted at the end), this document wins for new and
refactored code, and `CLAUDE.md` gets updated as slices converge.

---

## The canonical slice

```
app/domains/<domain>/<slice>/
├── entities.py          # frozen dataclasses + enums — the domain vocabulary (stdlib only)
├── errors.py            # the slice's error hierarchy — self-messaged, framework-free
├── use_cases.py         # one class per action, single public method run(...)
├── repository.py        # abstract persistence port (ABC) — plain domain name
├── db_repository.py     # the SQLAlchemy implementation — Db<Name>
├── models.py            # SQLAlchemy table models + module-level query helpers
├── interfaces/          # ONLY ports implemented by external vendor adapters
│   └── <concern>_adapter.py
├── wiring.py            # framework-free composition: build_<action>(db, ...) factories
├── api_schemas.py       # pydantic request/response DTOs + from_* presenters
└── docs/                # optional: mermaid flow/layer diagrams + README
```

Two things deliberately live **outside** the slice:

- **HTTP endpoints** → `app/endpoints/<slice>_endpoints.py`. The slice carries no
  FastAPI code at all.
- **Vendor adapter implementations** → `app/adapters/<vendor>/<concern>_adapter_impl.py`.
  The slice declares the port; the vendor folder implements it.

Not every slice needs every file. A table-less slice has no `models.py` /
`repository.py` / `db_repository.py`; a slice with no vendor seam has no `interfaces/`.
Never add a file to fill out the template.

### Reference map (concept → the exemplar file)

| Concept | Exemplar |
|---|---|
| Entities incl. serializable payloads | `app/domains/research/agent/entities.py` |
| Slice error hierarchy | `app/domains/research/agent/errors.py` |
| Use case (`run`) | `app/domains/research/agent/use_cases.py` (`RunResearchUseCase`) |
| Abstract repository | `app/domains/research/agent/repository.py` (`AgentRecipeRepository`) |
| DB repository | `app/domains/research/agent/db_repository.py` (`DbAgentRecipeRepository`) |
| Table model + query helpers | `app/domains/research/agent/models.py` |
| Vendor port | `app/domains/research/agent/interfaces/conversation_model_adapter.py` |
| Vendor adapter impl | `app/adapters/bedrock/conversation_model_adapter_impl.py` |
| In-slice polymorphism (base beside impls) | `tool.py` (base) + `tools.py` (impls) |
| Framework-free wiring | `app/domains/research/agent/wiring.py` |
| DTOs + presenter | `app/domains/research/agent/api_schemas.py` |
| Thin endpoint + Depends shim | `app/endpoints/research_endpoints.py` |
| Central error translation | `app/endpoints/error_handlers.py` |

---

## The dependency rule (unchanged, sharpened vocabulary)

Dependencies point inward. No inner layer imports an outer one; no layer imports a
vendor except its own adapter.

| Layer | File(s) | May import | Must NOT import |
|---|---|---|---|
| Entities | `entities.py` | stdlib only (+ shared-kernel entities) | everything else |
| Errors | `errors.py` | stdlib only | framework, HTTP |
| Ports | `repository.py`, `interfaces/`, base-class files (`tool.py`) | entities, stdlib `abc` | use cases, impls, vendors |
| Use cases | `use_cases.py` | entities, errors, ports (+ other slices' use cases) | SQLAlchemy, FastAPI, pydantic, vendor SDKs |
| Persistence impl | `db_repository.py`, `models.py` | entities, `repository.py`, SQLAlchemy, `app.db` | use cases, FastAPI, vendors |
| Vendor adapters | `app/adapters/<vendor>/` | entities, the slice port, the vendor SDK | other adapters, use cases, FastAPI |
| Wiring | `wiring.py` | everything in the slice + adapters + env | **FastAPI** |
| DTOs | `api_schemas.py` | pydantic + entities (read-only, for `from_*`) | use cases, adapters |
| Endpoints | `app/endpoints/` | everything | — |

---

## File-by-file rules

### `entities.py` — the domain vocabulary

Frozen `@dataclass`es and `Enum`s, stdlib imports only. Domain rules that are facts
about one object live here as `@property`s / small methods — computed, never stored.

**Payload entities.** When a component produces output for a *serialized audience* —
an LLM reading a tool result, a report body, anything that ultimately becomes
JSON/text — model that output as dedicated frozen dataclasses too (the agent's
`ToolResult` family: `StockScreenResult`, `MarketSentimentResult`, `ToolMessage`,
`ToolError`). Rules:

- The payload class is **the deliberate selection** of what the audience sees. Never
  `asdict()` a *domain* entity into a payload — that couples the output contract to an
  object that changes for other reasons. A dedicated class makes every contract change
  a reviewable diff.
- **Serialize at exactly one choke point** in the consumer (the agent loop's `_step`:
  `json.dumps(asdict(payload), default=str)`), never inside the producers.
- **No prose as a data channel.** An f-string return value that smuggles fields
  (`f"P/E {row.pe}"`) is a bug pattern — put the field on a payload class. f-strings
  are fine *inside* genuine human/model messages (`ToolMessage.message`, error text).
- Distinguish outcomes structurally, not by phrasing: an empty result is a
  `ToolMessage`, a failure is a `ToolError` with a **stable error code** plus data
  (`error="unknown_tool"`, `available_tools=[...]`) — so the consumer branches on
  fields, not on parsing sentences.

### `errors.py` — the slice's error hierarchy

- One base (`<Slice>Error`), intermediate bases where a *group* maps to one HTTP status
  (`AgentNotConfigured` → 503).
- **Errors carry their own message**: the constructor takes the interesting values and
  builds the message (`MissingAgentRecipe(agent_name)`). Raise sites never compose
  message strings.
- Framework-free. Translation to HTTP happens once, centrally, in
  `app/endpoints/error_handlers.py` (type → status table). Endpoints do **not**
  try/except domain errors; truly slice-generic errors (`StockNotFound`) stay in the
  shared kernel `app/domains/shared/exceptions.py`.

### `use_cases.py` — one class per action

```python
class RunResearchUseCase:
    def __init__(self, model: ConversationModelAdapter, tools: Sequence[Tool],
                 recipe_repo: AgentRecipeRepository, ...) -> None: ...
    def run(self, question: str, client_id: str | None = None) -> ResearchResult: ...
```

- Class named for the action (verb phrase); add the `UseCase` suffix when the bare verb
  phrase would read ambiguously as a class name (`RunResearchUseCase`).
- **The single public method is `run(...)`** (not `execute`). It normalizes input,
  raises the slice's errors, pulls data through ports, assembles entities, returns an
  entity.
- Constructor-injected with **ports only** — never a concrete `Db*`/`*AdapterImpl`.
- Cross-use-case calls (and any call whose signature lives elsewhere) use **explicit
  keyword arguments** — never `**asdict(...)`/`**dict` splats that silently mirror
  another signature. Signature drift must fail loudly at the call site.
- Untrusted input (LLM tool arguments, free-text query params) is coerced at the
  boundary with degrade-to-default helpers — a stray value becomes its default, never
  an exception out of the boundary component.

### `repository.py` / `db_repository.py` — persistence as a pair

- `repository.py`: the abstract port. Plain domain name, **no** `Adapter` suffix, no
  vendor/tech in the name: `AgentRecipeRepository`, `QuotaRepository`. Methods phrased
  in domain terms, returning entities.
- `db_repository.py`: the SQLAlchemy implementation, named `Db<Name>`
  (`DbAgentRecipeRepository`). Maps records → entities; composes `models.py`'s query
  helpers; owns the transaction.
- This **replaces** the older `interfaces/<x>_repository_adapter.py` +
  `<x>_repository_adapter_impl.py` convention. Repository = persistence the slice
  owns; Adapter = a third party the slice talks to. Different words on purpose.

### `models.py` — tables + query helpers

SQLAlchemy models named `<Concept>Record` (`AgentRecipeRecord`), plus module-level
query helper functions beside them (`recipe_by_name(session, name)`). The helpers keep
`db_repository.py` about mapping and transactions, not query text. Schema is
Alembic-owned — models change only with a migration.

### `interfaces/` — vendor ports ONLY

`interfaces/` holds exclusively the ABCs implemented **outside the slice** by a vendor
adapter in `app/adapters/` — the true dependency-inversion seams
(`ConversationModelAdapter` ← `app/adapters/bedrock/`). One port per file, package
`__init__` re-exports.

It is **not** a dumping ground for every ABC:

- Persistence abstraction → `repository.py` (above).
- In-slice polymorphism → abstract-beside-concrete files (below).

If nothing in `app/adapters/` implements it, it doesn't belong in `interfaces/`.

### Abstract-beside-concrete — in-slice polymorphism

When a slice defines a family of interchangeable things it *itself* implements (the
agent's tools), pair a singular base-class module with a plural implementations module:

- `tool.py` — the base class (`Tool`: abstract `spec` + `run(arguments) -> ToolResult`).
- `tools.py` — the implementations (`SearchStocksTool`, `MarketSentimentTool`).

Consumers (`use_cases.py`) import the base from the singular module only — they never
touch the implementations or their transitive imports. Constant class-level attributes
satisfy abstract properties as **plain class attributes** (`spec = _SEARCH_STOCKS_SPEC`),
not boilerplate `@property` methods; reserve a property for a value that genuinely
depends on instance state.

### `wiring.py` — the slice's composition root, framework-free

- Exposes `build_<action>(db, ...) -> <UseCase>` — all construction knowledge
  (which adapter, which env var, which registry) lives here.
- `@lru_cache` for process-wide singletons (clients, keyless providers).
- Reads env/config; raises the slice's *misconfiguration* errors (`BedrockNotInstalled`,
  `MissingAgentRecipe`) rather than HTTP errors.
- **No FastAPI import.** The endpoint module owns the framework shim.

### `api_schemas.py` — DTOs + presenters

Pydantic request/response models (the file is `api_schemas.py`, not `schemas.py` — it
names the audience). The entity→DTO mapping lives here as a classmethod presenter
(`ResearchResponse.from_result(result)`), keeping the endpoint one expression. JSON
naming/aliases belong here, never on entities.

### `app/endpoints/<slice>_endpoints.py` — thin controller

Three tiny jobs, nothing else:

```python
def get_run_research(db: Session = Depends(get_db)) -> wiring.RunResearchUseCase:
    return wiring.build_run_research(db, quota=research_generation_quota(db))

@router.post("/agents/research", response_model=ResearchResponse)
def run_research_endpoint(body: ResearchRequest,
                          use_case=Depends(get_run_research)) -> ResearchResponse:
    return ResearchResponse.from_result(use_case.run(body.question, ...))
```

- A `get_<action>` **Depends shim** over the slice's `build_<action>` — it exists for
  the db lifecycle and the `app.dependency_overrides` test seam, nothing more.
- The route: unpack request → `use_case.run(...)` → `Response.from_result(...)`.
- No try/except for domain errors — the central handlers translate them. Rate limits
  and other edge policy attach here.

### `docs/` — optional diagrams

For a slice with non-obvious control flow, a `docs/` folder with mermaid sources
(`.mmd`), rendered PNGs, and a README with the regen command. Keep diagrams in sync
when renaming — they're part of the slice.

---

## Naming conventions (summary)

| Thing | File | Class / callable |
|---|---|---|
| Entity / payload | `entities.py` | `AgentRecipe`, `ToolError` |
| Error | `errors.py` | `<Slice>Error` base; specific `EmptyQuestion` |
| Use case | `use_cases.py` | verb phrase (+`UseCase` if ambiguous); method **`run`** |
| Persistence port | `repository.py` | `<Concept>Repository` |
| Persistence impl | `db_repository.py` | `Db<Concept>Repository` |
| Table model | `models.py` | `<Concept>Record`; helpers `snake_case` functions |
| Vendor port | `interfaces/<concern>_adapter.py` | `<Concern>Adapter` |
| Vendor impl | `app/adapters/<vendor>/<concern>_adapter_impl.py` | `<Concern>AdapterImpl` |
| In-slice base | `<thing>.py` (singular) | `Thing` |
| In-slice impls | `<thing>s.py` (plural) | `<X>Thing`... |
| DTO | `api_schemas.py` | `<X>Request` / `<X>Response`, presenter `from_<entity>` |
| Wiring factory | `wiring.py` | `build_<action>`, `get_<singleton>` |
| Endpoint shim | `app/endpoints/<slice>_endpoints.py` | `get_<action>`, `<action>_endpoint` |

Tests mirror the slice exactly under `tests/<domain>/<slice>/`:
`test_use_cases.py` (fakes implementing the ports — scripted fakes for conversational
seams), `test_db_repository.py` (in-memory SQLite), `test_<things>.py` for in-slice
families, and `tests/endpoints/test_<slice>_endpoints.py` (TestClient +
`dependency_overrides` on the `get_<action>` shim). Everything runs offline; a test
that needs the network means the seam is in the wrong place.

---

## Refactoring an existing slice — the checklist

Work through in order; each step is independently landable (small PRs, like
#280 → #282 → #283):

1. **Persistence pair.** Move the abstract repo out of `interfaces/` into
   `repository.py` as `<Concept>Repository`; rename the impl file to
   `db_repository.py` and the class to `Db<Concept>Repository`. Rename the test to
   `test_db_repository.py`.
2. **Trim `interfaces/`.** Keep only ports implemented in `app/adapters/`. Move
   in-slice bases to singular-file-beside-plural-file pairs. Delete the package if it
   empties.
3. **`run`, not `execute`.** Rename the use case's public method; update endpoint +
   tests.
4. **Explicit kwargs.** Replace any `**asdict(...)`/`**dict` splat into another
   signature with named arguments; delete mirror dataclasses that only existed to be
   splatted.
5. **Class attributes over constant properties.** Any `@property` returning a module
   constant becomes `attr = CONSTANT`.
6. **Structured payloads.** Any component returning prose-that-carries-data gets
   payload entities + one serialization choke point; errors become typed payloads with
   stable codes.
7. **Slice `errors.py` + central handlers.** Self-messaged error classes; add the
   type→status rows to `error_handlers.py`; delete per-endpoint try/except
   translation.
8. **`api_schemas.py` + framework-free `wiring.py`.** Rename `schemas.py`; move
   construction into `build_<action>` in the slice; leave a `get_<action>` Depends
   shim in the endpoint module.
9. **Docs + `CLAUDE.md`.** Update any slice diagrams and the affected `CLAUDE.md`
   sections in the same PR as the rename they describe.

After each step: full `pytest` green before moving on.

---

## Known divergences from `CLAUDE.md` (as of 2026-07-23)

`CLAUDE.md` still documents the pre-refactor conventions in places. Until slices
converge and it's updated, read these as superseded **for refactored/new slices**:

- "use cases expose a single `execute(...)`" → the method is **`run(...)`**.
- "every abstraction is a plain-named `*Adapter` ABC … under `interfaces/`" → true
  only for **vendor** ports; persistence is the `repository.py`/`db_repository.py`
  pair, in-slice families are abstract-beside-concrete pairs.
- "Persistence … `NewsRepositoryAdapter` / `NewsRepositoryAdapterImpl` in
  `<x>_repository_adapter_impl.py`" → `<Concept>Repository` / `Db<Concept>Repository`
  in `repository.py` / `db_repository.py`.
- Slice DTO file `schemas.py` → `api_schemas.py`.
- "Presenter — `_present_*` functions in the endpoint module" and "DTOs must not
  import entities" → the presenter lives **on the DTO** as a `from_<entity>(...)`
  classmethod in `api_schemas.py`, which imports entities read-only for that mapping.

## Convergence status

Patterns that recur across converged slices (all implied by the rules above — restated
once here so status entries never repeat them):

- A table-less slice skips the persistence pair; a slice returning non-JSON output
  (raw bytes) skips `api_schemas.py`. Apply only the applicable steps.
- No converged slice has needed its own `errors.py` yet: they raise `ValueError`
  (kept as an inline 400 in the endpoint — it is not a domain error) and the
  shared-kernel errors, which the central handlers translate.
- A composite result that a DTO presents lives in `entities.py`, never `use_cases.py`
  (so `api_schemas.py` imports entities only).
- Cross-endpoint singletons (the Alpaca / price-router providers with their
  missing-key 503 gates) stay in `app/endpoints/wiring.py`; a slice's `build_<action>`
  takes them as **parameters** and the endpoint shim resolves them via `Depends`.
- Slice wiring may reuse another slice's `build_*` factory (wiring may import wiring).
- Support types a repository port needs (`RefreshTarget`, read models like
  `StoredTickerFacts`) live beside the port in `repository.py`.

| Slice | Status |
|---|---|
| `research/agent` | converged (the exemplar) |
| `coverage/news`, `coverage/recommendations` | converged |
| `profile/logo` | converged |
| `macro/yields`, `macro/sentiment` | converged |
| `markets/boards`, `markets/heatmap` | converged |
| `pricing/charts`, `pricing/options`, `pricing/ticker` | converged |
| `research/rate_limit_quota` | partial — persistence pair + `models.py` helpers done; `run` rename pending |
| all other slices | pending |

When a slice converges, flip or add its **one-line** row here; add a bullet above only
if a genuinely new repo-wide pattern emerged. Slice-specific detail belongs in the
slice's PR description and `CLAUDE.md`'s slice section, not this document.
