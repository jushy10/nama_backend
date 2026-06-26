"""The Alembic migrations build the schema the ORM models expect.

Runs the real migration chain against a throwaway SQLite database: upgrade
creates ``index_constituents`` with the expected columns, downgrade drops it.
Catches a migration that won't apply or has drifted from the model.
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


def test_upgrade_creates_table_then_downgrade_drops_it(alembic):
    config, url = alembic

    command.upgrade(config, "head")
    columns = inspect(create_engine(url)).get_columns("index_constituents")
    assert {c["name"] for c in columns} == {
        "symbol",
        "name",
        "sector",
        "in_sp500",
        "in_nasdaq100",
    }

    command.downgrade(config, "base")
    assert "index_constituents" not in inspect(create_engine(url)).get_table_names()
