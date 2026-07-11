# SEO / server-rendered content pages

Public, crawlable HTML pages — one per entity — that make nama visible to search engines
**and** AI answer engines. The React app (`nama_frontend`) is a client-rendered SPA: the
served HTML is an empty `<div id="root">`, so crawlers that don't execute JavaScript —
which is **most AI crawlers** (GPTBot, ClaudeBot, PerplexityBot, Google-Extended) — see
nothing. These pages are the fix: real HTML, rendered server-side, with structured data,
that funnels visitors into the interactive app.

## Design principles

- **DB-only, never live.** A page is a projection of facts already on the shared `stocks`
  anchor (written by the sync crons). A crawler hitting thousands of pages triggers **zero**
  rate-limited Alpaca/Yahoo calls — one indexed DB read each. Freshness stays the crons'
  job (same stance as the analysis path's DB-only context providers).
- **No thin pages in the index.** Only *screened* stocks (the universe sync filled
  `market_cap`) are `index,follow`; a merely-incidental ticker-only row renders but is
  `noindex,follow`; an unknown symbol is a `404` (no soft, contentless 200s).
- **Structured data first.** Every page carries schema.org JSON-LD (Corporation +
  BreadcrumbList today) — the clean facts search rich-results and AI engines lift and cite.

## Canonical URL scheme

Content pages use **singular, top-level prefixes**, deliberately distinct from the
`/stocks/` (plural) JSON API — a bare `/stocks/{ticker}` HTML route would shadow API
literals like `/stocks/etfs` and `/stocks/classifications`.

| Page | URL | Status |
|------|-----|--------|
| Stock | `/stock/{TICKER}` | **live** |
| ETF | `/etf/{TICKER}` | planned |
| Sector | `/sector/{slug}` | planned |
| Screen landing | `/screen/{slug}` (e.g. `/screen/high-fcf-yield`) | planned |
| Comparison | `/compare/{a}-vs-{b}` | planned |
| Glossary | `/learn/{term}` | planned |
| Crawler files | `/robots.txt`, `/sitemap.xml`, `/llms.txt` | planned |

Canonical/OG URLs point at the **public** origin (`PUBLIC_SITE_ORIGIN`, default
`https://namainsights.com`), even though the FastAPI backend is the rendering origin.

## Edge routing (deploy step — not code)

The pages must be served from the **main domain** so they share the site's authority (not
a separate API host). At CloudFront, route these path patterns to the backend origin;
everything else continues to the SPA (S3):

```
/stock/*      → backend      /sitemap.xml  → backend
/etf/*        → backend      /robots.txt   → backend
/sector/*     → backend      /llms.txt     → backend
/screen/*     → backend
/compare/*    → backend      (default)     → SPA (S3)
/learn/*      → backend
```

No path rewrite is needed — the backend serves the same paths. The JSON API stays on its
own `api.` subdomain, untouched.
