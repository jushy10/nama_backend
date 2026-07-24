from datetime import date

import pytest

from app.domains.shared.exceptions import StockDataUnavailable
from app.domains.pricing.options.entities import (
    CONTRACT_MULTIPLIER,
    ExpiryChain,
    OptionContract,
    OptionType,
    OptionsFlowSummary,
)
from app.domains.pricing.options.interfaces import OptionsChainAdapter
from app.domains.pricing.options.use_cases import GetOptionsFlow

_EXPIRY = date(2026, 7, 31)


def _c(strike, option_type=OptionType.CALL, **kw) -> OptionContract:
    return OptionContract(
        expiration=kw.pop("expiration", _EXPIRY),
        strike=strike,
        option_type=option_type,
        **kw,
    )


def test_mid_prefers_bid_ask_midpoint():
    assert _c(100, bid=2.8, ask=3.2, last_price=9.9).mid == pytest.approx(3.0)


def test_mid_falls_back_to_last_when_no_live_quote():
    assert _c(100, bid=None, ask=None, last_price=2.5).mid == 2.5


def test_mid_is_none_for_a_dead_quote():
    # Zero bid/ask and no last trade: no price, not a price of 0.
    assert _c(100, bid=0.0, ask=0.0, last_price=None).mid is None


def test_premium_is_mid_times_volume_times_lot():
    c = _c(100, bid=2.8, ask=3.2, volume=500)  # mid 3.0
    assert c.premium == pytest.approx(3.0 * 500 * CONTRACT_MULTIPLIER)


def test_premium_is_none_without_a_price_or_volume():
    assert _c(100, bid=None, ask=None, last_price=None, volume=500).premium is None
    assert _c(100, last_price=3.0, volume=None).premium is None
    assert _c(100, last_price=3.0, volume=0).premium is None


def test_volume_oi_ratio_and_unusual_flag():
    # More volume than open interest -> unusual (fresh positioning), ratio > 1.
    hot = _c(100, volume=1500, open_interest=500)
    assert hot.volume_oi_ratio == pytest.approx(3.0)
    assert hot.is_unusual is True
    # Volume within the standing book -> not unusual.
    calm = _c(100, volume=100, open_interest=500)
    assert calm.is_unusual is False


def test_unusual_needs_known_open_interest():
    # Unknown OI can't be judged; brand-new interest (OI 0 with real volume) counts.
    assert _c(100, volume=1000, open_interest=None).is_unusual is False
    fresh = _c(100, volume=1000, open_interest=0)
    assert fresh.is_unusual is True
    assert fresh.volume_oi_ratio is None  # undefined over zero, but still flagged


def test_summary_aggregates_per_side_and_derives_lean():
    contracts = [
        _c(100, OptionType.CALL, bid=1.9, ask=2.1, volume=100, open_interest=1000),  # mid 2.0
        _c(110, OptionType.CALL, bid=0.9, ask=1.1, volume=200, open_interest=500),   # mid 1.0
        _c(90, OptionType.PUT, bid=2.9, ask=3.1, volume=600, open_interest=400),     # mid 3.0
    ]
    s = OptionsFlowSummary.from_contracts(contracts)
    assert (s.call_volume, s.put_volume, s.total_volume) == (300, 600, 900)
    assert (s.call_open_interest, s.put_open_interest) == (1500, 400)
    # call premium = 2.0*100*100 + 1.0*200*100 = 40_000; put premium = 3.0*600*100 = 180_000.
    assert s.call_premium == pytest.approx(40_000)
    assert s.put_premium == pytest.approx(180_000)
    assert s.net_premium == pytest.approx(40_000 - 180_000)
    assert s.put_call_volume_ratio == pytest.approx(600 / 300)
    assert s.put_call_oi_ratio == pytest.approx(400 / 1500)


def test_summary_ratios_are_none_without_a_call_denominator():
    s = OptionsFlowSummary.from_contracts([_c(90, OptionType.PUT, volume=10, open_interest=10)])
    assert s.put_call_volume_ratio is None
    assert s.put_call_oi_ratio is None
    # Missing per-contract figures count as zero rather than voiding the totals.
    assert s.call_premium == 0.0 and s.put_premium == 0.0


def test_expiry_chain_sorts_sides_and_ranks_unusual_by_premium():
    small = _c(100, OptionType.CALL, bid=0.9, ask=1.1, volume=200, open_interest=50)   # unusual, prem 20k
    big = _c(120, OptionType.CALL, bid=4.9, ask=5.1, volume=1000, open_interest=100)   # unusual, prem 500k
    calm = _c(110, OptionType.CALL, bid=2.0, ask=2.2, volume=10, open_interest=9000)   # not unusual
    put = _c(95, OptionType.PUT, bid=1.0, ask=1.2, volume=5000, open_interest=100)     # unusual, prem 550k
    chain = ExpiryChain(expiration=_EXPIRY, spot=110.0, contracts=(small, calm, big, put))

    assert [c.strike for c in chain.calls] == [100, 110, 120]  # ascending ladder
    assert [c.strike for c in chain.puts] == [95]
    # Unusual, most money first: put (550k) > big call (500k) > small call (20k); calm excluded.
    assert [c.strike for c in chain.unusual] == [95, 120, 100]
    assert chain.summary.total_volume == 200 + 1000 + 10 + 5000


class _FakeProvider(OptionsChainAdapter):
    def __init__(self, *, expirations=(), chains=None, error=None) -> None:
        self._expirations = tuple(expirations)
        self._chains = chains or {}
        self._error = error
        self.chain_requests: list[date] = []

    def get_expirations(self, symbol):
        if self._error is not None:
            raise self._error
        return self._expirations

    def get_chain(self, symbol, expiration):
        self.chain_requests.append(expiration)
        if self._error is not None:
            raise self._error
        return self._chains[expiration]


def _chain(expiration: date) -> ExpiryChain:
    return ExpiryChain(
        expiration=expiration,
        spot=110.0,
        contracts=(_c(110, OptionType.CALL, volume=1, open_interest=1, expiration=expiration),),
    )


def _today():
    return date(2026, 7, 20)


def test_default_selects_nearest_upcoming_expiry():
    near, far = date(2026, 7, 31), date(2026, 9, 18)
    provider = _FakeProvider(
        expirations=(near, far), chains={near: _chain(near), far: _chain(far)}
    )
    flow = GetOptionsFlow(provider, today=_today).run("aapl")
    assert flow.symbol == "AAPL"  # normalized
    assert provider.chain_requests == [near]  # only the shown expiry is fetched
    assert flow.chain.expiration == near
    assert flow.expirations == (near, far)  # full list served for the client's selector


def test_explicit_expiration_is_honored():
    near, far = date(2026, 7, 31), date(2026, 9, 18)
    provider = _FakeProvider(
        expirations=(near, far), chains={near: _chain(near), far: _chain(far)}
    )
    flow = GetOptionsFlow(provider, today=_today).run("AAPL", expiration=far)
    assert provider.chain_requests == [far]
    assert flow.chain.expiration == far


def test_unknown_expiration_is_rejected():
    near = date(2026, 7, 31)
    provider = _FakeProvider(expirations=(near,), chains={near: _chain(near)})
    with pytest.raises(ValueError):
        GetOptionsFlow(provider, today=_today).run("AAPL", expiration=date(2026, 1, 1))


def test_no_listed_options_is_an_empty_flow_not_an_error():
    provider = _FakeProvider(expirations=())
    flow = GetOptionsFlow(provider, today=_today).run("ZZZZ")
    assert flow.chain is None
    assert flow.expirations == ()
    assert provider.chain_requests == []  # nothing to fetch


def test_all_expiries_past_falls_back_to_the_latest():
    p1, p2 = date(2026, 6, 5), date(2026, 6, 19)  # both before _today()
    provider = _FakeProvider(expirations=(p1, p2), chains={p1: _chain(p1), p2: _chain(p2)})
    flow = GetOptionsFlow(provider, today=_today).run("AAPL")
    assert provider.chain_requests == [p2]  # the latest, not a failure


def test_bad_symbol_is_rejected():
    provider = _FakeProvider(expirations=())
    with pytest.raises(ValueError):
        GetOptionsFlow(provider, today=_today).run("123")


def test_vendor_failure_propagates():
    provider = _FakeProvider(error=StockDataUnavailable("AAPL", "blocked"))
    with pytest.raises(StockDataUnavailable):
        GetOptionsFlow(provider, today=_today).run("AAPL")
