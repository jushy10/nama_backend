"""HTTP API for the SEO / server-rendered content pages.

``GET /stock/{ticker}`` — a public, crawlable HTML page for one stock, rendered
server-side from **DB-only** facts (the shared ``stocks`` anchor) so search *and* AI
crawlers that don't run JavaScript see real content. The React app stays the live,
interactive experience; this is the indexable, citable surface that funnels into it.

Why a *singular* ``/stock/`` prefix rather than ``/stocks/{ticker}``: the entire JSON
API lives under ``/stocks/`` (plural), where a bare ``/stocks/{ticker}`` HTML route would
shadow literals like ``/stocks/etfs`` / ``/stocks/classifications``. A distinct top-level
prefix keeps the content surface collision-free and lets the edge (CloudFront) route
``/stock/*`` to this origin with no path rewrite. See ``app/stocks/seo/README.md``.

Controller + presenter + wiring, the composition-root way. The presenter is split between
the small formatting helpers here and the Jinja2 template
(``app/stocks/seo/templates/ticker.html``); the use case stays framework-free.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.stocks.seo.db_repository import SqlSeoReadRepository
from app.stocks.seo.repository import StockPageRef, TickerPageFacts
from app.stocks.seo.use_cases import GetSitemap, GetTickerStockPage, TickerStockPage

router = APIRouter(tags=["seo"])

# Templates live in the slice, next to its use case — the presenter half that's HTML.
_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "seo" / "templates")
)

# The public origin the pages are served from (canonical/OG URLs point here even though
# the backend is the origin behind the edge). Env-driven so prod and local differ without
# a code change, read only here in the wiring like every other secret/config. Defaults to
# the *canonical* host — www, not the apex, which the CloudFront edge 301-redirects to www
# (so a canonical/sitemap URL on the apex would needlessly bounce). Prod sets
# PUBLIC_SITE_ORIGIN explicitly (see infra/environments/dev/main.tf).
_DEFAULT_SITE_ORIGIN = "https://www.namainsights.com"


def _site_origin() -> str:
    return os.environ.get("PUBLIC_SITE_ORIGIN", _DEFAULT_SITE_ORIGIN).rstrip("/")


def get_ticker_stock_page_use_case(
    db: Session = Depends(get_db),
) -> GetTickerStockPage:
    # Pure DB read over the shared anchor — no vendor, no key — so it's always
    # constructable (the pages must render even when every upstream key is absent).
    return GetTickerStockPage(SqlSeoReadRepository(db))


# --- Presenter helpers: stored facts -> display strings ----------------------------------


def _humanize(slug: str | None) -> str | None:
    """A snake_case classification slug -> a human label (``consumer_electronics`` ->
    ``Consumer Electronics``)."""
    if not slug:
        return None
    return slug.replace("_", " ").replace("-", " ").title()


def _fmt_cap(value: float | None) -> str | None:
    if value is None:
        return None
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(value) >= div:
            return f"${value / div:.2f}{unit}"
    return f"${value:,.0f}"


def _fmt_ratio(value: float | None) -> str | None:
    return None if value is None else f"{value:.1f}"


def _fmt_pct(value: float | None, *, signed: bool = False) -> str | None:
    if value is None:
        return None
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def _join_and(parts: list[str]) -> str:
    """``[a, b, c]`` -> ``"a, b and c"`` for a natural summary sentence."""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _summary(name: str, ticker: str, facts: TickerPageFacts) -> str:
    """A short, unique, fact-framed paragraph — placeholder for the richer Bedrock summary
    (task 6), but already unique per page (thin/duplicate text is what gets suppressed)."""
    sector = _humanize(facts.sector)
    industry = _humanize(facts.industry)
    if sector and industry:
        lead = f"{name} ({ticker}) is a {sector} company in the {industry} industry."
    elif sector:
        lead = f"{name} ({ticker}) is a {sector} company."
    else:
        lead = f"{name} ({ticker}) is a publicly traded company."

    financials: list[str] = []
    if facts.market_cap is not None:
        financials.append(f"a market capitalization of {_fmt_cap(facts.market_cap)}")
    if facts.pe_ratio is not None:
        financials.append(f"a trailing price-to-earnings ratio of {facts.pe_ratio:.1f}")
    if facts.fcf_yield is not None:
        financials.append(f"a free-cash-flow yield of {facts.fcf_yield:.1f}%")
    sentence = f" It has {_join_and(financials)}." if financials else ""

    growth: list[str] = []
    if facts.revenue_growth_yoy is not None:
        verb = "grew" if facts.revenue_growth_yoy >= 0 else "declined"
        growth.append(f"revenue {verb} {abs(facts.revenue_growth_yoy):.1f}%")
    if facts.fcf_growth_yoy is not None:
        verb = "grew" if facts.fcf_growth_yoy >= 0 else "fell"
        growth.append(f"free cash flow per share {verb} {abs(facts.fcf_growth_yoy):.1f}%")
    growth_sentence = (
        f" Over the most recent reported fiscal year, {_join_and(growth)}."
        if growth
        else ""
    )
    return lead + sentence + growth_sentence


def _description(name: str, ticker: str, facts: TickerPageFacts) -> str:
    """The <=~160-char meta description: lead with the name/sector and the headline
    numbers (the snippet an engine extracts), then the value prop."""
    sector = _humanize(facts.sector)
    lead = f"{name} ({ticker})" + (f" — {sector}" if sector else "")
    parts: list[str] = []
    if facts.market_cap is not None:
        parts.append(f"market cap {_fmt_cap(facts.market_cap)}")
    if facts.pe_ratio is not None:
        parts.append(f"P/E {facts.pe_ratio:.1f}")
    if facts.fcf_yield is not None:
        parts.append(f"FCF yield {facts.fcf_yield:.1f}%")
    stats = f" {', '.join(parts)}." if parts else "."
    desc = (
        f"{lead}.{stats} Live price, earnings, analyst targets and cash-flow "
        "metrics on Nama Insights."
    )
    return desc if len(desc) <= 160 else desc[:159].rstrip(" ,.") + "…"


def _metrics(facts: TickerPageFacts) -> list[dict[str, str]]:
    """The visible metrics table — every row present (missing values as ``—``) so the page
    reads consistently across stocks."""
    rows = [
        ("Market cap", _fmt_cap(facts.market_cap)),
        ("Trailing P/E", _fmt_ratio(facts.pe_ratio)),
        ("FCF yield", _fmt_pct(facts.fcf_yield)),
        ("Revenue growth (YoY)", _fmt_pct(facts.revenue_growth_yoy, signed=True)),
        ("EPS growth (YoY)", _fmt_pct(facts.eps_growth_yoy, signed=True)),
        ("FCF/share growth (YoY)", _fmt_pct(facts.fcf_growth_yoy, signed=True)),
        ("Exchange", facts.exchange),
        ("Sector", _humanize(facts.sector)),
        ("Industry", _humanize(facts.industry)),
    ]
    return [{"label": label, "value": value or "—"} for label, value in rows]


def _jsonld(name: str, ticker: str, facts: TickerPageFacts, canonical: str, site: str) -> str:
    """schema.org JSON-LD: a Corporation node (name + ticker for entity clarity) and a
    breadcrumb trail. Serialized ASCII with ``<`` escaped so page data can't break out of
    the <script> block."""
    corporation: dict = {
        "@context": "https://schema.org",
        "@type": "Corporation",
        "name": name,
        "tickerSymbol": ticker,
        "url": canonical,
    }
    industry = _humanize(facts.industry)
    if industry:
        corporation["industry"] = industry
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Nama Insights", "item": site},
            {"@type": "ListItem", "position": 2, "name": "Stocks", "item": f"{site}/search"},
            {"@type": "ListItem", "position": 3, "name": f"{name} ({ticker})", "item": canonical},
        ],
    }
    return json.dumps([corporation, breadcrumbs]).replace("<", "\\u003c")


def _render(request: Request, page: TickerStockPage) -> Response:
    """Build the template context from the page view and render it."""
    facts = page.facts
    assert facts is not None  # guarded by the endpoint's has_data check before calling
    site = _site_origin()
    ticker = page.ticker
    name = page.display_name
    canonical = f"{site}/stock/{ticker}"

    subtitle_bits = [
        b for b in (_humanize(facts.sector), _humanize(facts.industry), facts.exchange) if b
    ]
    chips: list[str] = []
    if facts.in_sp500:
        chips.append("S&P 500")
    if facts.in_nasdaq100:
        chips.append("Nasdaq-100")

    context = {
        "title": f"{name} ({ticker}) Stock — Key Metrics & Valuation | Nama Insights",
        "description": _description(name, ticker, facts),
        "canonical": canonical,
        # Screened stocks are index-worthy; a merely-incidental row is served but kept out
        # of the index so a thin page never dilutes the site.
        "robots": "index,follow" if page.indexable else "noindex,follow",
        "site": site,
        "app_url": f"{site}/search",
        "name": name,
        "ticker": ticker,
        "subtitle": " · ".join(subtitle_bits),
        "chips": chips,
        "summary": _summary(name, ticker, facts),
        "metrics": _metrics(facts),
        "jsonld": _jsonld(name, ticker, facts, canonical, site),
        "year": datetime.now(timezone.utc).year,
    }
    response = _TEMPLATES.TemplateResponse(
        request=request, name="ticker.html", context=context
    )
    # Slow-moving DB facts (refreshed out of band by the syncs) — cache generously so a
    # CDN/crawler burst collapses onto one render.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@router.get("/stock/{ticker}")
def stock_page_endpoint(
    ticker: str,
    request: Request,
    use_case: GetTickerStockPage = Depends(get_ticker_stock_page_use_case),
):
    """A single stock's server-rendered content page. A malformed ticker is a 400; a symbol
    we hold no data for is a 404 (no soft, contentless 200s in the index)."""
    try:
        page = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not page.has_data:
        raise HTTPException(404, f"No data available for {page.ticker}.")
    return _render(request, page)


# --- Crawler files: robots.txt, llms.txt, sitemap.xml ------------------------------------
#
# These belong at the site root, so at the edge (CloudFront) they route to this origin
# alongside /stock/*. All three are served from here so the sitemap can be generated from
# live DB state (the screened universe) rather than shipped as a stale static file.


# A permissive robots that *explicitly welcomes* the AI answer/training crawlers (blocking
# them would forfeit AI-search visibility, the whole point of the AI-SEO push) and points
# every bot at the sitemap. ``{sitemap}`` is filled with the absolute sitemap URL.
_ROBOTS_TEMPLATE = """\
# Nama Insights — robots.txt
# Search and AI crawlers are welcome.

User-agent: *
Allow: /

# AI answer engines / training crawlers — explicitly allowed so Nama can be surfaced
# and cited in AI-generated answers.
User-agent: GPTBot
Allow: /

User-agent: OAI-SearchBot
Allow: /

User-agent: ChatGPT-User
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Google-Extended
Allow: /

User-agent: Applebot-Extended
Allow: /

User-agent: CCBot
Allow: /

Sitemap: {sitemap}
"""


# The llms.txt convention: a short Markdown map that points AI crawlers at what matters.
_LLMS_TXT = """\
# Nama Insights

> Free stock and ETF research for US-listed companies: live quotes, fundamentals,
> free-cash-flow metrics (FCF yield, P/FCF), earnings history, analyst coverage, and
> AI-generated analysis. Market data is refreshed daily.

## Stock pages
Per-ticker pages with market cap, trailing P/E, free-cash-flow yield, year-over-year
growth, and sector/industry classification — e.g. /stock/AAPL, /stock/NVDA, /stock/MSFT.
The full list is in /sitemap.xml.

## Tools
- /search — search and filter the >=$1B US stock universe
- /screener — stock screener
- /etf-screener — ETF screener
- /sectors — sector overview
- /heatmap — market heat map

## Notes
Figures are the most recently synced values and are for informational purposes only —
not investment advice.
"""


def get_sitemap_use_case(db: Session = Depends(get_db)) -> GetSitemap:
    # Pure DB read over the screened universe — no vendor, no key.
    return GetSitemap(SqlSeoReadRepository(db))


def _sitemap_xml(refs: tuple[StockPageRef, ...], site: str) -> str:
    """Build the urlset XML: the homepage plus one <url> per index-worthy stock page,
    each carrying its ``lastmod`` when known. ``loc`` values are XML-escaped (tickers are
    constrained, but escaping the origin-joined URL is correct and cheap)."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{escape(site + '/')}</loc></url>",
    ]
    for ref in refs:
        loc = escape(f"{site}/stock/{ref.ticker}")
        if ref.last_modified is not None:
            parts.append(
                f"  <url><loc>{loc}</loc>"
                f"<lastmod>{ref.last_modified.isoformat()}</lastmod></url>"
            )
        else:
            parts.append(f"  <url><loc>{loc}</loc></url>")
    parts.append("</urlset>")
    return "\n".join(parts)


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt() -> PlainTextResponse:
    body = _ROBOTS_TEMPLATE.format(sitemap=f"{_site_origin()}/sitemap.xml")
    # Rarely changes — cache a day.
    return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt() -> PlainTextResponse:
    return PlainTextResponse(_LLMS_TXT, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/sitemap.xml")
def sitemap_xml(use_case: GetSitemap = Depends(get_sitemap_use_case)) -> Response:
    """The sitemap of every index-worthy stock page, generated live from the screened
    universe. Cached an hour — the universe is slow-moving and a crawler burst should
    collapse onto one render."""
    xml = _sitemap_xml(use_case.execute(), _site_origin())
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )
