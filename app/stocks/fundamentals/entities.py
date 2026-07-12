"""Entities for the fundamentals slice.

One frozen value object — the trailing fundamentals snapshot the sweep fetches for a stock
and lands on the shared ``stocks`` anchor. Vendor-agnostic on purpose: the yfinance adapter
maps Yahoo's ``.info`` onto these fields (and normalizes units + foreign-ADR currency), so the
domain never sees the vendor's names or quirks.

Two kinds of figure sit together here, split by *clock*:

- **Ratios served as-is** — ``gross_margin`` / ``operating_margin`` / ``net_margin`` /
  ``return_on_equity`` (percent), ``current_ratio``, ``debt_to_equity`` (a ratio), ``beta``.
  Currency-agnostic and slow (they move ~quarterly, on a filing).
- **Per-share *inputs*** — ``book_value_per_share`` / ``sales_per_share`` /
  ``dividend_per_share`` (trading currency). The reader divides the live quote into these to
  get P/B, P/S and the dividend yield, the same "store the input, price it live" split
  ``fcf_per_share`` and the quarterly TTM EPS use — so those price-derived ratios stay fresh
  without storing a stale snapshot.

Every field is optional: Yahoo covers tickers unevenly and this is best-effort enrichment, so
any unknown value is left ``None``.

It also carries the clean display ``name`` off the same ``.info`` read (Yahoo's
``longName`` / ``shortName``). That isn't a *fundamental*, but the sweep is the natural place to
keep the anchor's ``name`` column populated once the live Finnhub profile call is retired — the
repository fills it fill-once, so an already-named row is left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Fundamentals:
    """A stock's trailing fundamentals snapshot — what the fundamentals sweep lands on the
    anchor. Margins / ROE are percent; ``debt_to_equity`` a ratio; the per-share figures are in
    the stock's trading currency; ``name`` is the clean display name for the anchor."""

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
    name: str | None = None

    @property
    def is_empty(self) -> bool:
        """True when every field is ``None`` — a served-but-hollow ``.info`` that carried
        nothing at all (not even a name). The sync skips these (leaves the row unstamped so a
        later sweep retries) rather than stamping a stock as freshly-synced with nothing to
        show; an ``.info`` with only a name still upserts, so the anchor's name gets filled."""
        return all(getattr(self, f.name) is None for f in fields(self))
