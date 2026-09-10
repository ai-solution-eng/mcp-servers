"""Shared test fixtures for the sqlhandler test suite."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_catalog_store(tmp_path, monkeypatch):
    """Point every test's catalog upload store into its own tmp_path.

    The engine defaults SQLHANDLER_CATALOG_STORE next to the platform temp
    dir; a leftover uploaded catalog from any previous run would silently
    override the SQLHANDLER_CATALOG file a test is trying to exercise (the
    upload store takes precedence by design). Autouse + per-test tmp_path
    makes that impossible.
    """
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "catalog-store.json"))
