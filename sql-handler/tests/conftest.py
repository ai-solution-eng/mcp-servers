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


@pytest.fixture(autouse=True)
def _isolated_saved_query_store(tmp_path, monkeypatch):
    """Point every test's saved-query store into its own tmp_path.

    Same shape as the catalog-store isolation: the saved-query store defaults
    next to the platform temp dir, and a test that saves a query must never
    leak into (or read) a store left behind by another test or a real run.
    """
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(tmp_path / "saved-queries.json"))
