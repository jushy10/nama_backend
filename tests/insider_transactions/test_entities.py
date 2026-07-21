from datetime import date

from app.stocks.insider_transactions.entities import (
    InsiderActivity,
    InsiderTransaction,
)


def _txn(
    *,
    code="P",
    acquired_disposed="A",
    shares=100.0,
    price=10.0,
    line_index=0,
    officer_title=None,
    is_director=False,
    is_officer=False,
    is_ten=False,
) -> InsiderTransaction:
    return InsiderTransaction(
        filing_date=date(2026, 6, 17),
        transaction_date=date(2026, 6, 15),
        insider_name="Jane Insider",
        officer_title=officer_title,
        is_director=is_director,
        is_officer=is_officer,
        is_ten_percent_owner=is_ten,
        security_title="Common Stock",
        transaction_code=code,
        acquired_disposed=acquired_disposed,
        shares=shares,
        price_per_share=price,
        shares_owned_following=1000.0,
        accession_number="0000000000-26-000001",
        line_index=line_index,
    )


def test_value_is_shares_times_price():
    assert _txn(shares=100, price=10).value == 1000


def test_value_is_none_when_price_is_missing():
    # A Form 4 can report a price only in a footnote (e.g. an option exercise) -> no value.
    assert _txn(price=None).value is None
    assert _txn(shares=None).value is None


def test_open_market_flags():
    buy = _txn(code="P")
    sell = _txn(code="S")
    grant = _txn(code="A")
    assert buy.is_open_market and buy.is_open_market_buy and not buy.is_open_market_sale
    assert sell.is_open_market and sell.is_open_market_sale and not sell.is_open_market_buy
    assert not grant.is_open_market  # a grant is compensation, not a conviction trade


def test_code_label_falls_back_to_the_raw_code():
    assert _txn(code="P").code_label == "Open-market purchase"
    assert _txn(code="S").code_label == "Open-market sale"
    assert _txn(code="ZZ").code_label == "ZZ"  # unknown code -> raw


def test_role_prefers_the_officer_title():
    assert _txn(officer_title="Chief Executive Officer").role == "Chief Executive Officer"
    assert _txn(is_officer=True).role == "Officer"
    assert _txn(is_director=True).role == "Director"
    assert _txn(is_ten=True).role == "10% Owner"
    assert _txn().role == "Insider"


def test_activity_is_empty_and_open_market_view():
    assert InsiderActivity("AAPL").is_empty
    activity = InsiderActivity(
        "AAPL",
        (_txn(code="P", line_index=0), _txn(code="M", line_index=1), _txn(code="S", line_index=2)),
    )
    assert not activity.is_empty
    codes = [t.transaction_code for t in activity.open_market]
    assert codes == ["P", "S"]  # the option exercise (M) is dropped from the conviction view


def test_summary_nets_open_market_buys_and_sells():
    activity = InsiderActivity(
        "AAPL",
        (
            _txn(code="P", shares=100, price=10, line_index=0),  # +1,000 buy
            _txn(code="P", shares=50, price=20, line_index=1),  # +1,000 buy
            _txn(code="S", shares=200, price=10, line_index=2),  # -2,000 sell
            _txn(code="A", shares=999, price=10, line_index=3),  # grant — excluded
            _txn(code="P", shares=None, price=10, line_index=4),  # counted, but no value
        ),
    )
    summary = activity.summary
    assert summary.open_market_buy_count == 3  # two priced buys + the valueless one
    assert summary.open_market_sell_count == 1
    assert summary.open_market_buy_value == 2000  # valueless buy contributes 0 to the value
    assert summary.open_market_sell_value == 2000
    assert summary.net_value == 0
