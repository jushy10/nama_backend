from app.domains.listings.universe.entities import (
    IndustryValuation,
    MarketCapTier,
    PeerCompany,
    PeerComparison,
)

MEGA = MarketCapTier.MEGA
LARGE = MarketCapTier.LARGE
MID = MarketCapTier.MID


def _peers(*specs):
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


# ─────────────────────── PeerComparison.build ───────────────────────
#
# The named side-by-side table. Like IndustryValuation it scopes to the anchor's cap tier and
# widens as needed — but returns the peer *rows* (not a P/E summary), marks the anchor, caps the
# list by market cap, and medians every metric over the cohort.


def _peer(ticker, tier, *, cap=1e12, pe=None, ev=None, fcf=None, nm=None, rg=None):
    return PeerCompany(
        ticker=ticker,
        name=f"{ticker} Inc.",
        market_cap=cap,
        pe_ratio=pe,
        ev_ebitda=ev,
        fcf_yield=fcf,
        net_margin=nm,
        revenue_growth_yoy=rg,
        tier=tier,
    )


def test_peers_scope_to_the_anchor_tier_when_representative():
    # Four mega-cap peers clear MIN_PEERS on their own, so the cohort stays mega — the large-caps
    # present in the industry are left out, and the anchor is split from the peer list.
    candidates = (
        _peer("ANCHOR", MEGA),
        *[_peer(f"M{i}", MEGA) for i in range(4)],
        *[_peer(f"L{i}", LARGE) for i in range(3)],
    )
    c = PeerComparison.build("ANCHOR", "semiconductors", candidates)

    assert c.cohort == "mega"
    assert c.anchor is not None and c.anchor.ticker == "ANCHOR" and c.anchor.is_anchor
    assert {p.ticker for p in c.peers} == {"M0", "M1", "M2", "M3"}  # large-caps excluded
    assert all(not p.is_anchor for p in c.peers)  # the anchor isn't repeated in the peer list


def test_peers_widen_a_tier_when_same_tier_is_thin():
    # Two mega peers fall short of MIN_PEERS, so the cohort widens to the neighbouring large tier.
    # The mid-caps stay out (so the cohort is a proper subset of the industry -> "large/mega",
    # not the whole industry).
    candidates = (
        _peer("ANCHOR", MEGA),
        *[_peer(f"M{i}", MEGA) for i in range(2)],
        *[_peer(f"L{i}", LARGE) for i in range(5)],
        *[_peer(f"D{i}", MID) for i in range(10)],
    )
    c = PeerComparison.build("ANCHOR", "semiconductors", candidates)

    assert c.cohort == "large/mega"
    assert len(c.peers) == 7  # 2 mega + 5 large; the mid-caps stay out


def test_peers_are_capped_by_market_cap_largest_first():
    # More same-tier peers than MAX_PEERS: keep the largest MAX_PEERS, ordered by market cap.
    candidates = (_peer("ANCHOR", MEGA, cap=9e12),) + tuple(
        _peer(f"M{i:02d}", MEGA, cap=float(i) * 1e11) for i in range(20)
    )
    c = PeerComparison.build("ANCHOR", "semiconductors", candidates)

    assert len(c.peers) == PeerComparison.MAX_PEERS  # capped
    caps = [p.market_cap for p in c.peers]
    assert caps == sorted(caps, reverse=True)  # largest first
    assert c.peers[0].ticker == "M19"  # the biggest peer leads


def test_medians_are_taken_over_the_cohort_including_the_anchor():
    # The median spans the anchor and its peers; a metric only some carry still yields one from
    # those that do, and a wholly-absent metric medians to None.
    candidates = (
        _peer("ANCHOR", MEGA, pe=50.0, rg=None),  # anchor carries a P/E, no growth
        _peer("M0", MEGA, pe=10.0, rg=5.0),
        _peer("M1", MEGA, pe=20.0, rg=15.0),
        _peer("M2", MEGA, pe=30.0),
        _peer("M3", MEGA, pe=40.0),
    )
    c = PeerComparison.build("ANCHOR", "semiconductors", candidates)

    assert c.medians.pe_ratio == 30.0  # median of [10,20,30,40,50] — anchor included
    assert c.medians.revenue_growth_yoy == 10.0  # median of [5,15] — the two that carry it
    assert c.medians.ev_ebitda is None  # no company carries it


def test_an_unknown_anchor_yields_the_whole_industry_with_no_anchor_row():
    # A ticker not among the screened industry members (unscreened / unknown): no tier to anchor
    # on, so the cohort is the whole industry and the anchor row is null — the peers still serve.
    candidates = tuple(_peer(f"M{i}", MEGA) for i in range(3))
    c = PeerComparison.build("GHOST", "semiconductors", candidates)

    assert c.anchor is None
    assert c.cohort == "industry"
    assert {p.ticker for p in c.peers} == {"M0", "M1", "M2"}


def test_no_industry_is_an_empty_comparison():
    # An unclassified stock (industry None, no candidates) builds an empty comparison, not an error.
    c = PeerComparison.build("ANCHOR", None, ())

    assert c.industry is None
    assert c.anchor is None
    assert c.peers == ()
    assert c.medians.pe_ratio is None
