from datetime import date

import pytest

from emporio.data import StoreData
from emporio.tools import Toolbox

# Dataset snapshot date: right after the last order in orders.csv (2026-03-22).
SNAPSHOT = date(2026, 3, 25)


@pytest.fixture(scope="session")
def store() -> StoreData:
    return StoreData.load()


@pytest.fixture
def toolbox(store, tmp_path, monkeypatch) -> Toolbox:
    # Handoff tickets go to a temp dir instead of the real runtime/ folder.
    monkeypatch.setattr("emporio.tools.RUNTIME_DIR", tmp_path)
    return Toolbox(store=store, today=lambda: SNAPSHOT)
