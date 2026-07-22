from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

import httpx

from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.financials.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
    SegmentAxis,
)
from app.domains.financials.revenue_segments.interfaces import RevenueSegmentsAdapter

logger = logging.getLogger(__name__)

# SEC asks automated clients to identify themselves; a blank User-Agent is refused.
_USER_AGENT = "nama-backend/1.0 (revenue-segments sync; +https://namainsights.com)"

# The ticker -> CIK map and the per-company submissions index / filing archive.
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"

# XBRL revenue concepts, in preference order (lower rank wins when the same (year, axis, member)
# is tagged under more than one concept — a filer may state both the ASC-606 "excluding assessed
# tax" figure and a legacy ``Revenues``; we take the most specific).
_REVENUE_TAGS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": 0,
    "RevenueFromContractWithCustomerIncludingAssessedTax": 1,
    "Revenues": 2,
    "SalesRevenueNet": 3,
}

# XBRL segment axis local-name (any namespace prefix matches) for each of our SegmentAxis cuts.
_BUSINESS_AXIS = "StatementBusinessSegmentsAxis"
_AXIS_XBRL = {
    SegmentAxis.BUSINESS: _BUSINESS_AXIS,
    SegmentAxis.PRODUCT: "ProductOrServiceAxis",
    SegmentAxis.GEOGRAPHY: "StatementGeographicalAxis",
}

# A context's period must span at least this many days to count as annual — filters out the
# quarterly facts a 10-K can also carry (a fiscal year is ~365 days; 350 leaves slack for 52/53
# week filers).
_MIN_DURATION_DAYS = 350

# Belt to the serial sweep's suspenders: a minimum spacing between SEC requests so a fast host
# doesn't exceed EDGAR's ~10 req/s fair-use ceiling. 0 in the adapter default (tests never
# sleep); the production wiring dials it up.
_DEFAULT_MIN_REQUEST_INTERVAL = 0.0


@dataclass(frozen=True)
class _Context:
    members: tuple[tuple[str, str], ...]  # (axis local-name, member local-name)
    start: date | None
    end: date | None
    instant: date | None


class RevenueSegmentsAdapterImpl(RevenueSegmentsAdapter):
    def __init__(
        self,
        *,
        min_request_interval_seconds: float = _DEFAULT_MIN_REQUEST_INTERVAL,
    ) -> None:
        self._http = httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        )
        self._min_interval = max(0.0, min_request_interval_seconds)
        self._last_request = 0.0
        # Lazily-built ticker -> CIK map (one ~1 MB file covers every filer); cached for the
        # process once fetched, since it changes only as companies list/delist.
        self._ticker_cik: dict[str, int] | None = None

    def get_revenue_segments(self, symbol: str) -> RevenueSegmentation:
        cik = self._cik_for(symbol)  # raises StockNotFound when unmapped
        filing = self._latest_10k(cik)
        if filing is None:
            # Covered filer, but no annual report to parse (e.g. a foreign issuer that files a
            # 20-F) — best-effort, so an empty segmentation, not an error.
            return RevenueSegmentation(symbol=symbol, segments=())
        accession, primary_document = filing
        xml_bytes = self._instance_document(cik, accession, primary_document)
        if xml_bytes is None:
            return RevenueSegmentation(symbol=symbol, segments=())
        segments = _parse_revenue_segments(xml_bytes)
        return RevenueSegmentation(symbol=symbol, segments=segments)

    def _cik_for(self, symbol: str) -> int:
        if self._ticker_cik is None:
            self._ticker_cik = self._load_ticker_map()
        cik = self._ticker_cik.get(symbol.upper())
        if cik is None:
            raise StockNotFound(symbol)
        return cik

    def _load_ticker_map(self) -> dict[str, int]:
        payload = self._get_json(_COMPANY_TICKERS_URL, "*")
        mapping: dict[str, int] = {}
        # The file is a JSON object of positional rows: {"0": {"cik_str", "ticker", "title"}, …}.
        for row in payload.values():
            ticker = row.get("ticker")
            cik = row.get("cik_str")
            if isinstance(ticker, str) and isinstance(cik, int):
                mapping[ticker.upper()] = cik
        return mapping

    def _latest_10k(self, cik: int) -> tuple[str, str] | None:
        payload = self._get_json(_SUBMISSIONS_URL.format(cik=cik), str(cik))
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        documents = recent.get("primaryDocument", [])
        for i, form in enumerate(forms):
            if form == "10-K":
                # Guard against ragged arrays (they're parallel, but be defensive).
                if i < len(accessions) and i < len(documents):
                    return accessions[i], documents[i]
        return None

    def _instance_document(
        self, cik: int, accession: str, primary_document: str
    ) -> bytes | None:
        base = _ARCHIVE_BASE.format(cik=cik, accession=accession.replace("-", ""))
        stem = primary_document.rsplit(".", 1)[0]
        derived_url = f"{base}/{stem}_htm.xml"
        resp = self._get(derived_url, str(cik), allow_404=True)
        if resp is not None and resp.status_code == 200:
            return resp.content
        # Fall back to the directory listing to find the instance by suffix.
        instance_name = self._instance_name_from_index(base, str(cik))
        if instance_name is None:
            logger.info(
                "revenue-segments: no XBRL instance found for CIK %s filing %s",
                cik,
                accession,
            )
            return None
        return self._get(f"{base}/{instance_name}", str(cik)).content

    def _instance_name_from_index(self, base: str, label: str) -> str | None:
        payload = self._get_json(f"{base}/index.json", label)
        items = payload.get("directory", {}).get("item", [])
        candidates = [
            item["name"]
            for item in items
            if isinstance(item.get("name"), str) and item["name"].endswith("_htm.xml")
        ]
        return candidates[0] if candidates else None

    def _get(
        self, url: str, label: str, *, allow_404: bool = False
    ) -> httpx.Response | None:
        self._pace()
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(label, f"SEC request failed: {exc}") from exc
        if resp.status_code == 404 and allow_404:
            return resp
        if resp.status_code != 200:
            raise StockDataUnavailable(
                label, f"SEC returned HTTP {resp.status_code} for {url}"
            )
        return resp

    def _get_json(self, url: str, label: str) -> dict:
        resp = self._get(url, label)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(label, f"SEC returned non-JSON for {url}") from exc
        return payload if isinstance(payload, dict) else {}

    def _pace(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


# ── parsing (pure — no HTTP, exercised directly by the tests) ─────────────────────────────


@dataclass(frozen=True)
class _Fact:
    year: int
    period_end: date | None
    rank: int
    value: float
    members: dict[str, str]  # axis local-name -> member local-name


def _parse_revenue_segments(xml_bytes: bytes) -> tuple[RevenueSegment, ...]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ()

    contexts = _parse_contexts(root)
    facts = _annual_revenue_facts(root, contexts)

    segments: list[RevenueSegment] = []
    for axis, axis_xbrl in _AXIS_XBRL.items():
        for year, member, period_end, value in _aggregate_axis(facts, axis_xbrl):
            segments.append(
                RevenueSegment(
                    fiscal_year=year,
                    period_end=period_end,
                    axis=axis,
                    member=member,
                    value=value,
                )
            )
    return tuple(segments)


def _annual_revenue_facts(
    root: ET.Element, contexts: dict[str, _Context]
) -> list[_Fact]:
    facts: list[_Fact] = []
    for el in root.iter():
        rank = _REVENUE_TAGS.get(_local_tag(el.tag))
        if rank is None:
            continue
        context = contexts.get(el.get("contextRef"))
        if context is None or not context.members:
            continue
        if context.start is None or context.end is None:
            continue  # need a duration
        if (context.end - context.start).days < _MIN_DURATION_DAYS:
            continue  # a quarterly (or shorter) fact — this slice is annual
        value = _parse_number(el.text)
        if value is None:
            continue
        facts.append(
            _Fact(
                year=context.end.year,
                period_end=context.end,
                rank=rank,
                value=value,
                members=dict(context.members),
            )
        )
    return facts


def _aggregate_axis(
    facts: list[_Fact], axis_xbrl: str
) -> list[tuple[int, str, date | None, float]]:
    allowed = {axis_xbrl, _BUSINESS_AXIS}
    # (year, member) -> {"flat": [_Fact], "nested": {segment_member: [_Fact]}}
    groups: dict[tuple[int, str], dict] = {}
    for fact in facts:
        axes = set(fact.members)
        if axis_xbrl not in axes or not axes <= allowed:
            continue
        member = fact.members[axis_xbrl]
        group = groups.setdefault((fact.year, member), {"flat": [], "nested": {}})
        if len(fact.members) == 1:
            group["flat"].append(fact)
        else:
            segment = fact.members.get(_BUSINESS_AXIS)
            if segment is not None:
                group["nested"].setdefault(segment, []).append(fact)

    resolved: list[tuple[int, str, date | None, float]] = []
    for (year, member), group in groups.items():
        if group["flat"]:
            best = min(group["flat"], key=lambda f: f.rank)
            resolved.append((year, member, best.period_end, best.value))
        elif group["nested"]:
            total = 0.0
            period_end: date | None = None
            for segment_facts in group["nested"].values():
                best = min(segment_facts, key=lambda f: f.rank)
                total += best.value
                period_end = best.period_end
            resolved.append((year, member, period_end, total))
    return resolved


def _parse_contexts(root: ET.Element) -> dict[str, _Context]:
    contexts: dict[str, _Context] = {}
    for ctx in root.iter():
        if _local_tag(ctx.tag) != "context":
            continue
        cid = ctx.get("id")
        if cid is None:
            continue
        members: list[tuple[str, str]] = []
        start = end = instant = None
        for el in ctx.iter():
            tag = _local_tag(el.tag)
            if tag == "explicitMember":
                dimension = _local_name(el.get("dimension", ""))
                member = _local_name((el.text or "").strip())
                if dimension and member:
                    members.append((dimension, member))
            elif tag == "startDate":
                start = _parse_date(el.text)
            elif tag == "endDate":
                end = _parse_date(el.text)
            elif tag == "instant":
                instant = _parse_date(el.text)
        contexts[cid] = _Context(tuple(members), start, end, instant)
    return contexts


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _local_name(qname: str) -> str:
    return qname.rsplit(":", 1)[-1] if ":" in qname else qname


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None
