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
