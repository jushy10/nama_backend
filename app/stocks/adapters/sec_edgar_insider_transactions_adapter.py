"""Interface Adapter: insider transactions from SEC EDGAR (Form 4).

The only module that knows SEC EDGAR backs the insider-transactions slice; swap it for another
``InsiderTransactionsProvider`` and only this file changes. It translates a stock's recent
**Form 4** filings — the two-business-day disclosures an officer, director, or 10%+ owner must
file when they trade their own company's stock — into ``InsiderTransaction`` entities.

**Keyless**, like the SEC revenue-segments source and unlike the paid insider APIs: EDGAR is a
public government open-data service that welcomes programmatic reads from data-centre IPs — it
works from Fargate where Yahoo's endpoints block us. Its only ask is a descriptive ``User-Agent``
(a blank one is refused) and staying under ~10 requests/second, which the read-path pacing
respects.

The walk: ticker -> CIK (``company_tickers.json``) -> the filer's recent Form 4s
(``submissions``) -> each filing's raw ownership XML -> parse its non-derivative transactions.
Unlike the revenue-segments source's clean JSON APIs, the transaction detail lives only in each
Form 4's own XML instance, so this fetches one document per filing (bounded to the most recent
``_MAX_FILINGS``) — each **best-effort**, so one unreadable filing is skipped, not fatal.

Only **non-derivative** transactions are read — the actual buys and sells of the stock. The
derivative table (option grants / exercises) is deliberately out of scope: it's compensation
plumbing, not the "big buy / big sell" this feature is about.

The ``_http`` attribute is the fake seam the offline tests swap; ``_parse_form4`` is a pure
function they exercise directly on a canned ownership document.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

import httpx

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider

logger = logging.getLogger(__name__)

# SEC asks automated clients to identify themselves; a blank User-Agent is refused.
_USER_AGENT = "nama-backend/1.0 (insider-transactions sync; +https://namainsights.com)"

# The ticker -> CIK map and the per-company submissions index / filing archive.
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"

# How many of the most-recent Form 4 filings to read per stock. Each is a separate document
# fetch, so this bounds both the per-read latency and the round-trips at EDGAR — a couple dozen
# filings already spans a long window of activity for most companies (each filing can carry
# several transactions).
_MAX_FILINGS = 25

# Belt to the read path's suspenders: a minimum spacing between SEC requests so a fast host
# doesn't exceed EDGAR's ~10 req/s fair-use ceiling. 0 in the adapter default (tests never
# sleep); the production wiring dials it up.
_DEFAULT_MIN_REQUEST_INTERVAL = 0.0

# Clip the free-text fields (insider name, officer title, security title) to the width of their
# DB columns so a pathological outlier can never overflow the column on Postgres — a swallowed
# cache write would otherwise silently stop the stock from ever caching (no cron here to recover),
# forcing a live 25-filing EDGAR walk on every read. Real values are far shorter; this is a guard.
_MAX_TEXT_LEN = 255


class SecEdgarInsiderTransactionsProvider(InsiderTransactionsProvider):
    """Reads a stock's recent insider transactions from its Form 4 filings on SEC EDGAR (keyless)."""

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

    # ── the port ──────────────────────────────────────────────────────────────────────────

    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        cik = self._cik_for(symbol)  # raises StockNotFound when unmapped
        filings = self._recent_form4_filings(cik)
        transactions: list[InsiderTransaction] = []
        for accession, primary_document, filing_date in filings:
            xml_bytes = self._filing_xml(cik, accession, primary_document)
            if xml_bytes is None:
                continue  # best-effort per filing: an unreadable one is skipped, not fatal
            for parsed in _parse_form4(xml_bytes):
                transactions.append(
                    InsiderTransaction(
                        filing_date=filing_date,
                        transaction_date=parsed.transaction_date,
                        insider_name=parsed.insider_name,
                        officer_title=parsed.officer_title,
                        is_director=parsed.is_director,
                        is_officer=parsed.is_officer,
                        is_ten_percent_owner=parsed.is_ten_percent_owner,
                        security_title=parsed.security_title,
                        transaction_code=parsed.transaction_code,
                        acquired_disposed=parsed.acquired_disposed,
                        shares=parsed.shares,
                        price_per_share=parsed.price_per_share,
                        shares_owned_following=parsed.shares_owned_following,
                        accession_number=accession,
                        # The transaction's ordinal in the filing's nonDerivativeTable (assigned in
                        # _parse_form4), NOT its position among successfully-parsed lines — so the
                        # (accession, line_index) cache key is stable even if a later parser change
                        # keeps/drops different lines, and never re-inserts an existing transaction.
                        line_index=parsed.line_index,
                    )
                )
        # Newest first, matching the DB repository's serving order *exactly* so a live-served
        # response and a cache-served one are identical regardless of cache state: transaction
        # date (falling back to the filing date) desc, then filing date desc, accession desc, and
        # document order (line_index) asc within a filing. The ``-line_index`` flips just that
        # last leg back to ascending under the overall ``reverse``.
        transactions.sort(
            key=lambda t: (
                t.transaction_date or t.filing_date,
                t.filing_date,
                t.accession_number,
                -t.line_index,
            ),
            reverse=True,
        )
        return InsiderActivity(symbol=symbol, transactions=tuple(transactions))

    # ── SEC walk steps ────────────────────────────────────────────────────────────────────

    def _cik_for(self, symbol: str) -> int:
        """Resolve a ticker to its CIK via the cached ticker map. ``StockNotFound`` when the
        ticker maps to no SEC filer (delisted, foreign-only, or simply absent)."""
        if self._ticker_cik is None:
            self._ticker_cik = self._load_ticker_map()
        cik = self._ticker_cik.get(symbol.upper())
        if cik is None:
            raise StockNotFound(symbol)
        return cik

    def _load_ticker_map(self) -> dict[str, int]:
        """Fetch ``company_tickers.json`` and build ``{ticker: cik}``. A failure here sinks the
        request (the map is required to resolve any symbol), so it raises ``StockDataUnavailable``."""
        payload = self._get_json(_COMPANY_TICKERS_URL, "*")
        mapping: dict[str, int] = {}
        # The file is a JSON object of positional rows: {"0": {"cik_str", "ticker", "title"}, …}.
        for row in payload.values():
            if not isinstance(row, dict):
                continue  # a malformed/null row must not crash the whole map build
            ticker = row.get("ticker")
            cik = row.get("cik_str")
            if isinstance(ticker, str) and isinstance(cik, int):
                mapping[ticker.upper()] = cik
        return mapping

    def _recent_form4_filings(self, cik: int) -> list[tuple[str, str, date]]:
        """The most-recent Form 4 filings as ``(accession, primary_document, filing_date)``,
        newest first and capped to ``_MAX_FILINGS``. ``submissions`` lists recent filings
        newest-first as parallel arrays; a filing missing a parseable filing date is skipped
        (there'd be nothing to order or serve it by)."""
        payload = self._get_json(_SUBMISSIONS_URL.format(cik=cik), str(cik))
        # ``… or {}`` / ``… or []`` (not ``.get(k, default)``) so a present-but-null key in a
        # malformed 200 body degrades to empty rather than raising an unmapped AttributeError
        # (which would escape the endpoint's domain-exception handlers as a 500).
        filings = payload.get("filings") or {}
        recent = filings.get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        documents = recent.get("primaryDocument") or []
        filing_dates = recent.get("filingDate") or []
        out: list[tuple[str, str, date]] = []
        for i, form in enumerate(forms):
            if form != "4":
                continue
            # Guard against ragged arrays (they're parallel, but be defensive).
            if i >= len(accessions) or i >= len(documents):
                continue
            filing_date = _parse_date(filing_dates[i]) if i < len(filing_dates) else None
            if filing_date is None:
                continue
            out.append((accessions[i], documents[i], filing_date))
            if len(out) >= _MAX_FILINGS:
                break
        return out

    def _filing_xml(
        self, cik: int, accession: str, primary_document: str
    ) -> bytes | None:
        """Fetch one Form 4's raw ownership XML. ``submissions`` gives the *rendered* primary
        document (``xslF345X0N/<name>.xml``); the raw XML we parse is its basename at the
        accession root. **Best-effort**: a transport error, a non-200, or a missing document
        yields ``None`` (the filing is skipped) rather than sinking the whole feed."""
        base = _ARCHIVE_BASE.format(cik=cik, accession=accession.replace("-", ""))
        raw_doc = primary_document.rsplit("/", 1)[-1]  # drop the ``xslF345X0N/`` render prefix
        url = f"{base}/{raw_doc}"
        try:
            resp = self._get(url, str(cik))
        except StockDataUnavailable:
            logger.info(
                "insider-transactions: skipping unreadable Form 4 %s", url, exc_info=True
            )
            return None
        return resp.content

    # ── HTTP plumbing ─────────────────────────────────────────────────────────────────────

    def _get(self, url: str, label: str) -> httpx.Response:
        """GET ``url``, paced under EDGAR's rate ceiling. Maps transport failures and non-200
        responses to ``StockDataUnavailable`` (``label`` is the symbol/CIK for the message)."""
        self._pace()
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise StockDataUnavailable(label, f"SEC request failed: {exc}") from exc
        if resp.status_code != 200:
            raise StockDataUnavailable(
                label, f"SEC returned HTTP {resp.status_code} for {url}"
            )
        return resp

    def _get_json(self, url: str, label: str) -> dict:
        """GET ``url`` and parse it as a JSON object. A body that isn't JSON is a source failure."""
        resp = self._get(url, label)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StockDataUnavailable(label, f"SEC returned non-JSON for {url}") from exc
        return payload if isinstance(payload, dict) else {}

    def _pace(self) -> None:
        """Sleep just enough to keep successive SEC requests at/under the configured spacing —
        a no-op when the interval is 0 (the test default)."""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


# ── parsing (pure — no HTTP, exercised directly by the tests) ─────────────────────────────


@dataclass(frozen=True)
class _ParsedTxn:
    """One non-derivative transaction parsed from a Form 4, before the caller stamps the filing
    accession and filing date onto it. ``line_index`` is the transaction's ordinal in the
    filing's ``nonDerivativeTable`` (its raw position, not its position among the kept lines), so
    the ``(accession, line_index)`` cache key stays stable across parser changes."""

    transaction_date: date | None
    insider_name: str
    officer_title: str | None
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    security_title: str | None
    transaction_code: str
    acquired_disposed: str | None
    shares: float | None
    price_per_share: float | None
    shares_owned_following: float | None
    line_index: int


def _parse_form4(xml_bytes: bytes) -> list[_ParsedTxn]:
    """Extract the non-derivative transactions from a Form 4 ownership document.

    The Form 4 XML carries **no namespace** (plain ``<ownershipDocument>`` tags), so the paths
    here are literal. Reads the reporting owner (name + relationship flags + officer title) once,
    then each ``<nonDerivativeTransaction>`` (code, shares, price, acquired/disposed, resulting
    holding). A transaction missing its code is dropped (nothing to classify); a price reported
    only as a footnote reference (no ``<value>``) parses to ``None`` (best-effort value). A
    malformed document yields an empty list rather than raising — one bad filing shouldn't sink
    the feed. Only the first reporting owner is read (joint filings are rare)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    owner = root.find("reportingOwner")
    insider_name = _first_text(owner, "reportingOwnerId/rptOwnerName") if owner is not None else None
    relationship = (
        owner.find("reportingOwnerRelationship") if owner is not None else None
    )
    officer_title = _first_text(relationship, "officerTitle")
    is_director = _bool_text(relationship, "isDirector")
    is_officer = _bool_text(relationship, "isOfficer")
    is_ten_percent_owner = _bool_text(relationship, "isTenPercentOwner")

    table = root.find("nonDerivativeTable")
    if table is None:
        return []

    # Name/title clipped once (they're constant across the filing's transactions).
    insider_name = _clip(insider_name or "Unknown")
    officer_title = _clip(officer_title)

    transactions: list[_ParsedTxn] = []
    # Enumerate over *all* transactions so ``line_index`` is the raw table ordinal — a code-less
    # line still consumes its index, keeping the (accession, line_index) key stable even as the
    # kept-line set changes.
    for idx, tx in enumerate(table.findall("nonDerivativeTransaction")):
        code = _first_text(tx, "transactionCoding/transactionCode")
        if not code:
            continue  # nothing to classify without a transaction code
        transactions.append(
            _ParsedTxn(
                transaction_date=_parse_date(_first_text(tx, "transactionDate/value")),
                insider_name=insider_name,
                officer_title=officer_title,
                is_director=is_director,
                is_officer=is_officer,
                is_ten_percent_owner=is_ten_percent_owner,
                security_title=_clip(_first_text(tx, "securityTitle/value")),
                transaction_code=code,
                acquired_disposed=_first_text(
                    tx, "transactionAmounts/transactionAcquiredDisposedCode/value"
                ),
                shares=_parse_number(
                    _first_text(tx, "transactionAmounts/transactionShares/value")
                ),
                price_per_share=_parse_number(
                    _first_text(tx, "transactionAmounts/transactionPricePerShare/value")
                ),
                shares_owned_following=_parse_number(
                    _first_text(
                        tx,
                        "postTransactionAmounts/sharesOwnedFollowingTransaction/value",
                    )
                ),
                line_index=idx,
            )
        )
    return transactions


def _clip(text: str | None) -> str | None:
    """Bound a free-text field to the DB column width so an outlier can't overflow the column on
    Postgres (see ``_MAX_TEXT_LEN``). ``None`` passes through unchanged."""
    if text is None:
        return None
    return text[:_MAX_TEXT_LEN]


def _first_text(element: ET.Element | None, path: str) -> str | None:
    """The stripped text at ``path`` under ``element`` (a limited-XPath ``a/b/c``), or ``None``
    when the element/path/text is absent or blank."""
    if element is None:
        return None
    found = element.find(path)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _bool_text(element: ET.Element | None, path: str) -> bool:
    """A Form 4 boolean flag at ``path`` (``1``/``true`` -> True), defaulting to False when absent."""
    text = _first_text(element, path)
    return text is not None and text.lower() in {"1", "true"}


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _parse_number(text: str | None) -> float | None:
    """A numeric field's value (commas stripped). A nil/blank/non-numeric field — including a
    price reported only as a footnote reference (no ``<value>``) — yields ``None``."""
    if text is None:
        return None
    stripped = text.strip().replace(",", "")
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None
