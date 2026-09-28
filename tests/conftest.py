import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def tmp_data_env(tmp_path, monkeypatch):
    """Point the persistent data/output directories at a temporary location."""
    monkeypatch.setenv("AZMONITOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZMONITOR_OUTPUT_DIR", str(tmp_path / "outputs"))
    return tmp_path
