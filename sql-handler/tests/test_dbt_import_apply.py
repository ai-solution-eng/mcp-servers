"""apply_import abort semantics (audit 2026-11-12 P1-11).

A failed read of the CURRENT catalog must abort the apply: merging onto an
empty base and writing the result back would silently destroy every
hand-written entry in the upload store. These tests pin the no-fallback
behavior and the merge-on-success path around it.
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import FileConfig
from sqlhandler.dbt_import import apply_import, import_dbt_manifest
from sqlhandler.engine import SqlEngine
from sqlhandler.file import FileProvider


def _manifest_import():
    return import_dbt_manifest(
        {
            "nodes": {
                "model.jaffle.orders": {
                    "resource_type": "model",
                    "name": "orders",
                    "schema": "analytics",
                    "database": "wh",
                    "relation_name": '"wh"."analytics"."orders"',
                    "description": "Imported wording",
                    "config": {"materialized": "table"},
                    "columns": {},
                }
            }
        }
    )


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    d = tmp_path / "analytics" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}), d / "part.parquet")
    return SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)


def test_apply_aborts_when_the_store_is_unreadable(engine, tmp_path):
    """A REAL unreadable store, no mocks (cross-validation 2026-11-12).

    The previous version of this test monkeypatched _catalog to raise — a
    behavior the real function never has (it swallows read errors and returns
    {}) — and replaced set_catalog_text, so it passed with the bug present.
    This version makes the store file genuinely unreadable and asserts the
    hand-written entries survive byte-identically.
    """
    import os

    store = tmp_path / "store.json"
    engine.set_catalog_text(
        json.dumps(
            {
                "tables": {
                    "analytics/orders": {"description": "hand-written"},
                    "analytics/precious": {"description": "also hand-written"},
                }
            }
        )
    )
    seed = store.read_bytes()
    os.chmod(store, 0o000)  # unreadable for the owner (the process runs unprivileged)
    try:
        with pytest.raises(Exception):
            apply_import(engine, _manifest_import())
    finally:
        os.chmod(store, 0o644)
    assert store.read_bytes() == seed, "the store must be byte-identical after a refused apply"
    assert b"precious" in store.read_bytes()


def test_apply_works_on_a_fresh_deployment_with_no_store(engine, tmp_path):
    """An ABSENT store is not an error: the first import creates it."""
    store = tmp_path / "store.json"
    assert not store.exists()
    result = apply_import(engine, _manifest_import())
    assert result["applied"] == 1
    assert store.exists()


def test_apply_merges_onto_current_entries(engine):
    engine.set_catalog_text(
        json.dumps(
            {
                "tables": {
                    "analytics/orders": {"description": "hand-written"},
                    "analytics/other": {"description": "precious"},
                }
            }
        )
    )
    out = apply_import(engine, _manifest_import())
    catalog = engine._catalog()
    assert catalog["analytics/other"]["description"] == "precious"  # survives (P1-11)
    assert catalog["analytics/orders"]["description"] == "hand-written"  # kept by merge rule
    assert out["overwritten"] == 0 and out["merged_total"] == 2


def test_apply_writes_when_store_read_succeeds(engine):
    out = apply_import(engine, _manifest_import())
    assert out["applied"] == 1 and out["merged_total"] == 1
    assert engine._catalog()["analytics/orders"]["description"] == "Imported wording"


def test_apply_respects_uploads_disabled(engine, monkeypatch):
    monkeypatch.setattr(engine, "catalog_uploads_enabled", False, raising=True)
    calls = []
    monkeypatch.setattr(engine, "set_catalog_text", lambda text: calls.append(text))
    with pytest.raises(ValueError):
        apply_import(engine, _manifest_import())
    assert calls == []