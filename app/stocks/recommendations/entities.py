from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class RecommendationTrend:
    period: date  # first day of the month this snapshot covers
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int

    @property
    def total(self) -> int:
        return self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell

    @property
    def score(self) -> float | None:
        if self.total == 0:
            return None
        weighted = (
            self.strong_buy * 1
            + self.buy * 2
            + self.hold * 3
            + self.sell * 4
            + self.strong_sell * 5
        )
        return round(weighted / self.total, 2)

    @property
    def consensus(self) -> str | None:
        score = self.score
        if score is None:
            return None
        if score <= 1.5:
            return "Strong Buy"
        if score <= 2.5:
            return "Buy"
        if score <= 3.5:
            return "Hold"
        if score <= 4.5:
            return "Sell"
        return "Strong Sell"


@dataclass(frozen=True)
class AnalystPriceTargets:
    mean: float | None = None
    high: float | None = None
    low: float | None = None
    median: float | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.mean is None
            and self.high is None
            and self.low is None
            and self.median is None
        )

    def upside_percent(self, price: float | None) -> float | None:
        if self.mean is None or price is None or price <= 0:
            return None
        return round((self.mean - price) / price * 100, 2)


@dataclass(frozen=True)
class AnalystRecommendations:
    symbol: str
    trends: tuple[RecommendationTrend, ...] = ()
    price_targets: AnalystPriceTargets | None = None

    @property
    def is_empty(self) -> bool:
        return not self.trends

    @property
    def latest(self) -> RecommendationTrend | None:
        return self.trends[0] if self.trends else None

    @property
    def direction(self) -> str | None:
        if len(self.trends) < 2:
            return None
        latest = self.trends[0].score
        prior = self.trends[1].score
        if latest is None or prior is None:
            return None
        if latest < prior:
            return "upgraded"
        if latest > prior:
            return "downgraded"
        return "unchanged"


# --- Firm credibility: the analyst card's "top firms" ranking -----------------------------
#
# A curated, best→worst ranking of sell-side research firms by track record and standing,
# seeded from published analyst-accuracy league tables (TipRanks-style success-rate + average-
# return rankings). It is deliberately *subjective and editable* reference data — not derived
# from anything in this repo — and it exists so the analyst card can surface the most credible
# firms covering a stock instead of a raw newest-first event feed. The names here are the
# canonical labels; the messier strings the vendor actually publishes fold onto them through
# ``_FIRM_ALIASES`` + ``_normalize_firm`` (e.g. "B of A Securities" → "Bank of America").
FIRM_CREDIBILITY: tuple[str, ...] = (
    "KBW",
    "RBC Capital",
    "Evercore ISI",
    "UBS",
    "Truist",
    "Morgan Stanley",
    "Goldman Sachs",
    "JP Morgan",
    "Bank of America",
    "Bernstein",
    "MoffettNathanson",
    "Barclays",
    "Raymond James",
    "Jefferies",
    "Wells Fargo",
    "Baird",
    "Wolfe Research",
    "Piper Sandler",
    "Mizuho",
    "Deutsche Bank",
    "Citigroup",
    "BMO Capital",
    "TD Cowen",
    "Guggenheim",
    "Stifel",
    "Oppenheimer",
    "William Blair",
    "Needham",
    "Canaccord Genuity",
)

# Vendor label (normalized) → canonical name in ``FIRM_CREDIBILITY``, for the strings that don't
# already normalize onto a canonical one. Note KeyBanc is intentionally absent: "KeyBanc" is a
# different firm from "KBW" (Keefe, Bruyette & Woods) and must never fold onto it.
_FIRM_ALIASES: dict[str, str] = {
    "keefe bruyette and woods": "KBW",
    "evercore isi group": "Evercore ISI",
    "evercore": "Evercore ISI",
    "truist securities": "Truist",
    "truist financial": "Truist",
    "rbc capital markets": "RBC Capital",
    "rbc": "RBC Capital",
    "b of a securities": "Bank of America",
    "bofa securities": "Bank of America",
    "bofa": "Bank of America",
    "b of a": "Bank of America",
    "merrill lynch": "Bank of America",
    "jpmorgan": "JP Morgan",
    "j p morgan": "JP Morgan",
    "cowen": "TD Cowen",
    "cowen and co": "TD Cowen",
    "stifel nicolaus": "Stifel",
    "bmo capital markets": "BMO Capital",
    "bmo": "BMO Capital",
    "piper jaffray": "Piper Sandler",
    "moffett nathanson": "MoffettNathanson",
    "sanford c bernstein": "Bernstein",
    "sanford bernstein": "Bernstein",
    "alliancebernstein": "Bernstein",
    "canaccord": "Canaccord Genuity",
    "citi": "Citigroup",
    "robert w baird": "Baird",
    "goldman": "Goldman Sachs",
}


def _normalize_firm(name: str) -> str:
    lowered = (name or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", lowered).split())


_CREDIBILITY_RANK: dict[str, int] = {
    _normalize_firm(name): rank for rank, name in enumerate(FIRM_CREDIBILITY)
}


def _credibility_rank(firm: str) -> int | None:
    key = _normalize_firm(firm)
    canonical = _FIRM_ALIASES.get(key)
    if canonical is not None:
        key = _normalize_firm(canonical)
    return _CREDIBILITY_RANK.get(key)


@dataclass(frozen=True)
class FirmRating:
    firm: str
    rank: int
    rating: str | None
    action: str | None
    target: float | None
    published_at: date

    def upside_percent(self, price: float | None) -> float | None:
        if self.target is None or price is None or price <= 0:
            return None
        return round((self.target - price) / price * 100, 2)


@dataclass(frozen=True)
class RatingChange:
    firm: str
    published_at: date
    action: str | None = None
    from_grade: str | None = None
    to_grade: str | None = None
    target_current: float | None = None
    target_prior: float | None = None

    @property
    def is_upgrade(self) -> bool:
        return (self.action or "").strip().lower() == "up"

    @property
    def is_downgrade(self) -> bool:
        return (self.action or "").strip().lower() == "down"


@dataclass(frozen=True)
class AnalystRatingChanges:
    symbol: str
    changes: tuple[RatingChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def latest(self) -> RatingChange | None:
        return self.changes[0] if self.changes else None

    def top_credible_firms(
        self,
        limit: int = 10,
        *,
        as_of: date | None = None,
        max_age_days: int = 365,
    ) -> tuple[FirmRating, ...]:
        cutoff = as_of - timedelta(days=max_age_days) if as_of is not None else None
        seen: set[int] = set()
        ranked: list[FirmRating] = []
        for change in self.changes:  # newest first
            rank = _credibility_rank(change.firm)
            if rank is None or rank in seen:
                continue
            # First (newest) action for this firm: mark it seen so its older rows are skipped,
            # then drop the firm entirely when even that newest action is stale.
            seen.add(rank)
            if cutoff is not None and change.published_at < cutoff:
                continue
            ranked.append(
                FirmRating(
                    firm=change.firm,
                    rank=rank,
                    rating=change.to_grade,
                    action=change.action,
                    target=change.target_current,
                    published_at=change.published_at,
                )
            )
        ranked.sort(key=lambda fr: fr.rank)
        return tuple(ranked[: max(0, limit)])
