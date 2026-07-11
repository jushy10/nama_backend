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
| ETF | `/etf/{TICKER}` | **live** |
| Sector | `/sector/{slug}` (hyphenated, e.g. `/sector/consumer-electronics`) | **live** |
| Screen landing | `/screen/{slug}` (e.g. `/screen/high-fcf-yield`) | **live** |
| Crawler files | `/robots.txt`, `/sitemap.xml`, `/llms.txt` | **live** |
| Comparison | `/compare/{a}-vs-{b}` | planned |
| Glossary | `/learn/{term}` | planned |

Screens are a curated registry (`SCREENS` in `use_cases.py`): `high-fcf-yield`, `cheapest-pe`,
`highest-revenue-growth`, `largest-companies` — adding one is a new `ScreenDef` entry. ETF pages
reuse the generic entity template (`ticker.html`); sector + screen pages share `listing.html`.
Off-page launch assets (Product Hunt, Reddit, Show HN, directories, outreach) live in
[`LAUNCH_ASSETS.md`](LAUNCH_ASSETS.md).

Sector pages list the top stocks in a sector (linked to their stock pages) and each stock
page links back to its sector — the hub/spoke internal-linking structure that helps crawlers
reach every leaf page. Sector URLs are hyphenated; the stored slug is snake_case.

Canonical/OG URLs point at the **public canonical** origin (`PUBLIC_SITE_ORIGIN`, default
`https://www.namainsights.com` — www, because the CloudFront edge 301-redirects the apex to
www), even though the FastAPI backend is the rendering origin.

## Edge routing (in Terraform)

The pages are served from the **main domain** so they share the site's authority (not a
separate API host). This is wired in the frontend CloudFront distribution: the
`static-site-cloudfront` module takes an optional `backend_origin_domain_name` +
`backend_path_patterns`, and `infra/environments/dev/main.tf` routes these paths to the
app origin (`api.namainsights.com`) while everything else stays the S3 SPA:

```
/stock/*      → app origin
/sitemap.xml  → app origin
/robots.txt   → app origin
/llms.txt     → app origin
(default)     → SPA (S3)
```

No path rewrite is needed — the app serves the same paths (the API Gateway `$default`
route passes them through). The apex→www canonical redirect applies to these paths too, so
`PUBLIC_SITE_ORIGIN` is set to the **www** host. The distribution-level SPA 404→index.html
rewrite also applies to the app origin, so an *unknown* `/stock/*` renders the SPA shell
rather than a hard 404 — harmless, since only real (screened) tickers are ever advertised
in the sitemap. Adding `/etf/*`, `/sector/*`, etc. later is one line in `backend_path_patterns`.

Ship it with `terraform apply` (or merge to main — the infra workflow applies), then submit
`/sitemap.xml` in Google Search Console + Bing Webmaster.
