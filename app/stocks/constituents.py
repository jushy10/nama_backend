"""Interface Adapter: the file-backed ConstituentRepository.

The screener's universe — which symbols belong to which index, and each one's
GICS sector — is static reference data, so it ships as a JSON file baked into
the package rather than coming from a live feed. Regenerate that file with
``scripts/build_constituents.py`` when the indices reconstitute.

This is the only module that knows the on-disk shape; it maps the JSON onto the
Constituent entity. Swap the storage and only this file changes.
"""

import json
from pathlib import Path

from app.stocks.entities import Constituent
from app.stocks.ports import ConstituentRepository

_DATA_FILE = Path(__file__).resolve().parent / "data" / "constituents.json"


class JsonConstituentRepository(ConstituentRepository):
    """Reads the index-constituents universe from the bundled JSON file.

    The file is parsed once and cached on the instance: it's static for the
    process's lifetime and the router holds a single shared instance, so there
    is no per-request file read.
    """

    def __init__(self, data_file: Path = _DATA_FILE) -> None:
        self._data_file = data_file
        self._constituents: tuple[Constituent, ...] | None = None

    def all(self) -> tuple[Constituent, ...]:
        if self._constituents is None:
            self._constituents = self._load()
        return self._constituents

    def _load(self) -> tuple[Constituent, ...]:
        raw = json.loads(self._data_file.read_text(encoding="utf-8"))
        return tuple(
            Constituent(
                symbol=record["symbol"],
                name=record.get("name"),
                sector=record.get("sector"),
                indices=frozenset(record.get("indices", ())),
            )
            for record in raw["constituents"]
        )
