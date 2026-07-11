"""The SEO / server-rendered content slice.

Public, crawlable HTML pages — one per entity — rendered **server-side** so that
search crawlers *and* AI crawlers (GPTBot, ClaudeBot, PerplexityBot, Google-Extended)
that don't execute JavaScript see real content, which the client-rendered React app
can't give them. These pages are the indexable, citable surface; each links into the
interactive app for the live experience.

Deliberately **DB-only** (no live vendor calls): a page is assembled from facts already
stored on the shared ``stocks`` anchor by the sync crons, so a crawler hitting thousands
of pages never triggers a rate-limited Alpaca/Yahoo round-trip. Freshness stays the
crons' job — the same read-through stance the analysis path takes with its DB-only
context providers.

Layering mirrors the other slices: ``repository`` (port) + ``db_repository`` (SQLAlchemy
read) + ``use_cases`` (orchestration) + ``templates/`` (the presenter, as Jinja2). The
HTTP routes live in ``app/stocks/endpoints/seo_endpoints.py`` like every other slice's
endpoints. See ``README.md`` for the canonical URL scheme and the edge-routing plan.
"""
