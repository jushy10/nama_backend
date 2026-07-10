"""Shared yfinance helper: normalize a foreign issuer's mixed-currency earnings figures
onto its trading currency.

A foreign issuer trading in the US as an ADR (TSM, TM, BABA, ASML, …) reports in one
currency but trades in another, and Yahoo hands back its earnings surfaces in a *mix* of
the two — while the rest of this app assumes a single currency throughout (the quote,
market cap, and every EPS/revenue field are taken as the trading currency). Left as-is,
one earnings timeline splices together figures ~32x apart (TWD→USD) or ~150x (JPY→USD):
the classic "TSM annual ``eps_actual`` of 331 sitting next to a forward estimate of 16".

Which surface is in which currency is only partly fixed (verified against TSM/TM/BABA/ASML):

- **reliably reporting currency** (``info['financialCurrency']`` — TWD/JPY/CNY/EUR): the
  financial statements (``income_stmt`` / ``quarterly_income_stmt`` — EPS, revenue, net
  income) and the ``revenue_estimate`` frame. Statements are *always* in the filing
  currency, and revenue has no per-ADR convention, so this is a hard fact.
- **reliably trading currency** (``info['currency']`` — USD): the quote and
  ``info['forwardEps']`` / ``info['trailingEps']`` (they pair with the USD price for the P/E).
- **per-issuer-varying**: every *market* EPS surface — ``earnings_dates`` (the reported
  actual, its preceding estimate, and the summed annual consensus) *and* ``earnings_estimate``
  (the forward EPS). Yahoo quotes these per-ADR in the trading currency for some issuers
  (TSM, TM: USD) but in the reporting currency for others (BABA: CNY). So their currency is
  **detected**, not assumed — and detected *once* per issuer (the market surfaces share a
  currency), by comparing the forward annual estimate (``earnings_estimate`` ``0y``) against
  ``info['forwardEps']`` (a reliable trading-currency reference at the same scale).

This helper fetches the reporting→trading FX rate once and exposes two conversions: an
unconditional one for the reliably-reporting-currency figures, and one gated on the detected
market-EPS currency. A **single current-spot rate** is applied to every period on purpose: it
leaves every year-over-year *ratio* (the growth figures, the PEGs) exactly unchanged, trading
a few percent of absolute-level accuracy on older years for growth that carries no FX drift.

Best-effort, never-worse: when the issuer already trades in its reporting currency (every US
company — ``financialCurrency == currency``), or ``info`` / the FX rate can't be read, the
normalizer is the **identity**. Importing yfinance here is fine — this is adapter-layer
infrastructure, the only place allowed to know the vendor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.stocks.adapters import yfinance_session

# Only *detect* the market-EPS currency when the two currencies are at least this far apart in
# log-space (~ln(1.35), i.e. >~35%). Below it — EUR, GBP, CHF, CAD against USD — the pair is
# too close to tell apart by magnitude *and* the residual error is immaterial, so the market
# surfaces are left on the trading-currency assumption. The *reliably* reporting-currency
# fields are still converted (a small, unambiguous adjustment).
_DETECT_MIN_LOG_FX = 0.30


@dataclass(frozen=True)
class CurrencyNormalizer:
    """Converts an issuer's earnings figures onto its trading currency.

    Two multipliers, both reporting→trading:

    - ``fx`` — applied unconditionally to the *reliably reporting-currency* figures (the
      income-statement rows and ``revenue_estimate``).
    - ``market_fx`` — applied to the *market* EPS surfaces (``earnings_dates`` +
      ``earnings_estimate``), whose currency varies by issuer: it equals ``fx`` when those
      surfaces were detected to be in the reporting currency, and ``1.0`` when they're already
      in the trading currency (or the detection couldn't be made).

    ``1.0`` on both is the identity used for domestic issuers and whenever the rate is
    unavailable, so every method is then a no-op.
    """

    fx: float = 1.0
    market_fx: float = 1.0

    @property
    def is_identity(self) -> bool:
        """Whether this normalizer leaves every value unchanged (a domestic issuer, or an
        unavailable FX rate — either implies both multipliers are ``1.0``)."""
        return self.fx == 1.0 and self.market_fx == 1.0

    def to_trading(self, value: float | None) -> float | None:
        """A reliably reporting-currency figure → trading currency.

        For the fields Yahoo always denominates in the filing currency: the income-statement
        rows (EPS actual, revenue, net income) and the ``revenue_estimate``. A no-op under the
        identity normalizer, and ``None`` passes through untouched."""
        if value is None:
            return None
        return value * self.fx

    def market_to_trading(self, value: float | None) -> float | None:
        """A *market* EPS figure (from ``earnings_dates`` or ``earnings_estimate``) → trading
        currency, applying ``market_fx`` — i.e. converting only for an issuer whose market
        surfaces were detected to be in the reporting currency (BABA), and leaving alone one
        whose are already in the trading currency (TSM). ``None`` passes through."""
        if value is None:
            return None
        return value * self.market_fx


def read_info(ticker) -> dict:
    """A ticker's ``info`` dict, best-effort — ``{}`` on any failure.

    Enrichment for the earnings adapters (it drives currency normalization, and the annual
    slice also reads ``nextFiscalYearEnd`` / ``forwardEps`` from it), so a blocked or
    reshaped ``info`` must degrade quietly rather than sink the timeline."""
    try:
        info = yfinance_session.call(lambda: ticker.info)
    except Exception:  # noqa: BLE001 — info is optional enrichment
        return {}
    return info if isinstance(info, dict) else {}


def build(
    ticker_factory,
    info: dict | None,
    *,
    market_eps_sample: float | None = None,
    market_eps_reference: float | None = None,
) -> CurrencyNormalizer:
    """A normalizer for the issuer described by ``info``.

    The identity when ``info`` is missing/unreadable, when ``financialCurrency == currency``
    (a domestic issuer — the overwhelming common case, so no FX call is made for it), or when
    the FX rate can't be fetched — the best-effort, never-worse contract. Otherwise fetches
    the reporting→trading spot rate from Yahoo's ``{reporting}{trading}=X`` pair (built through
    the same ``ticker_factory`` the calling adapter uses, so tests fake it the same way) and
    resolves ``market_fx`` by detecting the market-EPS currency: ``market_eps_sample`` is a
    forward annual EPS from the market surface (``earnings_estimate`` ``0y``) and
    ``market_eps_reference`` a trading-currency EPS at the same scale (``info['forwardEps']``)."""
    if not isinstance(info, dict):
        return CurrencyNormalizer()
    reporting = info.get("financialCurrency")
    trading = info.get("currency")
    if not reporting or not trading or reporting == trading:
        return CurrencyNormalizer()
    fx = _fetch_fx_rate(ticker_factory, reporting, trading)
    if fx is None:
        return CurrencyNormalizer()
    market_fx = _resolve_market_fx(fx, market_eps_sample, market_eps_reference)
    return CurrencyNormalizer(fx=fx, market_fx=market_fx)


def _resolve_market_fx(
    fx: float, sample: float | None, reference: float | None
) -> float:
    """Whether the market EPS surfaces are in the reporting currency → the multiplier to
    convert them (``fx``), else ``1.0`` (already trading currency).

    Classifies by magnitude: ``sample`` (a market forward annual EPS) sits closer, in
    log-space, to ``reference / fx`` (the reporting-currency hypothesis) than to ``reference``
    (the trading-currency hypothesis). Only discriminates when the currencies are far enough
    apart to tell apart by magnitude (``_DETECT_MIN_LOG_FX``) and both inputs are usable;
    otherwise assumes trading currency (``1.0``), the safe default for the near-parity
    currencies the guard excludes."""
    if abs(math.log(fx)) < _DETECT_MIN_LOG_FX:
        return 1.0
    if not sample or not reference:
        return 1.0
    reference = abs(reference)
    distance_if_trading = abs(math.log(abs(sample) / reference))
    distance_if_reporting = abs(math.log(abs(sample) * fx / reference))
    return fx if distance_if_reporting < distance_if_trading else 1.0


def _fetch_fx_rate(ticker_factory, reporting: str, trading: str) -> float | None:
    """The reporting→trading spot rate from Yahoo's ``{reporting}{trading}=X`` pair, or
    ``None`` (unavailable). Tries the lightweight ``fast_info`` last price first, then a
    short ``history`` close. Best-effort — any failure yields ``None`` (→ identity)."""
    pair = f"{reporting}{trading}=X"
    try:
        fx_ticker = ticker_factory(pair)
    except Exception:  # noqa: BLE001 — vendor boundary
        return None
    rate = _fast_info_last_price(fx_ticker)
    if rate is not None:
        return rate
    return _history_last_close(fx_ticker)


def _fast_info_last_price(fx_ticker) -> float | None:
    """The FX pair's ``fast_info`` last price (the cheap, no-download read), or ``None``."""
    try:
        fast_info = yfinance_session.call(lambda: fx_ticker.fast_info)
    except Exception:  # noqa: BLE001 — vendor boundary
        return None
    return _positive_float(_lookup(fast_info, "last_price"))


def _history_last_close(fx_ticker) -> float | None:
    """The FX pair's most recent daily close — the fallback when ``fast_info`` is empty."""
    try:
        frame = yfinance_session.call(lambda: fx_ticker.history(period="5d"))
    except Exception:  # noqa: BLE001 — vendor boundary
        return None
    try:
        if frame is None or getattr(frame, "empty", True) or "Close" not in frame.columns:
            return None
        closes = frame["Close"].dropna()
        return _positive_float(closes.iloc[-1]) if len(closes) else None
    except Exception:  # noqa: BLE001 — never let a frame quirk escape the adapter
        return None


def _lookup(obj, key: str):
    """One value from a mapping-like or attribute-bearing object (``fast_info`` is both a
    ``Mapping`` in real yfinance and a plain dict in tests), or ``None``."""
    if obj is None:
        return None
    try:
        if hasattr(obj, "get"):
            return obj.get(key)
        return obj[key]
    except Exception:  # noqa: BLE001
        return getattr(obj, key, None)


def _positive_float(value) -> float | None:
    """A scalar → a positive float, or ``None`` (missing, non-numeric, NaN, non-positive).
    A non-positive FX rate is meaningless, so it's rejected the same as a missing one."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(rate) or rate <= 0:
        return None
    return rate
