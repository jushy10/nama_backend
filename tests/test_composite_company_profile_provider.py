"""Unit tests for the composite company-profile adapter.

The clean name and the description come from different vendors, merged behind one
port. Verifies the merge, that each source is optional, and that one source's
failure doesn't drop the other's field.
"""

from app.stocks.composite_company_profile_provider import (
    CompositeCompanyProfileProvider,
)
from app.stocks.entities import CompanyProfile
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.ports import CompanyProfileProvider


class FakeProfile(CompanyProfileProvider):
    def __init__(self, profile=None, raises: Exception | None = None):
        self._profile = profile
        self._raises = raises

    def get_profile(self, symbol: str) -> CompanyProfile:
        if self._raises is not None:
            raise self._raises
        return self._profile


def test_merges_name_from_one_and_description_from_other():
    name_src = FakeProfile(CompanyProfile(name="Apple Inc.", description="ignored"))
    desc_src = FakeProfile(CompanyProfile(name="ignored", description="Makes phones."))
    profile = CompositeCompanyProfileProvider(name_src, desc_src).get_profile("AAPL")
    assert profile.name == "Apple Inc."
    assert profile.description == "Makes phones."


def test_name_source_failure_does_not_drop_description():
    name_src = FakeProfile(raises=StockDataUnavailable("AAPL", "boom"))
    desc_src = FakeProfile(CompanyProfile(name=None, description="Makes phones."))
    profile = CompositeCompanyProfileProvider(name_src, desc_src).get_profile("AAPL")
    assert profile.name is None
    assert profile.description == "Makes phones."


def test_description_source_failure_does_not_drop_name():
    name_src = FakeProfile(CompanyProfile(name="Apple Inc.", description=None))
    desc_src = FakeProfile(raises=StockNotFound("AAPL"))
    profile = CompositeCompanyProfileProvider(name_src, desc_src).get_profile("AAPL")
    assert profile.name == "Apple Inc."
    assert profile.description is None


def test_missing_name_source_keeps_description():
    desc_src = FakeProfile(CompanyProfile(name=None, description="Makes phones."))
    profile = CompositeCompanyProfileProvider(None, desc_src).get_profile("AAPL")
    assert profile.name is None
    assert profile.description == "Makes phones."


def test_missing_description_source_keeps_name():
    name_src = FakeProfile(CompanyProfile(name="Apple Inc.", description=None))
    profile = CompositeCompanyProfileProvider(name_src, None).get_profile("AAPL")
    assert profile.name == "Apple Inc."
    assert profile.description is None


def test_both_sources_absent_yields_empty_profile():
    profile = CompositeCompanyProfileProvider(None, None).get_profile("AAPL")
    assert profile.name is None
    assert profile.description is None
