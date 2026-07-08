"""The Alembic migrations build the schema the ORM models expect.

Runs the real migration chain against a throwaway SQLite database: upgrade to
head builds the tables/columns the models expect, downgrade to base tears them
down. Catches a migration that won't apply or has drifted from the model.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def alembic(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    monkeypatch.setenv("DATABASE_URL", url)  # env.py reads this at run time
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    return config, url


def test_upgrade_adds_index_membership_flags_to_stocks(alembic):
    # 0014 folds the S&P 500 / Nasdaq-100 membership flags onto the shared stocks anchor.
    config, url = alembic

    command.upgrade(config, "head")
    columns = {c["name"] for c in inspect(create_engine(url)).get_columns("stocks")}
    assert {"in_sp500", "in_nasdaq100"} <= columns

    command.downgrade(config, "base")
    assert "stocks" not in inspect(create_engine(url)).get_table_names()


def test_upgrade_creates_the_etfs_table(alembic):
    # 0016 adds the standalone `etfs` table backing the top-ETFs slice.
    config, url = alembic

    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    assert "etfs" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("etfs")}
    assert {"ticker", "net_assets", "expense_ratio", "category"} <= columns

    command.downgrade(config, "base")
    assert "etfs" not in inspect(create_engine(url)).get_table_names()


def test_upgrade_adds_the_etf_profile_columns_and_child_tables(alembic):
    # 0020 adds the profile scalars onto `etfs` and the two child tables the sync persists; 0021
    # then drops the trailing-return ladder (served live from Yahoo instead), so at head those
    # three columns are gone while the rest of the profile stays.
    config, url = alembic

    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    etf_columns = {c["name"] for c in inspector.get_columns("etfs")}
    assert {
        "fund_family",
        "dividend_yield",
        "description",
        "nav",
        "profile_fetched_at",
    } <= etf_columns
    # The trailing-return ladder was dropped by 0021 — no longer stored.
    assert not (
        {"ytd_return", "three_year_return", "five_year_return"} & etf_columns
    )
    tables = set(inspector.get_table_names())
    assert {"etf_sector_weightings", "etf_top_holdings"} <= tables

    command.downgrade(config, "base")
    remaining = set(inspect(create_engine(url)).get_table_names())
    assert not ({"etf_sector_weightings", "etf_top_holdings"} & remaining)
