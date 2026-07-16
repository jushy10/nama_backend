"""The Alembic migrations build the schema the ORM models expect.

Runs the real migration chain against a throwaway SQLite database: upgrade to
head builds the tables/columns the models expect, downgrade to base tears them
down. Catches a migration that won't apply or has drifted from the model.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

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


def test_upgrade_renames_trends_and_adds_analyst_coverage_tables(alembic):
    # 0023 renames stock_recommendation_trends -> stock_analyst_trends and adds the four
    # price-target columns; 0024 adds the sibling stock_analyst_rating_changes events table.
    config, url = alembic

    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    tables = set(inspector.get_table_names())
    assert "stock_analyst_trends" in tables
    assert "stock_recommendation_trends" not in tables  # renamed, not duplicated
    assert "stock_analyst_rating_changes" in tables
    trend_columns = {c["name"] for c in inspector.get_columns("stock_analyst_trends")}
    assert {
        "target_mean",
        "target_high",
        "target_low",
        "target_median",
    } <= trend_columns
    change_columns = {
        c["name"] for c in inspector.get_columns("stock_analyst_rating_changes")
    }
    assert {"firm", "published_at", "to_grade", "target_current"} <= change_columns

    command.downgrade(config, "base")
    remaining = set(inspect(create_engine(url)).get_table_names())
    assert not (
        {"stock_analyst_trends", "stock_analyst_rating_changes"} & remaining
    )


def test_upgrade_creates_the_congress_trades_table(alembic):
    # 0034 adds stock_congress_trades — the Congressional-trades cache off the stocks anchor.
    config, url = alembic

    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    assert "stock_congress_trades" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("stock_congress_trades")}
    assert {
        "member",
        "chamber",
        "party",
        "tx_type",
        "amount_range",
        "transaction_date",
        "disclosure_date",
    } <= columns

    command.downgrade(config, "base")
    assert "stock_congress_trades" not in inspect(create_engine(url)).get_table_names()


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


def test_0041_purges_ne_cdr_rows_from_stocks(alembic):
    # 0041 deletes the Cboe Canada (.NE) CDR rows the old CA screen wrote onto the anchor,
    # leaving TSX (.TO) names — and a lookalike like STONE (no dotted suffix) — untouched.
    config, url = alembic
    command.upgrade(config, "0040_domicile_country")

    engine = create_engine(url)
    with engine.begin() as conn:
        for i, ticker in enumerate(["INTC.NE", "ZAAP.NE", "SHOP.TO", "STONE"]):
            conn.execute(
                text("INSERT INTO stocks (id, ticker) VALUES (:id, :t)"),
                {"id": f"{i:032d}", "t": ticker},
            )

    command.upgrade(config, "head")

    with engine.connect() as conn:
        tickers = {r[0] for r in conn.execute(text("SELECT ticker FROM stocks"))}
    assert not any(t.endswith(".NE") for t in tickers)  # every .NE CDR is purged
    assert {"SHOP.TO", "STONE"} <= tickers  # the TSX name and the lookalike stay


def test_upgrade_creates_the_market_brief_table(alembic):
    # 0034 adds the standalone stock_market_brief table (date PK, no stocks anchor) backing
    # the daily-market-brief slice.
    config, url = alembic

    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    assert "stock_market_brief" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("stock_market_brief")}
    assert {"brief_date", "generated_at", "tone", "summary", "sections", "model"} <= columns
    pk = inspector.get_pk_constraint("stock_market_brief")
    assert pk["constrained_columns"] == ["brief_date"]

    command.downgrade(config, "base")
    assert "stock_market_brief" not in inspect(create_engine(url)).get_table_names()
