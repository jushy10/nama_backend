"""Entity tests for the universe slice — the tier-scoped industry P/E cohort.

``IndustryValuation.for_stock_peers`` is pure statistics over ``(pe, tier)`` pairs, so it
drives directly here with no repository. Covers the three outcomes the widening rule can
land on: a representative same-tier hit, a one-step widen to the neighbouring tier, and the
whole-industry fall-back — plus the cohort labels each produces.
"""

from app.stocks.universe.entities import IndustryValuation, MarketCapTier

MEGA = MarketCapTier.MEGA
LARGE = MarketCapTier.LARGE
MID = MarketCapTier.MID


def _peers(*specs):
    """Expand ``(tier, n)`` specs into ``n`` identical-tier peers, each with a distinct P/E."""
    peers = []
    pe = 10.0
    for tier, n in specs:
        for _ in range(n):
            peers.append((pe, tier))
            pe += 1.0
    return tuple(peers)


def test_same_tier_cohort_when_it_is_representative():
    # Five mega-caps clear MIN_REPRESENTATIVE_PEERS on their own, so the benchmark stays
    # scoped to the anchor's tier — the mid-caps present in the industry are not pulled in.
    peers = _peers((MEGA, 5), (MID, 4))
    v = IndustryValuation.for_stock_peers("semiconductors", MEGA, peers)
    assert v.cohort == "mega"
    assert v.count == 5  # only the mega-caps
    assert v.is_representative


def test_widens_one_tier_when_same_tier_is_thin():
    # Three mega-caps alone fall short, so the cohort widens to the nearest tier (large),
    # reaching a representative sample; the mid-caps stay out, so it isn't the whole industry.
    peers = _peers((MEGA, 3), (LARGE, 5), (MID, 10))
    v = IndustryValuation.for_stock_peers("semiconductors", MEGA, peers)
    assert v.cohort == "large/mega"
    assert v.count == 8  # mega + large, not the mid-caps
    assert v.is_representative


def test_falls_back_to_whole_industry_and_stays_thin():
    # No radius reaches five, so it widens all the way to the industry and reports what it has
    # — count below the gate, so the caller (the analysis path) omits it.
    peers = _peers((MEGA, 1), (LARGE, 2), (MID, 1))
    v = IndustryValuation.for_stock_peers("semiconductors", MEGA, peers)
    assert v.cohort == "industry"
    assert v.count == 4
    assert not v.is_representative


def test_no_anchor_tier_is_the_plain_industry_benchmark():
    # An unknown cap (None tier) can't anchor a size cohort, so it's the whole mid-cap-and-up
    # industry — the pre-tier behaviour.
    peers = _peers((MEGA, 3), (LARGE, 3))
    v = IndustryValuation.for_stock_peers("semiconductors", None, peers)
    assert v.cohort == "industry"
    assert v.count == 6


def test_does_not_over_widen_past_a_representative_same_tier():
    # A representative mid-cap sample is kept as-is — the larger tiers aren't folded in just
    # because they exist.
    peers = _peers((MID, 6), (MEGA, 6))
    v = IndustryValuation.for_stock_peers("semiconductors", MID, peers)
    assert v.cohort == "mid"
    assert v.count == 6
