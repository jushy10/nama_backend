from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

import httpx

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.company.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)
from app.stocks.company.insider_transactions.interfaces import InsiderTransactionsAdapter

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


class InsiderTransactionsAdapterImpl(InsiderTransactionsAdapter):
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
            if not isinstance(row, dict):
                continue  # a malformed/null row must not crash the whole map build
            ticker = row.get("ticker")
            cik = row.get("cik_str")
            if isinstance(ticker, str) and isinstance(cik, int):
                mapping[ticker.upper()] = cik
        return mapping

    def _recent_form4_filings(self, cik: int) -> list[tuple[str, str, date]]:
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

    def _get(self, url: str, label: str) -> httpx.Response:
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
class _ParsedTxn:
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
    if text is None:
        return None
    return text[:_MAX_TEXT_LEN]


def _first_text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    found = element.find(path)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _bool_text(element: ET.Element | None, path: str) -> bool:
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
    if text is None:
        return None
    stripped = text.strip().replace(",", "")
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None
