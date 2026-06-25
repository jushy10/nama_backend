"""Tests for the file-backed ConstituentRepository.

Two jobs: map the JSON onto the Constituent entity (offline, against a tiny
fixture file), and a sanity check that the bundled universe the screener
actually ships is well-formed.
"""

import json
from pathlib import Path

from app.stocks.constituents import JsonConstituentRepository
from app.stocks.entities import Constituent, StockIndex


def _write(tmp_path, records) -> Path:
    path = tmp_path / "constituents.json"
    path.write_text(json.dumps({"constituents": records}), encoding="utf-8")
    return path


def test_maps_records_onto_entities(tmp_path):
    repo = JsonConstituentRepository(
        _write(
            tmp_path,
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "sector": "Information Technology",
                    "indices": ["sp500", "nasdaq100"],
                }
            ],
        )
    )
    (apple,) = repo.all()
    assert isinstance(apple, Constituent)
    assert apple.symbol == "AAPL"
    assert apple.name == "Apple Inc."
    assert apple.sector == "Information Technology"
    assert apple.in_index(StockIndex.SP500)
    assert apple.in_index(StockIndex.NASDAQ100)


def test_missing_fields_become_none(tmp_path):
    repo = JsonConstituentRepository(_write(tmp_path, [{"symbol": "ZZZZ"}]))
    (z,) = repo.all()
    assert z.name is None and z.sector is None
    assert z.indices == frozenset()
    assert not z.in_index(StockIndex.SP500)


def test_file_is_parsed_once_and_cached(tmp_path):
    path = _write(tmp_path, [{"symbol": "AAPL", "indices": ["sp500"]}])
    repo = JsonConstituentRepository(path)
    first = repo.all()
    path.write_text(json.dumps({"constituents": []}), encoding="utf-8")  # change on disk
    assert repo.all() is first  # cached: the file isn't re-read


# --------------------------- bundled universe sanity ---------------------------


def test_bundled_universe_is_wellformed():
    universe = JsonConstituentRepository().all()
    by_symbol = {c.symbol: c for c in universe}
    assert len(by_symbol) == len(universe)  # symbols are unique

    sp500 = [c for c in universe if c.in_index(StockIndex.SP500)]
    nasdaq = [c for c in universe if c.in_index(StockIndex.NASDAQ100)]
    assert 490 <= len(sp500) <= 510  # roughly 500 names
    assert 95 <= len(nasdaq) <= 110  # roughly 100 names

    # Every name belongs to at least one index and carries a GICS sector.
    assert all(c.indices for c in universe)
    assert all(c.sector for c in universe)

    # A couple of well-known members land in the right indices.
    assert by_symbol["AAPL"].in_index(StockIndex.SP500)
    assert by_symbol["AAPL"].in_index(StockIndex.NASDAQ100)
