"""Shared pytest fixtures for the JobSpy Lite test suite."""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _reset_pipeline_shutdown():
    """A previous server shutdown must not skip scoring in later tests."""
    from app.pipeline import _SHUTDOWN

    _SHUTDOWN.clear()
    yield
    _SHUTDOWN.clear()


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point storage at a throwaway database and close it after the test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.storage.DB_PATH", db_path)
    import importlib

    from app import storage

    storage.close()
    import app.config as config_module

    monkeypatch.setattr(config_module, "DB_PATH", db_path)
    yield db_path
    storage.close()


@pytest.fixture()
def clean_settings(temp_db):
    """Ensure the settings table starts empty for each test."""
    from app import storage

    yield storage
    for key in storage.all_settings():
        storage.set_setting(key, None)
