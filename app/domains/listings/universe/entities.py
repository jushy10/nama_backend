from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum

from app.domains.shared.entities import StockPerformance


@dataclass(frozen=True)
class ScreenedStock:
    ticker: str
    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None
    price: float | None = None  # screen-time price; derives pe_ratio, not itself stored
    country: str | None = None  # ISO-2 listing country (US / CA)
    currency: str | None = None  # ISO-3 trading currency (USD / CAD)
    # True when this Canadian listing duplicates a US-listed company (a CDR or a same-ticker
    # dual-listing, matched by base ticker; or a rebranded .NE CDR, matched by company name) —
    # computed by the sync's CA pass (this listing vs the US universe), not the screen adapter,
    # which always leaves it False. The search hides these by default.
    has_us_listing: bool = False


@dataclass(frozen=True)
class CompanyClassification:
    sector: str | None = None
    industry: str | None = None
    domicile_country: str | None = None

    @classmethod
    def from_labels(
        cls, sector: object, industry: object, country: object = None
    ) -> "CompanyClassification":
        return cls(
            sector=slugify(sector),
            industry=slugify(industry),
            domicile_country=country_to_iso2(country),
        )


@dataclass(frozen=True)
class AnchorMetrics:
    fcf_per_share: float | None = None
    ocf_per_share: float | None = None
    revenue_growth_yoy: float | None = None
    eps_growth_yoy: float | None = None
    fcf_growth_yoy: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    return_on_equity: float | None = None
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    beta: float | None = None
    book_value_per_share: float | None = None
    sales_per_share: float | None = None
    dividend_per_share: float | None = None
    ebitda: float | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    shares_outstanding: float | None = None
    market_cap: float | None = None
    name: str | None = None


class StockSort(str, Enum):
    MARKET_CAP = "market_cap"
    REVENUE_GROWTH = "revenue_growth"
    EPS_GROWTH = "eps_growth"
    FCF_GROWTH = "fcf_growth"
    GROWTH = "growth"
    FORWARD_REVENUE_GROWTH = "forward_revenue_growth"
    FORWARD_EPS_GROWTH = "forward_eps_growth"
    FORWARD_GROWTH = "forward_growth"
    PE = "pe"
    FCF_YIELD = "fcf_yield"
    EV_EBITDA = "ev_ebitda"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class MarketCapTier(str, Enum):
    MEGA = "mega"
    LARGE = "large"
    MID = "mid"
    SMALL = "small"


# Ascending size order — the axis a tier-anchored peer cohort widens along (same tier first,
# then its nearest neighbours). Naming/ordering only; the tier ⇄ dollar-bounds mapping stays
# the adapter's job (see the ``MarketCapTier`` docstring).
_TIER_ORDER_ASC: tuple[MarketCapTier, ...] = (
    MarketCapTier.SMALL,
    MarketCapTier.MID,
    MarketCapTier.LARGE,
    MarketCapTier.MEGA,
)


@dataclass(frozen=True)
class StockSearchResult:
    ticker: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: float | None
    pe_ratio: float | None
    fcf_yield: float | None
    ev_ebitda: float | None
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    fcf_growth_yoy: float | None
    forward_revenue_growth_yoy: float | None
    forward_eps_growth_yoy: float | None
    in_sp500: bool
    in_nasdaq100: bool
    # Market facts default to None so a pre-multi-market builder still constructs; the DB read
    # (`_to_result`) always supplies them for a screened row. has_us_listing is surfaced so a
    # client can label / opt into the interlisted Canadian duplicates the search hides by default.
    country: str | None = None
    currency: str | None = None
    has_us_listing: bool = False
    performance: StockPerformance | None = None


@dataclass(frozen=True)
class StockSearchCriteria:
    query: str | None
    sectors: tuple[str, ...]
    industries: tuple[str, ...]
    in_sp500: bool | None
    in_nasdaq100: bool | None
    sort: StockSort | None
    direction: SortDirection
    limit: int
    offset: int
    market_cap_tiers: tuple[MarketCapTier, ...] = ()
    countries: tuple[str, ...] = ()
    include_interlisted: bool = False


@dataclass(frozen=True)
class ScreenIntent:
    query: str | None = None
    sectors: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    in_sp500: bool | None = None
    in_nasdaq100: bool | None = None
    market_cap_tiers: tuple[MarketCapTier, ...] = ()
    sort: StockSort | None = None
    direction: SortDirection = SortDirection.DESC
    limit: int | None = None


@dataclass(frozen=True)
class StockSearchPage:
    results: tuple[StockSearchResult, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class Classifications:
    sectors: tuple[str, ...]
    industries: tuple[str, ...]


@dataclass(frozen=True)
class IndustryValuation:
    # The smallest peer sample a benchmark can rest on and still say something about the
    # *industry* rather than about one or two companies. Below this, the "median" is just
    # a couple of stocks' own multiples (in the worst case the looked-up stock itself —
    # sole-peer industries exist in the live universe), so a comparison against it is
    # noise wearing a verdict. Five keeps ~80% of live industries while dropping every
    # degenerate case a sweep of the deployed data surfaced.
    MIN_REPRESENTATIVE_PEERS = 5

    industry: str
    count: int
    median_pe: float | None
    p25_pe: float | None
    p75_pe: float | None
    cohort: str = "industry"

    @property
    def is_representative(self) -> bool:
        return self.count >= self.MIN_REPRESENTATIVE_PEERS

    @classmethod
    def from_pe_ratios(
        cls, industry: str, pe_ratios: Sequence[float]
    ) -> "IndustryValuation":
        values = sorted(pe_ratios)
        return cls(
            industry=industry,
            count=len(values),
            median_pe=_percentile(values, 50),
            p25_pe=_percentile(values, 25),
            p75_pe=_percentile(values, 75),
        )

    @classmethod
    def for_stock_peers(
        cls,
        industry: str,
        anchor_tier: "MarketCapTier | None",
        peers: Sequence[tuple[float, "MarketCapTier"]],
    ) -> "IndustryValuation":
        all_pes = [pe for pe, _ in peers]
        if anchor_tier is None or not peers:
            return cls.from_pe_ratios(industry, all_pes)

        order = _TIER_ORDER_ASC
        anchor_rank = order.index(anchor_tier)
        present = {tier for _, tier in peers}
        # Widen the radius around the anchor tier until the cohort is representative; the last
        # radius (covering every present tier) is the whole-industry fallback, taken even if it
        # still falls short — the caller applies ``is_representative`` to decide whether to use it.
        max_radius = max(abs(order.index(tier) - anchor_rank) for tier in present)
        for radius in range(max_radius + 1):
            allowed = {
                tier
                for i, tier in enumerate(order)
                if abs(i - anchor_rank) <= radius
            }
            cohort_pes = [pe for pe, tier in peers if tier in allowed]
            if len(cohort_pes) >= cls.MIN_REPRESENTATIVE_PEERS or radius == max_radius:
                label = _cohort_label(allowed & present, present)
                return replace(cls.from_pe_ratios(industry, cohort_pes), cohort=label)
        # Unreachable: max_radius always terminates the loop above.
        return cls.from_pe_ratios(industry, all_pes)


@dataclass(frozen=True)
class PeerCompany:
    ticker: str
    name: str | None
    market_cap: float | None
    pe_ratio: float | None
    ev_ebitda: float | None
    fcf_yield: float | None
    net_margin: float | None
    revenue_growth_yoy: float | None
    tier: MarketCapTier | None = None
    is_anchor: bool = False


@dataclass(frozen=True)
class PeerMedians:
    pe_ratio: float | None
    ev_ebitda: float | None
    fcf_yield: float | None
    net_margin: float | None
    revenue_growth_yoy: float | None


@dataclass(frozen=True)
class PeerComparison:
    # The cohort widens (to neighbouring size tiers) until it holds at least this many peers
    # besides the anchor — enough for a median to mean "versus the group" rather than versus one
    # or two names. Deliberately looser than IndustryValuation's benchmark floor (which needs a
    # statistically representative sample); a comparison table is useful with a handful of named
    # peers, and the tier scoping matters more than the count.
    MIN_PEERS = 4
    # The most peers to show beside the anchor — the largest by market cap, so the table stays a
    # readable "you vs the giants of your industry" rather than a full sector dump.
    MAX_PEERS = 11

    ticker: str
    industry: str | None
    cohort: str
    anchor: PeerCompany | None
    peers: tuple[PeerCompany, ...]
    medians: PeerMedians

    @classmethod
    def build(
        cls, ticker: str, industry: str | None, candidates: Sequence[PeerCompany]
    ) -> "PeerComparison":
        anchor = next((c for c in candidates if c.ticker == ticker), None)
        others = [c for c in candidates if c.ticker != ticker]
        cohort, label = cls._select_cohort(anchor, others)
        cohort = sorted(cohort, key=lambda c: c.market_cap or 0.0, reverse=True)[
            : cls.MAX_PEERS
        ]
        displayed = ([anchor] if anchor is not None else []) + cohort
        return cls(
            ticker=ticker,
            industry=industry,
            cohort=label,
            anchor=replace(anchor, is_anchor=True) if anchor is not None else None,
            peers=tuple(cohort),
            medians=cls._medians(displayed),
        )

    @classmethod
    def _select_cohort(
        cls, anchor: PeerCompany | None, others: list[PeerCompany]
    ) -> tuple[list[PeerCompany], str]:
        order = _TIER_ORDER_ASC
        present = {c.tier for c in others if c.tier is not None}
        if anchor is None or anchor.tier is None or not present:
            return others, "industry"
        anchor_rank = order.index(anchor.tier)
        max_radius = max(abs(order.index(tier) - anchor_rank) for tier in present)
        for radius in range(max_radius + 1):
            allowed = {
                tier for i, tier in enumerate(order) if abs(i - anchor_rank) <= radius
            }
            cohort = [c for c in others if c.tier in allowed]
            if len(cohort) >= cls.MIN_PEERS or radius == max_radius:
                return cohort, _cohort_label(allowed & present, present)
        return others, "industry"  # unreachable: the loop always terminates at max_radius

    @staticmethod
    def _medians(companies: Sequence[PeerCompany]) -> PeerMedians:
        def median(values: list[float]) -> float | None:
            return _percentile(sorted(values), 50)

        def column(attr: str) -> float | None:
            return median(
                [
                    value
                    for c in companies
                    if (value := getattr(c, attr)) is not None
                ]
            )

        return PeerMedians(
            pe_ratio=column("pe_ratio"),
            ev_ebitda=column("ev_ebitda"),
            fcf_yield=column("fcf_yield"),
            net_margin=column("net_margin"),
            revenue_growth_yoy=column("revenue_growth_yoy"),
        )


def _cohort_label(chosen: set[MarketCapTier], present: set[MarketCapTier]) -> str:
    if not chosen or chosen == present:
        return "industry"
    return "/".join(tier.value for tier in _TIER_ORDER_ASC if tier in chosen)


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return round(sorted_values[0], 2)
    rank = (q / 100) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return round(sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo]), 2)


def slugify(label: object) -> str | None:
    if not isinstance(label, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or None


# Yahoo `.info['country']` display name → ISO-2 code, for the issuer-domicile column the US /
# Canadian screener split filters on. Only ``US`` and ``CA`` need to be exact (they drive the
# split); the rest are here so a *foreign* issuer (a CDR's underlying) reads as a definite
# non-``CA`` / non-``US`` code rather than an unknown ``None`` — an unmapped country stays
# ``None`` and is treated leniently (shown in its listing market). Covers the domiciles that
# actually appear at ≥$1B on a North-American listing, including every common CDR-issuer home.
_COUNTRY_ISO2 = {
    "united states": "US",
    "canada": "CA",
    "united kingdom": "GB",
    "ireland": "IE",
    "switzerland": "CH",
    "germany": "DE",
    "france": "FR",
    "netherlands": "NL",
    "belgium": "BE",
    "luxembourg": "LU",
    "spain": "ES",
    "italy": "IT",
    "sweden": "SE",
    "denmark": "DK",
    "norway": "NO",
    "finland": "FI",
    "austria": "AT",
    "portugal": "PT",
    "greece": "GR",
    "japan": "JP",
    "china": "CN",
    "hong kong": "HK",
    "taiwan": "TW",
    "south korea": "KR",
    "korea, republic of": "KR",
    "india": "IN",
    "singapore": "SG",
    "australia": "AU",
    "new zealand": "NZ",
    "israel": "IL",
    "brazil": "BR",
    "mexico": "MX",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
    "peru": "PE",
    "south africa": "ZA",
    "united arab emirates": "AE",
    "saudi arabia": "SA",
    "turkey": "TR",
    "indonesia": "ID",
    "thailand": "TH",
    "malaysia": "MY",
    "philippines": "PH",
    "vietnam": "VN",
    "bermuda": "BM",
    "cayman islands": "KY",
    "jersey": "JE",
    "guernsey": "GG",
    "monaco": "MC",
    "cyprus": "CY",
    "panama": "PA",
    "uruguay": "UY",
    "puerto rico": "PR",
}


def country_to_iso2(country: object) -> str | None:
    if not isinstance(country, str):
        return None
    text = country.strip()
    if not text:
        return None
    mapped = _COUNTRY_ISO2.get(text.lower())
    if mapped is not None:
        return mapped
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return None


# Tokens dropped when reducing a company name to its identity key — legal-form suffixes (which
# vary between a listing and its cross-listing, "Corp" vs "Corporation") and depositary-receipt
# markers. Limited to legal-form + DR noise, NOT generic words like "Group"/"Holdings", so two
# genuinely different companies don't collapse onto the same key.
_NAME_NOISE_TOKENS = frozenset(
    {
        "THE",
        "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES",
        "LTD", "LIMITED", "LLC", "LP", "LLP", "PLC", "SA", "AG", "NV", "SE",
        "CDR", "CDRS", "DEPOSITARY", "DEPOSITORY", "RECEIPT", "RECEIPTS",
        "CAD", "USD", "HEDGED", "UNHEDGED", "NONHEDGED",
    }
)


def normalize_company_name(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    tokens = [
        token
        for token in re.split(r"[^A-Z0-9]+", name.upper())
        if token and token not in _NAME_NOISE_TOKENS
    ]
    return "".join(tokens) or None
