"""Interface Adapter: revenue broken out by segment/product from SEC EDGAR.

The flat XBRL REST API (``companyconcept``) that ``SecEdgarRevenueProvider``
uses returns only *consolidated* figures — the SEC strips the dimensional
contexts, so the segment/product splits a filer reports are absent from it.
Those splits live one level deeper: in the filing's **inline XBRL** (the primary
10-Q/10-K document), where each revenue fact is qualified by an axis + member
(``StatementBusinessSegmentsAxis`` / ``ProductOrServiceAxis``). This adapter
fetches the recent periodic filings and parses those member-qualified facts into
per-quarter ``RevenueBreakdown`` entities. It's the only module that knows the
inline-XBRL shape exists; swap it and nothing else changes.

How it stays correct:

* Only **standalone-quarter** facts are kept (period span ~90 days), so the
  year-to-date and full-year figures in the same filing don't masquerade as a
  quarter — the same duration-band trick the flat revenue adapter uses.
* A fact is attributed to *one* cut only: a context carrying a segment member
  (and no product member, nor any other disaggregation axis) is a segment line;
  one carrying a product member likewise. A context crossed by both — or by an
  unrelated axis like geography — is a sub-cell, not a top-level split, and is
  skipped, so the components in each list sum to ~the quarter's total without
  double-counting. ``ConsolidationItemsAxis`` (OperatingSegments vs Corporate)
  is treated as a neutral qualifier, so a segment's own line still counts while
  the corporate-reconciliation bucket (which carries no segment member) drops.
* A standalone Q4 isn't separately tagged — a 10-K reports the year — so the
  fiscal Q4 simply has no breakdown (best-effort: that quarter stays empty).

Like the sibling revenue adapter, EDGAR is keyed by CIK (resolved through
``company_tickers.json``) and requires a descriptive ``User-Agent``.

Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""

from __future__ import annotations

import re
import threading
from datetime import date

import httpx

from app.stocks.entities import RevenueBreakdown, RevenueComponent
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import SegmentRevenueProvider

# The same us-gaap revenue concepts the flat adapter merges; a filer tags its
# segment lines under whichever of these it reports revenue with.
_REVENUE_TAGS = frozenset(
    {
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    }
)

# Duration band (days) that marks an XBRL period as a single quarter; wide enough
# to absorb 52/53-week fiscal calendars. Year-to-date (~182/273d) and annual
# (~365d) facts fall outside it and are ignored — we only want standalone quarters.
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100

# The XBRL axes we read. A segment member splits revenue by reportable operating
# segment; a product member by product/service line. ConsolidationItems
# (OperatingSegments vs Corporate) is a reconciliation qualifier, not a
# disaggregation — allowed alongside a segment line, but never a cut of its own.
_SEGMENT_AXIS = "StatementBusinessSegmentsAxis"
_PRODUCT_AXIS = "ProductOrServiceAxis"
_RECONCILIATION_AXIS = "ConsolidationItemsAxis"

# Inline-XBRL structure. Contexts carry the period + dimensional members; facts
# (ix:nonFraction) carry a value pointing at a context. Validated against real
# AMZN/MU/SNDK filings.
_CONTEXT_RE = re.compile(
    r'<(?:xbrli:)?context id="([^"]+)">(.*?)</(?:xbrli:)?context>', re.S
)
_MEMBER_RE = re.compile(r'explicitMember dimension="([^"]+)">\s*([^<\s]+)\s*<')
_START_RE = re.compile(r"startDate>\s*([0-9-]+)")
_END_RE = re.compile(r"endDate>\s*([0-9-]+)")
_FACT_RE = re.compile(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", re.S)
_NAME_RE = re.compile(r'name="([^"]+)"')
_CONTEXTREF_RE = re.compile(r'contextRef="([^"]+)"')
_SCALE_RE = re.compile(r'scale="(-?\d+)"')
_SIGN_RE = re.compile(r'sign="([^"]+)"')


class SecEdgarSegmentRevenueProvider(SegmentRevenueProvider):
    """Parses per-quarter segment/product revenue from SEC EDGAR filings."""

    _TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    _SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
    _ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"
    _DEFAULT_USER_AGENT = "nama_backend/1.0 (https://namainsights.com)"
    # How many recent 10-Q/10-K filings to scan. Each 10-Q contributes one
    # standalone quarter (plus the prior-year comparative, which falls outside the
    # 4-quarter EPS window), so a handful covers the recent quarters; a 10-K in the
    # run yields only annual facts (skipped by the quarter band).
    _MAX_FILINGS = 5

    def __init__(
        self,
        user_agent: str = _DEFAULT_USER_AGENT,
        *,
        tickers_url: str | None = None,
        submissions_url: str | None = None,
        archive_doc_url: str | None = None,
    ) -> None:
        self._http = httpx.Client(timeout=10.0, headers={"User-Agent": user_agent})
        self._tickers_url = tickers_url or self._TICKERS_URL
        self._submissions_url = submissions_url or self._SUBMISSIONS_URL
        self._archive_doc_url = archive_doc_url or self._ARCHIVE_DOC_URL
        # ticker -> CIK is one shared fetch for every symbol; built lazily and held
        # for the process (it changes rarely), like the flat revenue adapter.
        self._lock = threading.Lock()
        self._cik_by_symbol: dict[str, int] | None = None

    def get_quarterly_segment_revenue(
        self, symbol: str
    ) -> dict[date, RevenueBreakdown]:
        cik = self._resolve_cik(symbol)
        filings = self._recent_filings(symbol, cik)
        # period end -> dim ("segment"/"product") -> member -> (filed, amount).
        # The filed date breaks ties so a restatement (later filing) wins.
        acc: dict[date, dict[str, dict[str, tuple[str, float]]]] = {}
        for accn, doc, filed in filings:
            html = self._fetch_filing(cik, accn, doc)
            if html is None:
                continue  # a single unreadable filing must not sink the rest
            for end, dim, member, amount in _components(html):
                slot = acc.setdefault(end, {"segment": {}, "product": {}})[dim]
                prior = slot.get(member)
                if prior is None or filed >= prior[0]:
                    slot[member] = (filed, amount)
        return _breakdowns(acc)

    # -- CIK resolution -----------------------------------------------------

    def _resolve_cik(self, symbol: str) -> int:
        cik = self._cik_map().get(symbol.upper())
        if cik is None:
            # Not an EDGAR filer we can resolve (e.g. an ADR or ETF) — "no data",
            # like the other best-effort enrichment providers.
            raise StockNotFound(symbol)
        return cik

    def _cik_map(self) -> dict[str, int]:
        with self._lock:
            if self._cik_by_symbol is not None:
                return self._cik_by_symbol
        payload = self._get_json("company_tickers", self._tickers_url)
        mapping = _parse_cik_map(payload)
        with self._lock:
            self._cik_by_symbol = mapping
        return mapping

    # -- filings ------------------------------------------------------------

    def _recent_filings(self, symbol: str, cik: int) -> list[tuple[str, str, str]]:
        """Return up to ``_MAX_FILINGS`` recent (accession, document, filed) for
        the symbol's 10-Q/10-K filings, newest first."""
        payload = self._get_json(symbol, self._submissions_url.format(cik=cik))
        return _periodic_filings(payload, self._MAX_FILINGS)

    def _fetch_filing(self, cik: int, accn: str, doc: str) -> str | None:
        """Fetch one filing's primary inline-XBRL document, or ``None`` if it
        can't be retrieved — a missing filing degrades coverage, not the call."""
        url = self._archive_doc_url.format(cik=cik, accn=accn, doc=doc)
        try:
            resp = self._http.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        return resp.text

    def _get_json(self, symbol: str, url: str):
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(symbol, str(exc)) from exc
        if resp.status_code != 200:
            body = resp.text[:200].strip() or "<empty body>"
            raise StockDataUnavailable(
                symbol, f"EDGAR request failed (HTTP {resp.status_code}): {body}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(symbol, f"invalid JSON payload: {exc}") from exc


def _parse_cik_map(payload) -> dict[str, int]:
    """Build a TICKER -> CIK map from SEC's ``company_tickers.json`` (an object of
    rows like ``{"cik_str": 320193, "ticker": "AAPL"}``)."""
    out: dict[str, int] = {}
    rows = payload.values() if isinstance(payload, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        cik = row.get("cik_str")
        if isinstance(ticker, str) and isinstance(cik, int):
            out[ticker.upper()] = cik
    return out


def _periodic_filings(payload, limit: int) -> list[tuple[str, str, str]]:
    """Pull the most recent 10-Q/10-K (accession-no-dashes, document, filed) rows
    from a submissions payload. ``filings.recent`` holds parallel arrays, newest
    first."""
    recent = {}
    if isinstance(payload, dict):
        filings = payload.get("filings")
        if isinstance(filings, dict) and isinstance(filings.get("recent"), dict):
            recent = filings["recent"]
    forms = recent.get("form") or []
    accns = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    filed = recent.get("filingDate") or []
    out: list[tuple[str, str, str]] = []
    for f, accn, doc, when in zip(forms, accns, docs, filed):
        if f in ("10-Q", "10-K") and accn and doc:
            out.append((accn.replace("-", ""), doc, when or ""))
            if len(out) >= limit:
                break
    return out


def _components(html: str):
    """Yield ``(period_end, dim, label, amount)`` for every standalone-quarter
    segment/product revenue fact in one filing's inline XBRL.

    ``dim`` is ``"segment"`` or ``"product"``. Facts that are consolidated totals,
    year-to-date/annual periods, sub-cells crossed by two cuts, or qualified by an
    unrelated axis (e.g. geography) are skipped — see the module docstring.
    """
    contexts = _parse_contexts(html)
    for match in _FACT_RE.finditer(html):
        attrs, text = match.group(1), match.group(2)
        name = _NAME_RE.search(attrs)
        ctx_ref = _CONTEXTREF_RE.search(attrs)
        if name is None or ctx_ref is None:
            continue
        if _local(name.group(1)) not in _REVENUE_TAGS:
            continue
        ctx = contexts.get(ctx_ref.group(1))
        if ctx is None or ctx["start"] is None or ctx["end"] is None:
            continue
        span = (ctx["end"] - ctx["start"]).days
        if not _QUARTER_MIN_DAYS <= span <= _QUARTER_MAX_DAYS:
            continue
        classified = _classify(ctx["members"])
        if classified is None:
            continue
        dim, member = classified
        scale = _SCALE_RE.search(attrs)
        sign = _SIGN_RE.search(attrs)
        amount = _fact_value(
            text,
            int(scale.group(1)) if scale else None,
            sign.group(1) if sign else None,
        )
        if amount is None:
            continue
        yield ctx["end"], dim, member, amount


def _parse_contexts(html: str) -> dict[str, dict]:
    """Map context id -> {members: [(axis_local, member_local)], start, end}.

    Only duration contexts (with a start and end) are useful here; instants carry
    no period span to classify as a quarter.
    """
    contexts: dict[str, dict] = {}
    for match in _CONTEXT_RE.finditer(html):
        body = match.group(2)
        members = [
            (_local(axis), _local(member))
            for axis, member in _MEMBER_RE.findall(body)
        ]
        start = _START_RE.search(body)
        end = _END_RE.search(body)
        contexts[match.group(1)] = {
            "members": members,
            "start": _parse_date(start.group(1)) if start else None,
            "end": _parse_date(end.group(1)) if end else None,
        }
    return contexts


def _classify(members: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Decide whether a context's members make a fact a top-level segment or
    product line, returning ``(dim, member_local)`` or ``None`` to skip it.

    A clean top-level cut is qualified by exactly one of the two axes (segment or
    product), optionally alongside the neutral reconciliation axis — nothing else.
    Both axes together (a sub-cell) or any unrelated axis (geography, a custom
    plan) disqualifies it, keeping each list's components additive to the total.
    """
    segment = next((m for ax, m in members if ax == _SEGMENT_AXIS), None)
    product = next((m for ax, m in members if ax == _PRODUCT_AXIS), None)
    has_extra = any(
        ax not in (_SEGMENT_AXIS, _PRODUCT_AXIS, _RECONCILIATION_AXIS)
        for ax, _ in members
    )
    if has_extra:
        return None
    if segment is not None and product is None:
        return "segment", segment
    if product is not None and segment is None:
        return "product", product
    return None


# The us-gaap "goods vs services" members are a coarse top-level split on the
# same axis as a filer's specific product lines. When both are present (Amazon
# tags Product/Service *and* Online stores, AWS, Advertising, …) the two generic
# members duplicate the detailed partition — each sums to the same total — so we
# drop them in favour of the finer lines, keeping by_product a single clean
# partition. A filer that reports *only* the goods/services split keeps it.
_GENERIC_PRODUCT_MEMBERS = frozenset({"ProductMember", "ServiceMember"})


def _breakdowns(
    acc: dict[date, dict[str, dict[str, tuple[str, float]]]],
) -> dict[date, RevenueBreakdown]:
    """Fold the accumulator into a ``RevenueBreakdown`` per period end — labeling
    members, dropping the redundant coarse product split, and ordering each cut
    largest first (then label) for a stable, readable layout."""

    def components(
        slot: dict[str, tuple[str, float]], *, drop_generic: bool
    ) -> tuple[RevenueComponent, ...]:
        members = slot
        if drop_generic and any(m not in _GENERIC_PRODUCT_MEMBERS for m in slot):
            members = {m: v for m, v in slot.items() if m not in _GENERIC_PRODUCT_MEMBERS}
        labeled = [(_humanize_member(m), amt) for m, (_, amt) in members.items()]
        labeled.sort(key=lambda pair: (-pair[1], pair[0]))
        return tuple(RevenueComponent(label=label, amount=amt) for label, amt in labeled)

    out: dict[date, RevenueBreakdown] = {}
    for end, dims in acc.items():
        breakdown = RevenueBreakdown(
            by_segment=components(dims["segment"], drop_generic=False),
            by_product=components(dims["product"], drop_generic=True),
        )
        if not breakdown.is_empty:
            out[end] = breakdown
    return out


def _humanize_member(member_local: str) -> str:
    """Turn an XBRL member local-name into a display label.

    Strips the ``Member`` (and a trailing ``Segment``) suffix and splits the
    CamelCase into words while preserving acronyms: ``NorthAmericaSegmentMember``
    -> "North America", ``AWSSegmentMember`` -> "AWS", ``DRAMProductsMember`` ->
    "DRAM Products", ``OnlineStoresMember`` -> "Online Stores". Falls back to the
    raw local-name if stripping leaves nothing.
    """
    name = member_local
    if name.endswith("Member"):
        name = name[: -len("Member")]
    if name.endswith("Segment"):
        name = name[: -len("Segment")]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return spaced.strip() or member_local


def _fact_value(text: str, scale: int | None, sign: str | None) -> float | None:
    """Parse an inline-XBRL numeric fact: strip any nested markup and grouping
    commas, apply the ``scale`` power of ten and a negative ``sign``."""
    digits = re.sub(r"[^0-9.]", "", re.sub(r"<[^>]+>", "", text))
    if digits in ("", "."):
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    if scale is not None:
        value *= 10**scale
    if sign == "-":
        value = -value
    return value


def _local(qualified_name: str) -> str:
    """The local part of a ``prefix:Name`` qualified XBRL name."""
    return qualified_name.split(":")[-1]


def _parse_date(value) -> date | None:
    """Parse EDGAR's ``YYYY-MM-DD`` date; ``None`` if absent or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None
