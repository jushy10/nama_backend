# FE ticket — migrate to the new ticker/chart endpoints

**Type:** Frontend migration · **Repo:** `nama_frontend` · **Blocked by:** backend PRs #163, #164

## Summary

The backend is reshaping its per-symbol chart/price endpoints. Three changes ship
across two backend PRs and are **breaking** for the frontend:

1. `GET /stocks/{symbol}/resistance-levels` is **removed** (backend PR #163).
2. `GET /stocks/{symbol}/candles` **moves** to `GET /stocks/ticker/{ticker}/candles` (backend PR #164).
3. `GET /stocks/{symbol}/support-levels` **moves** to `GET /stocks/ticker/{ticker}/support-levels` (backend PR #164).
4. `GET /stocks/{symbol}/quote` is **removed** (backend PR #164).

The frontend must stop calling the removed endpoints and repoint the moved ones
before those PRs deploy.

## Endpoint change table

| Old (frontend calls today) | New | Action |
| --- | --- | --- |
| `GET /stocks/{symbol}/resistance-levels` | — | **Remove** all usage |
| `GET /stocks/{symbol}/candles` | `GET /stocks/ticker/{ticker}/candles` | **Repoint** URL only |
| `GET /stocks/{symbol}/support-levels` | `GET /stocks/ticker/{ticker}/support-levels` | **Repoint** URL only |
| `GET /stocks/{symbol}/quote` | — | **Remove**; source live price from the ticker/ETF card (see caveat) |

## Migration details

### 1. Resistance levels — remove
Delete the API client method, any hooks/queries, and the UI that renders resistance
levels (the swing-high overlay / ceiling markers on the chart). There is no
replacement — support levels remain the only horizontal-level endpoint.

### 2 & 3. Candles + support-levels — repoint URL only
**Only the path changes.** Query params (`timeframe`, `range`, `start`, `end`,
and for support-levels `window`, `tolerance`, `max_levels`) and the **response
bodies are identical** — no field renames, so only the request URL needs updating.

```diff
- GET /stocks/{symbol}/candles?timeframe=1Hour&range=5D
+ GET /stocks/ticker/{ticker}/candles?timeframe=1Hour&range=5D

- GET /stocks/{symbol}/support-levels?window=5
+ GET /stocks/ticker/{ticker}/support-levels?window=5
```

The response still carries a `symbol` field (unchanged) — the rename is at the URL
only, to group these under the `/stocks/ticker/{ticker}` resource alongside the
ticker card (`GET /stocks/ticker/{ticker}`).

### 4. Quote — remove ⚠️ (needs a product decision)
`GET /stocks/{symbol}/quote` was the **only lightweight, ~2s-cached** price endpoint,
and it also served **live ETF quotes** (Alpaca serves ETFs too). After removal, live
price is available only from:

- **Stocks:** `GET /stocks/ticker/{ticker}` (the ticker card — `price` / `change` / `change_percent`).
- **ETFs:** `GET /stocks/etf/{ticker}` (the ETF detail card — same fields).

**Both cards are cached ~5 minutes**, so high-frequency price polling (a live-ticking
widget) is no longer possible against the API as-is.

**Decision needed:** confirm the 5-minute-cached card price is acceptable for every
place the FE shows a live/ticking price. If any surface genuinely needs sub-minute
updates, flag it back to backend — the likely fix is a slim `GET /stocks/ticker/{ticker}/quote`
(cheap, short-cache) rather than reviving the old endpoint.

## Acceptance criteria

- [ ] No frontend code path calls `/stocks/{symbol}/resistance-levels`, `/stocks/{symbol}/candles`,
      `/stocks/{symbol}/support-levels`, or `/stocks/{symbol}/quote`.
- [ ] Candlestick chart loads from `/stocks/ticker/{ticker}/candles` (params + rendering unchanged).
- [ ] Support-levels overlay loads from `/stocks/ticker/{ticker}/support-levels`.
- [ ] Resistance-levels UI + client code fully removed (no dead code, no console errors).
- [ ] Every live-price surface sources price from the ticker card (`/stocks/ticker/{ticker}`) or ETF
      card (`/stocks/etf/{ticker}`); the polling-cadence decision above is resolved and recorded.
- [ ] Tests updated (unit + any e2e/MSW mocks pointing at the old paths).

## References

- Backend PR #163 — remove the resistance-levels endpoint
- Backend PR #164 — move candles + support-levels under `/stocks/ticker/{ticker}`, remove quote

> Coordinate the FE merge/deploy with the backend PRs: the moved/removed endpoints
> return 404 as soon as PR #164 ships, so land the FE change in the same window.
