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
from app.stocks.seo.seo_read_repository_adapter_impl import SeoReadRepositoryAdapterImpl
from app.stocks.seo.interfaces import CongressPageTrade, EtfPageFacts, TickerPageFacts
from app.stocks.seo.use_cases import (
    CongressBoardPage,
    EtfPage,
    GetCongressBoardPage,
    GetEtfPage,
    GetScreenPage,
    GetSectorPage,
    GetSitemap,
    GetTickerStockPage,
    ScreenPage,
    SectorPage,
    SitemapData,
    TickerStockPage,
)

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
    return GetTickerStockPage(SeoReadRepositoryAdapterImpl(db))


# --- Presenter helpers: stored facts -> display strings ----------------------------------


def _humanize(slug: str | None) -> str | None:
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
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _fmt_date(value) -> str:
    if value is None:
        return "—"
    return f"{value:%b} {value.day}, {value.year}"


# Normalized transaction action -> (display label, CSS direction class). ``Purchase`` reads as
# "Buy", ``Sale`` as "Sell"; everything else is a neutral "Exchange"/"Other".
_CONGRESS_DIR = {
    "Purchase": ("Buy", "buy"),
    "Sale": ("Sell", "sell"),
    "Exchange": ("Exchange", "other"),
    "Other": ("Other", "other"),
}


def _congress_rows(
    trades: tuple[CongressPageTrade, ...], site: str
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for trade in trades:
        label, css = _CONGRESS_DIR.get(trade.tx_type, ("Other", "other"))
        rows.append(
            {
                "member": trade.member,
                "chamber": trade.chamber,
                "ticker": trade.ticker,
                "url": f"{site}/stock/{trade.ticker}",
                "dir_label": label,
                "dir_class": css,
                "amount": trade.amount_range or "—",
                "traded": _fmt_date(trade.transaction_date),
                "disclosed": _fmt_date(trade.disclosure_date),
            }
        )
    return rows


def _summary(name: str, ticker: str, facts: TickerPageFacts) -> str:
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
        # Internal link to the stock's sector page — the hub/spoke structure that helps
        # crawlers reach every page and spreads authority. Null when unclassified.
        "sector_url": (
            f"{site}/sector/{facts.sector.replace('_', '-')}" if facts.sector else None
        ),
        "sector_label": _humanize(facts.sector),
        # The stock's recent Congressional trades (empty -> the section is hidden), plus the link
        # to the market-wide board.
        "congress": _congress_rows(page.congress, site),
        "congress_url": f"{site}/congress",
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
    try:
        page = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not page.has_data:
        raise HTTPException(404, f"No data available for {page.ticker}.")
    return _render(request, page)


# --- Sector pages: /sector/{slug} --------------------------------------------------------
#
# The internal-linking hub: each sector page lists its top stocks (linked to their /stock/
# pages), and each stock page links back to its sector. This sector->stock structure is
# what lets a crawler reach every leaf page and spreads authority across the site.


def get_sector_page_use_case(db: Session = Depends(get_db)) -> GetSectorPage:
    return GetSectorPage(SeoReadRepositoryAdapterImpl(db))


def _sector_description(page: SectorPage) -> str:
    label = page.label
    examples = ", ".join(s.ticker for s in page.stocks[:5])
    desc = (
        f"The largest {label} stocks by market cap — {len(page.stocks)} companies with "
        "P/E, FCF yield and key metrics on Nama Insights."
    )
    if examples:
        desc += f" Incl. {examples}."
    return desc if len(desc) <= 160 else desc[:159].rstrip(" ,.") + "…"


def _listing_jsonld(list_name: str, stocks, canonical: str, site: str) -> str:
    item_list = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": list_name,
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i + 1,
                "url": f"{site}/stock/{stock.ticker}",
                "name": stock.name or stock.ticker,
            }
            for i, stock in enumerate(stocks)
        ],
    }
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Nama Insights", "item": site},
            {"@type": "ListItem", "position": 2, "name": "Stocks", "item": f"{site}/search"},
            {"@type": "ListItem", "position": 3, "name": list_name, "item": canonical},
        ],
    }
    return json.dumps([item_list, breadcrumbs]).replace("<", "\\u003c")


def _sector_jsonld(page: SectorPage, canonical: str, site: str) -> str:
    return _listing_jsonld(f"{page.label} Stocks", page.stocks, canonical, site)


def _listing_rows(stocks, site: str) -> list[dict[str, str]]:
    return [
        {
            "ticker": stock.ticker,
            "url": f"{site}/stock/{stock.ticker}",
            "name": stock.name or "—",
            "market_cap": _fmt_cap(stock.market_cap) or "—",
            "pe": _fmt_ratio(stock.pe_ratio) or "—",
            "fcf_yield": _fmt_pct(stock.fcf_yield) or "—",
        }
        for stock in stocks
    ]


def _render_sector(request: Request, page: SectorPage) -> Response:
    site = _site_origin()
    label = page.label
    canonical = f"{site}/sector/{page.url_slug}"
    context = {
        "title": f"{label} Stocks — Top Companies by Market Cap | Nama Insights",
        "description": _sector_description(page),
        "canonical": canonical,
        "robots": "index,follow",  # a sector page only renders when it has stocks
        "site": site,
        "app_url": f"{site}/search",
        "heading": f"{label} Stocks",
        "crumb": label,
        "subtitle": (
            f"The {len(page.stocks)} largest {label} companies by market cap, with "
            "valuation and cash-flow metrics."
        ),
        "stocks": _listing_rows(page.stocks, site),
        "cta_text": f"Screen {label} stocks on Nama Insights →",
        "jsonld": _sector_jsonld(page, canonical, site),
        "year": datetime.now(timezone.utc).year,
    }
    response = _TEMPLATES.TemplateResponse(
        request=request, name="listing.html", context=context
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@router.get("/sector/{sector}")
def sector_page_endpoint(
    sector: str,
    request: Request,
    use_case: GetSectorPage = Depends(get_sector_page_use_case),
):
    try:
        page = use_case.execute(sector)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not page.has_data:
        raise HTTPException(404, f"No stocks found for sector '{sector}'.")
    return _render_sector(request, page)


# --- Screen ("best-of") landing pages: /screen/{slug} ------------------------------------
#
# High-intent long-tail pages generated from the same universe the search sorts (e.g.
# "highest FCF yield", "cheapest by P/E"). Each lists its top stocks, linked to their pages.


def get_screen_page_use_case(db: Session = Depends(get_db)) -> GetScreenPage:
    return GetScreenPage(SeoReadRepositoryAdapterImpl(db))


def _render_screen(request: Request, page: ScreenPage) -> Response:
    site = _site_origin()
    screen = page.screen
    assert screen is not None  # guarded by has_data before calling
    canonical = f"{site}/screen/{screen.slug}"
    context = {
        "title": f"{screen.heading} | Nama Insights",
        "description": screen.description,
        "canonical": canonical,
        "robots": "index,follow",
        "site": site,
        "app_url": f"{site}/screener",
        "heading": screen.heading,
        "crumb": screen.heading,
        "subtitle": screen.subtitle,
        "stocks": _listing_rows(page.stocks, site),
        "cta_text": "Build your own screen on Nama Insights →",
        "jsonld": _listing_jsonld(screen.heading, page.stocks, canonical, site),
        "year": datetime.now(timezone.utc).year,
    }
    response = _TEMPLATES.TemplateResponse(
        request=request, name="listing.html", context=context
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@router.get("/screen/{slug}")
def screen_page_endpoint(
    slug: str,
    request: Request,
    use_case: GetScreenPage = Depends(get_screen_page_use_case),
):
    try:
        page = use_case.execute(slug)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not page.has_data:
        raise HTTPException(404, f"No screen found for '{slug}'.")
    return _render_screen(request, page)


# --- ETF pages: /etf/{ticker} ------------------------------------------------------------
#
# A per-fund page, reusing the generic entity template (ticker.html) — the same shape as a
# stock page (name + metrics + summary), just with fund facts (AUM, expense ratio, category).


def get_etf_page_use_case(db: Session = Depends(get_db)) -> GetEtfPage:
    return GetEtfPage(SeoReadRepositoryAdapterImpl(db))


def _etf_description(name: str, ticker: str, facts: EtfPageFacts) -> str:
    category = _humanize(facts.category)
    lead = f"{name} ({ticker})" + (f" — {category} ETF" if category else " ETF")
    parts: list[str] = []
    if facts.net_assets is not None:
        parts.append(f"AUM {_fmt_cap(facts.net_assets)}")
    if facts.expense_ratio is not None:
        parts.append(f"expense ratio {facts.expense_ratio:.2f}%")
    stats = f" {', '.join(parts)}." if parts else "."
    desc = f"{lead}.{stats} AUM, expense ratio, dividend yield and key facts on Nama Insights."
    return desc if len(desc) <= 160 else desc[:159].rstrip(" ,.") + "…"


def _etf_summary(name: str, ticker: str, facts: EtfPageFacts) -> str:
    category = _humanize(facts.category)
    lead = f"{name} ({ticker}) is an exchange-traded fund" + (
        f" in the {category} category." if category else "."
    )
    bits: list[str] = []
    if facts.net_assets is not None:
        bits.append(f"{_fmt_cap(facts.net_assets)} in assets")
    if facts.expense_ratio is not None:
        bits.append(f"an expense ratio of {facts.expense_ratio:.2f}%")
    if facts.dividend_yield is not None:
        bits.append(f"a {facts.dividend_yield:.2f}% dividend yield")
    if bits:
        lead += " It has " + _join_and(bits) + "."
    if facts.description:
        desc = facts.description.strip()
        lead += " " + (desc if len(desc) <= 500 else desc[:499].rstrip() + "…")
    return lead


def _fmt_pct2(value: float | None) -> str | None:
    return None if value is None else f"{value:.2f}%"


def _etf_metrics(facts: EtfPageFacts) -> list[dict[str, str]]:
    rows = [
        ("Assets under management", _fmt_cap(facts.net_assets)),
        ("Expense ratio", _fmt_pct2(facts.expense_ratio)),
        ("Category", _humanize(facts.category)),
        ("Dividend yield", _fmt_pct2(facts.dividend_yield)),
        ("NAV", None if facts.nav is None else f"${facts.nav:,.2f}"),
        ("Fund family", facts.fund_family),
        ("Exchange", facts.exchange),
    ]
    return [{"label": label, "value": value or "—"} for label, value in rows]


def _etf_jsonld(name: str, ticker: str, facts: EtfPageFacts, canonical: str, site: str) -> str:
    product: dict = {
        "@context": "https://schema.org",
        "@type": "FinancialProduct",
        "name": name,
        "tickerSymbol": ticker,
        "url": canonical,
        "category": "Exchange-Traded Fund",
    }
    if facts.description:
        product["description"] = facts.description.strip()[:300]
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Nama Insights", "item": site},
            {"@type": "ListItem", "position": 2, "name": "ETFs", "item": f"{site}/etf-screener"},
            {"@type": "ListItem", "position": 3, "name": f"{name} ({ticker})", "item": canonical},
        ],
    }
    return json.dumps([product, breadcrumbs]).replace("<", "\\u003c")


def _render_etf(request: Request, page: EtfPage) -> Response:
    facts = page.facts
    assert facts is not None  # guarded by has_data before calling
    site = _site_origin()
    ticker = page.ticker
    name = page.display_name
    canonical = f"{site}/etf/{ticker}"
    category = _humanize(facts.category)
    chips = [c for c in (category,) if c] + ["ETF"]
    subtitle_bits = [b for b in (category, facts.fund_family, facts.exchange) if b]
    context = {
        "title": f"{name} ({ticker}) ETF — AUM, Expense Ratio & Facts | Nama Insights",
        "description": _etf_description(name, ticker, facts),
        "canonical": canonical,
        "robots": "index,follow" if page.indexable else "noindex,follow",
        "site": site,
        "app_url": f"{site}/etf-screener",
        "name": name,
        "ticker": ticker,
        "subtitle": " · ".join(subtitle_bits),
        "chips": chips,
        "summary": _etf_summary(name, ticker, facts),
        "metrics": _etf_metrics(facts),
        "jsonld": _etf_jsonld(name, ticker, facts, canonical, site),
        # A fund has no sector page, so the generic template's related link is skipped.
        "sector_url": None,
        "sector_label": None,
        "year": datetime.now(timezone.utc).year,
    }
    response = _TEMPLATES.TemplateResponse(
        request=request, name="ticker.html", context=context
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@router.get("/etf/{ticker}")
def etf_page_endpoint(
    ticker: str,
    request: Request,
    use_case: GetEtfPage = Depends(get_etf_page_use_case),
):
    try:
        page = use_case.execute(ticker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not page.has_data:
        raise HTTPException(404, f"No ETF data available for {page.ticker}.")
    return _render_etf(request, page)


# --- AI stock screener landing page: /ai-stock-screener ----------------------------------
#
# A marketing/SEO landing page targeting the "AI stock screener" keyword and funnelling into
# the app's plain-English screener. Static content (no DB), so it renders straight here. The
# example queries deliberately reflect what the AI screener actually supports — sector/industry,
# company size, index membership, and how to rank — so the copy never promises a filter it can't do.

_AI_SCREENER_EXAMPLES = [
    "Mega-cap technology stocks",
    "Semiconductor companies in the S&P 500",
    "Large-cap stocks sorted by revenue growth",
    "The cheapest S&P 500 stocks by P/E",
    "Consumer stocks with the highest free cash flow yield",
    "Nasdaq-100 companies ranked by forward growth",
]

_AI_SCREENER_STEPS = [
    ("Describe what you want", "Type a plain-English request — the kind of companies you're looking for."),
    ("AI builds the screen", "It turns your words into filters — sector, size, index and how to rank."),
    ("Explore the results", "See matching US stocks with valuation, growth and cash-flow metrics — and refine the filters anytime."),
]

# (question, answer) pairs — rendered on the page AND emitted as FAQPage JSON-LD.
_AI_SCREENER_FAQS = [
    (
        "What is an AI stock screener?",
        "An AI stock screener lets you find stocks by describing what you want in plain "
        "English instead of setting filters by hand. You type a request like “mega-cap "
        "technology stocks” and the AI turns it into a live screen of US stocks.",
    ),
    (
        "Is Nama's AI stock screener free?",
        "Yes. Nama Insights is free to use — no login and no paywall.",
    ),
    (
        "How is it different from a screener like Finviz?",
        "Traditional screeners make you set each filter yourself — sector, market cap, "
        "ratios. Nama's AI screener lets you describe what you want in a sentence and builds "
        "the screen for you, and you can still refine the filters by hand afterwards.",
    ),
    (
        "What can I ask it?",
        "Requests like “semiconductor companies in the S&P 500”, “large caps sorted by "
        "revenue growth”, or “the cheapest stocks by P/E”. It understands sectors and "
        "industries, company size (market cap), index membership, and how to rank the results.",
    ),
    (
        "What stocks does it cover?",
        "US-listed stocks with a market capitalization of $1 billion or more — around 2,700 "
        "companies — with the data refreshed daily.",
    ),
]


def _ai_screener_jsonld(canonical: str, site: str) -> str:
    web_app = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": "Nama Insights AI Stock Screener",
        "url": canonical,
        "applicationCategory": "FinanceApplication",
        "operatingSystem": "Web",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "description": (
            "A free AI stock screener: describe the US stocks you want in plain English "
            "and get a live, filtered screen."
        ),
    }
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            }
            for question, answer in _AI_SCREENER_FAQS
        ],
    }
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Nama Insights", "item": site},
            {"@type": "ListItem", "position": 2, "name": "AI Stock Screener", "item": canonical},
        ],
    }
    return json.dumps([web_app, faq, breadcrumbs]).replace("<", "\\u003c")


@router.get("/ai-stock-screener")
def ai_stock_screener_page(request: Request) -> Response:
    site = _site_origin()
    canonical = f"{site}/ai-stock-screener"
    popular = [
        ("Highest free cash flow yield", f"{site}/screen/high-fcf-yield"),
        ("Cheapest by P/E", f"{site}/screen/cheapest-pe"),
        ("Highest revenue growth", f"{site}/screen/highest-revenue-growth"),
        ("Largest by market cap", f"{site}/screen/largest-companies"),
    ]
    context = {
        "title": "Free AI Stock Screener — Screen Stocks in Plain English | Nama Insights",
        "description": (
            "A free AI stock screener: describe the US stocks you want in plain English and "
            "get a live, filtered screen — no filters to learn, no signup."
        ),
        "canonical": canonical,
        "robots": "index,follow",
        "site": site,
        "app_url": f"{site}/screener",
        "examples": _AI_SCREENER_EXAMPLES,
        "steps": [{"title": title, "body": body} for title, body in _AI_SCREENER_STEPS],
        "faqs": [{"q": question, "a": answer} for question, answer in _AI_SCREENER_FAQS],
        "popular": [{"label": label, "url": url} for label, url in popular],
        "jsonld": _ai_screener_jsonld(canonical, site),
        "year": datetime.now(timezone.utc).year,
    }
    response = _TEMPLATES.TemplateResponse(
        request=request, name="ai_screener.html", context=context
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# --- Stock screener landing page: /stock-screener ----------------------------------------
#
# The keyword page for "stock screener" / "free stock screener". Deliberately distinct from
# the AI page: it leads on the *filter dimensions* (what you can screen by) rather than the
# plain-English angle, so it's genuinely different content — not a doorway duplicate.

_STOCK_SCREENER_FEATURES = [
    ("Market cap", "Mega, large, mid or small cap — screen by company size."),
    ("Sector & industry", "Technology, healthcare, energy, semiconductors and dozens more."),
    ("Valuation", "Trailing P/E and free-cash-flow yield — cheap on earnings or on cash."),
    ("Growth", "Revenue and EPS growth, trailing and forward (analyst consensus)."),
    ("Index membership", "Filter to S&P 500 or Nasdaq-100 constituents."),
    ("Plain-English AI", "Prefer words to filters? Describe what you want and AI builds the screen."),
]

_STOCK_SCREENER_FAQS = [
    (
        "Is the stock screener free?",
        "Yes — Nama Insights is free to use, with no login and no paywall.",
    ),
    (
        "What can I screen stocks by?",
        "Market cap, sector and industry, valuation (trailing P/E and free-cash-flow yield), "
        "revenue and EPS growth (trailing and forward), and index membership (S&P 500 / "
        "Nasdaq-100).",
    ),
    (
        "What stocks are included?",
        "US-listed stocks with a market capitalization of $1 billion or more — around 2,700 "
        "companies — with the data refreshed daily.",
    ),
    (
        "Can I screen in plain English?",
        "Yes. Describe what you want — “mega-cap tech with high free cash flow yield” — and the "
        "AI stock screener builds the filters for you.",
    ),
    (
        "What is free-cash-flow yield?",
        "Free cash flow per share divided by the share price. It shows how much cash a company "
        "generates relative to its market value — a cash-based complement to the P/E ratio, and "
        "something most free screeners leave out.",
    ),
]


def _stock_screener_jsonld(canonical: str, site: str) -> str:
    web_app = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": "Nama Insights Stock Screener",
        "url": canonical,
        "applicationCategory": "FinanceApplication",
        "operatingSystem": "Web",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "description": (
            "A free stock screener for US stocks: filter by market cap, sector, valuation, "
            "free-cash-flow yield and growth."
        ),
    }
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            }
            for question, answer in _STOCK_SCREENER_FAQS
        ],
    }
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Nama Insights", "item": site},
            {"@type": "ListItem", "position": 2, "name": "Stock Screener", "item": canonical},
        ],
    }
    return json.dumps([web_app, faq, breadcrumbs]).replace("<", "\\u003c")


@router.get("/stock-screener")
def stock_screener_page(request: Request) -> Response:
    site = _site_origin()
    canonical = f"{site}/stock-screener"
    popular = [
        ("Highest free cash flow yield", f"{site}/screen/high-fcf-yield"),
        ("Cheapest by P/E", f"{site}/screen/cheapest-pe"),
        ("Highest revenue growth", f"{site}/screen/highest-revenue-growth"),
        ("Largest by market cap", f"{site}/screen/largest-companies"),
    ]
    context = {
        "title": "Free Stock Screener — Filter US Stocks by Valuation & Growth | Nama Insights",
        "description": (
            "A free stock screener for US stocks: filter by market cap, sector, valuation, "
            "free-cash-flow yield and growth — or describe what you want in plain English. "
            "No login, no paywall."
        ),
        "canonical": canonical,
        "robots": "index,follow",
        "site": site,
        "app_url": f"{site}/screener",
        "ai_url": f"{site}/ai-stock-screener",
        "features": [{"title": title, "body": body} for title, body in _STOCK_SCREENER_FEATURES],
        "faqs": [{"q": question, "a": answer} for question, answer in _STOCK_SCREENER_FAQS],
        "popular": [{"label": label, "url": url} for label, url in popular],
        "jsonld": _stock_screener_jsonld(canonical, site),
        "year": datetime.now(timezone.utc).year,
    }
    response = _TEMPLATES.TemplateResponse(
        request=request, name="stock_screener.html", context=context
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# --- Congress trades board: /congress ----------------------------------------------------
#
# The market-wide Congressional-trades board — a keyword/landing page for "congress stock
# trades" that lists the most recently disclosed House/Senate trades and links each to its stock
# page (the internal-linking hub back into the universe). DB-only, like every other content page.


_CONGRESS_FAQS = [
    (
        "What are Congressional stock trades?",
        "They're the stock purchases and sales that members of the US House and Senate (and their "
        "spouses and dependents) make and disclose publicly. Because members can see policy and "
        "briefings before the public does, many investors watch these trades closely.",
    ),
    (
        "Do members of Congress have to report their trades?",
        "Yes. The STOCK Act requires every Representative and Senator to publicly disclose each "
        "stock trade within 45 days, including the asset, the type of trade, and a dollar range.",
    ),
    (
        "Why is the amount shown as a range?",
        "Congress discloses trades in bands (for example, “$1,001 - $15,000”) rather than an exact "
        "figure — that's all the STOCK Act filings report, so an exact dollar amount isn't available.",
    ),
    (
        "Is Nama's Congress trades tracker free?",
        "Yes. Nama Insights is free to use — no login and no paywall.",
    ),
    (
        "How often is the data updated?",
        "The board is refreshed weekly from the official House and Senate disclosures. Trades can "
        "appear up to 45 days after they were made, since that's the STOCK Act filing window.",
    ),
]


def get_congress_board_page_use_case(
    db: Session = Depends(get_db),
) -> GetCongressBoardPage:
    # Pure DB read over the congress table joined to the anchor — no vendor, no key.
    return GetCongressBoardPage(SeoReadRepositoryAdapterImpl(db))


def _congress_jsonld(canonical: str, site: str) -> str:
    dataset = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "US Congress Stock Trades",
        "url": canonical,
        "description": (
            "Recent stock trades disclosed by members of the US House and Senate under the "
            "STOCK Act — member, chamber, ticker, buy/sell, disclosed dollar range and dates."
        ),
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "Nama Insights", "url": site},
    }
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            }
            for question, answer in _CONGRESS_FAQS
        ],
    }
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Nama Insights", "item": site},
            {"@type": "ListItem", "position": 2, "name": "Congress Trades", "item": canonical},
        ],
    }
    return json.dumps([dataset, faq, breadcrumbs]).replace("<", "\\u003c")


def _congress_stats(page: CongressBoardPage) -> list[dict[str, str]]:
    trades = page.trades
    buys = sum(1 for t in trades if t.tx_type == "Purchase")
    sells = sum(1 for t in trades if t.tx_type == "Sale")
    return [
        {"value": f"{len(trades)}", "label": "Recent trades"},
        {"value": f"{buys}", "label": "Buys"},
        {"value": f"{sells}", "label": "Sells"},
    ]


def _render_congress(request: Request, page: CongressBoardPage) -> Response:
    site = _site_origin()
    canonical = f"{site}/congress"
    context = {
        "title": "US Congress Stock Trades — Who's Buying & Selling | Nama Insights",
        "description": (
            "Track recent stock trades disclosed by members of the US House and Senate under the "
            "STOCK Act — member, chamber, buy or sell, dollar range and dates. Free, updated weekly."
        ),
        "canonical": canonical,
        "robots": "index,follow",
        "site": site,
        "app_url": f"{site}/search",
        "heading": "US Congress Stock Trades",
        "subtitle": (
            "The most recent stock trades disclosed by members of the US House and Senate under "
            "the STOCK Act — who traded, whether they bought or sold, and the disclosed dollar range."
        ),
        "stats": _congress_stats(page) if not page.is_empty else [],
        "trades": _congress_rows(page.trades, site),
        "cta_text": "Explore stocks on Nama Insights →",
        "faqs": [{"q": question, "a": answer} for question, answer in _CONGRESS_FAQS],
        "jsonld": _congress_jsonld(canonical, site),
        "year": datetime.now(timezone.utc).year,
    }
    response = _TEMPLATES.TemplateResponse(
        request=request, name="congress.html", context=context
    )
    # Slow-moving DB feed refreshed weekly by the cron — cache generously.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@router.get("/congress")
def congress_board_page(
    request: Request,
    use_case: GetCongressBoardPage = Depends(get_congress_board_page_use_case),
):
    return _render_congress(request, use_case.execute())


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
> AI-generated analysis. Includes a free AI stock screener that turns a plain-English
> request into a live screen. Market data is refreshed daily.

## Stock screener
Filter US stocks by market cap, sector, valuation, free-cash-flow yield and growth —
/stock-screener (the tool runs at /screener).

## AI stock screener
Describe the stocks you want in plain English and the AI builds the screen —
/ai-stock-screener (the tool runs at /screener).

## Stock pages
Per-ticker pages with market cap, trailing P/E, free-cash-flow yield, year-over-year
growth, and sector/industry classification — e.g. /stock/AAPL, /stock/NVDA, /stock/MSFT.
The full list is in /sitemap.xml.

## Sector pages
The largest stocks in each sector by market cap — e.g. /sector/technology,
/sector/financial-services, /sector/healthcare.

## Screens (best-of lists)
Ranked lists updated daily — /screen/high-fcf-yield, /screen/cheapest-pe,
/screen/highest-revenue-growth, /screen/largest-companies.

## Congress trades
Recent stock trades disclosed by members of the US House and Senate under the STOCK Act
(member, chamber, buy/sell, disclosed dollar range and dates) — /congress. Per-stock
trades also appear on that stock's page (e.g. /stock/NVDA).

## ETF pages
Per-fund pages with AUM, expense ratio, category, dividend yield and NAV —
e.g. /etf/VOO, /etf/QQQ, /etf/SPY.

## Daily market brief
A short, plain-language AI read of how the whole US market moved each day — the
headline indices, sector rotation, and the day's biggest movers. The latest is at
/market/brief, and each day is a dated page — e.g. /market/brief/2026-07-14. The full
list of dated briefs is in /sitemap.xml.

## Earnings calendar
Which US companies are scheduled to report earnings on which upcoming days, grouped by
day — /earnings-calendar.

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
    return GetSitemap(SeoReadRepositoryAdapterImpl(db))


def _sitemap_xml(data: SitemapData, site: str) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{escape(site + '/')}</loc></url>",
        # Static landing pages (marketing/keyword pages, not data-driven).
        f"  <url><loc>{escape(site + '/ai-stock-screener')}</loc></url>",
        f"  <url><loc>{escape(site + '/stock-screener')}</loc></url>",
        # The daily brief hub + the earnings-calendar page (both live, data-driven views).
        f"  <url><loc>{escape(site + '/market/brief')}</loc></url>",
        f"  <url><loc>{escape(site + '/earnings-calendar')}</loc></url>",
        f"  <url><loc>{escape(site + '/congress')}</loc></url>",
    ]
    def _entity_url(prefix: str, ref) -> str:
        loc = escape(f"{site}/{prefix}/{ref.ticker}")
        if ref.last_modified is not None:
            return (
                f"  <url><loc>{loc}</loc>"
                f"<lastmod>{ref.last_modified.isoformat()}</lastmod></url>"
            )
        return f"  <url><loc>{loc}</loc></url>"

    for ref in data.stock_pages:
        parts.append(_entity_url("stock", ref))
    for ref in data.etf_pages:
        parts.append(_entity_url("etf", ref))
    for slug in data.sector_slugs:
        # Hyphenated URL form of the stored snake_case slug.
        loc = escape(f"{site}/sector/{slug.replace('_', '-')}")
        parts.append(f"  <url><loc>{loc}</loc></url>")
    for slug in data.screen_slugs:
        parts.append(f"  <url><loc>{escape(f'{site}/screen/{slug}')}</loc></url>")
    # Each day's brief is a fresh, durable URL — dated pages are compounding SEO. ``lastmod``
    # is the brief's own date (it never changes once written).
    for brief_date in data.brief_dates:
        iso = brief_date.isoformat()
        loc = escape(f"{site}/market/brief/{iso}")
        parts.append(f"  <url><loc>{loc}</loc><lastmod>{iso}</lastmod></url>")
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
    xml = _sitemap_xml(use_case.execute(), _site_origin())
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )
