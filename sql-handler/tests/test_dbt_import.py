"""Tests for the dbt manifest → semantic-catalog importer.

Covers the pure module (``sqlhandler.dbt_import``), the REST surface
(``POST /api/semantic-catalog/import-dbt`` + ``/apply``) including its auth
posture (same token gating as the sibling catalog routes), the merge rule
through the engine's upload store, and hot reload: after an apply, the
engine's ``describe`` output carries the imported wording with no restart.
"""

import base64
import copy
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import webui
from sqlhandler.config import FileConfig
from sqlhandler.dbt_import import DbtImportError, apply_import, import_dbt_manifest
from sqlhandler.engine import SqlEngine
from sqlhandler.file import FileProvider

# ---------------------------------------------------------------------------
# fixture: a realistic small manifest (3 models, the shapes the import has to
# tell apart) + a file-backend engine with an isolated catalog store
# ---------------------------------------------------------------------------


def _manifest() -> dict:
    return {
        "nodes": {
            "model.jaffle.orders": {
                "resource_type": "model",
                "name": "orders",
                "alias": "orders",
                "schema": "analytics",
                "database": "wh",
                "relation_name": '"wh"."analytics"."orders"',
                "description": "Order headers, one row per order",
                "config": {"materialized": "table"},
                "columns": {
                    "id": {"name": "id", "description": "Order id", "dtype": "INTEGER"},
                    "amount": {"name": "amount", "description": "Order total in USD", "dtype": "FLOAT"},
                },
            },
            "model.jaffle.vw_big_orders": {
                "resource_type": "model",
                "name": "vw_big_orders",
                "alias": "vw_big_orders",
                "schema": "analytics",
                "description": "Big orders only",
                "config": {"materialized": "view"},
                "meta": {"sqlhandler": {"virtual": True, "aliases": ["big orders"]}},
                "compiled_sql": "SELECT id, amount FROM orders WHERE amount > 100",
                "columns": {},
            },
            "model.jaffle.stg_tmp": {
                "resource_type": "model",
                "name": "stg_tmp",
                "schema": "analytics",
                "description": "Ephemeral staging",
                "config": {"materialized": "ephemeral"},
                "depends_on": {"nodes": []},
                "columns": {},
            },
            "test.jaffle.unique_orders_id": {
                "resource_type": "test",
                "name": "unique_orders_id",
                "config": {},
            },
        }
    }


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """File backend with the physical table at <root>/analytics/orders/ — the
    same <schema>/<name> path the default import key generates."""
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    d = tmp_path / "analytics" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}), d / "part.parquet")
    return SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)


# ---------------------------------------------------------------------------
# import_dbt_manifest: result shape / filtering / virtual gating
# ---------------------------------------------------------------------------


def test_import_shape_descriptions_and_columns():
    r = import_dbt_manifest(_manifest())
    assert r["imported"] == 2 and r["virtuals"] == 0 and r["skipped"] == 1
    entry = r["catalog"]["tables"]["analytics/orders"]
    assert entry["description"] == "Order headers, one row per order"
    assert entry["columns"] == {"id": "Order id", "amount": "Order total in USD"}
    # provenance marker rides the entry meta (schema preserves extra keys)
    assert entry["meta"]["imported_from"] == "dbt"
    assert entry["meta"]["manifest_node"] == "model.jaffle.orders"
    assert "warnings" in r


def test_ephemeral_skipped_tests_never_imported():
    r = import_dbt_manifest(_manifest())
    assert "analytics/stg_tmp" not in r["catalog"]["tables"]
    assert r["skipped"] == 1  # the ephemeral model; tests never count at all
    assert all("stg_tmp" not in k for k in r["catalog"]["tables"])


def test_ephemeral_imported_when_depended_on():
    m = _manifest()
    m["nodes"]["model.jaffle.vw_big_orders"]["depends_on"] = {"nodes": ["model.jaffle.stg_tmp"]}
    m["nodes"]["model.jaffle.stg_tmp"]["columns"] = {"id": {"name": "id", "description": "pk"}}
    r = import_dbt_manifest(_copy_with(m))
    assert "analytics/stg_tmp" in r["catalog"]["tables"]
    assert any("ephemeral but imported" in w for w in r["warnings"])


def _copy_with(m):
    # deepcopy + an extra plain model so the ephemeral dependency is "included"
    m = copy.deepcopy(m)
    m["nodes"]["model.jaffle.plain"] = {
        "resource_type": "model",
        "name": "plain",
        "schema": "analytics",
        "config": {},
        "depends_on": {"nodes": ["model.jaffle.stg_tmp"]},
        "description": "needs the ephemeral",
    }
    return m


def test_virtual_double_gate_meta_only():
    """meta.sqlhandler.virtual WITHOUT allow_virtual → no definition, a warning."""
    r = import_dbt_manifest(_manifest())
    entry = r["catalog"]["tables"]["analytics/vw_big_orders"]
    assert "definition" not in entry
    assert r["virtuals"] == 0
    assert any("allow_virtual" in w for w in r["warnings"])


def test_virtual_double_gate_flag_only():
    """allow_virtual WITHOUT the node meta → still no definition."""
    m = _manifest()
    del m["nodes"]["model.jaffle.vw_big_orders"]["meta"]
    r = import_dbt_manifest(m, allow_virtual=True)
    assert r["virtuals"] == 0
    assert "definition" not in r["catalog"]["tables"]["analytics/vw_big_orders"]


def test_virtual_generated_with_both_gates():
    """Virtual entries take the BARE dbt name: the engine only registers a
    definition from a bare-identifier catalog key (schema/… keys are skipped
    with a warning at load)."""
    r = import_dbt_manifest(_manifest(), allow_virtual=True)
    entry = r["catalog"]["tables"]["vw_big_orders"]
    assert "analytics/vw_big_orders" not in r["catalog"]["tables"]
    assert entry["definition"] == "SELECT id, amount FROM orders WHERE amount > 100"
    assert r["virtuals"] == 1 and r["imported"] == 2
    assert entry["aliases"] == ["big orders"]  # meta.sqlhandler.aliases honored


def test_virtual_definition_refuses_non_select():
    m = _manifest()
    m["nodes"]["model.jaffle.vw_big_orders"]["compiled_sql"] = "INSERT INTO orders VALUES (1)"
    r = import_dbt_manifest(m, allow_virtual=True)
    assert r["virtuals"] == 0
    assert any("not a read-only SELECT/WITH" in w for w in r["warnings"])


def test_virtual_definition_refuses_ddl():
    m = _manifest()
    m["nodes"]["model.jaffle.vw_big_orders"]["compiled_sql"] = "CREATE TABLE x AS SELECT 1"
    r = import_dbt_manifest(m, allow_virtual=True)
    assert r["virtuals"] == 0
    assert any("not a read-only SELECT/WITH" in w for w in r["warnings"])


def test_virtual_without_compiled_sql_warns():
    m = _manifest()
    del m["nodes"]["model.jaffle.vw_big_orders"]["compiled_sql"]
    r = import_dbt_manifest(m, allow_virtual=True)
    assert r["virtuals"] == 0
    assert any("dbt compile" in w for w in r["warnings"])


def test_hide_omits_node_entirely():
    m = _manifest()
    m["nodes"]["model.jaffle.orders"]["meta"] = {"sqlhandler": {"hide": True}}
    r = import_dbt_manifest(m, allow_virtual=True)
    assert "analytics/orders" not in r["catalog"]["tables"]
    assert any("hidden via meta.sqlhandler.hide" in w for w in r["warnings"])


def test_source_filter_substring():
    r = import_dbt_manifest(_manifest(), source_filter="big")
    assert list(r["catalog"]["tables"]) == ["analytics/vw_big_orders"]
    r_open = import_dbt_manifest(_manifest(), source_filter="big", allow_virtual=True)
    assert list(r_open["catalog"]["tables"]) == ["vw_big_orders"]
    r2 = import_dbt_manifest(_manifest(), source_filter='"wh"."analytics"')
    assert r2["imported"] == 1  # only the non-ephemeral relations carry relation_name


def test_alias_map_overrides_generated_key():
    r = import_dbt_manifest(_manifest(), alias_map={"orders": "workorder/work_order"})
    assert "workorder/work_order" in r["catalog"]["tables"]
    assert "analytics/orders" not in r["catalog"]["tables"]


def test_manifest_b64_and_invalid_inputs():
    raw = json.dumps(_manifest()).encode()
    r = import_dbt_manifest(base64.b64encode(raw).decode())
    assert r["imported"] == 2
    with pytest.raises(DbtImportError, match="not valid JSON"):
        import_dbt_manifest("{oops")
    with pytest.raises(DbtImportError, match="nodes"):
        import_dbt_manifest({"foo": {}})
    with pytest.raises(DbtImportError):
        import_dbt_manifest(42)


def test_dtype_without_description_becomes_placeholder():
    m = {"nodes": {
        "model.a.t": {
            "resource_type": "model", "name": "t", "schema": "s", "config": {},
            "columns": {"a": {"name": "a", "dtype": "INTEGER"}},  # dtype only, no description
        }
    }}
    r = import_dbt_manifest(m)
    assert r["catalog"]["tables"]["s/t"]["columns"] == {"a": "INTEGER column"}


def test_undocumented_node_skipped_not_blank_overwritten():
    """A node with nothing to say is skipped: importing empty docs would
    clobber richer hand-written entries with nothing."""
    m = {"nodes": {
        "model.a.t": {"resource_type": "model", "name": "t", "schema": "s", "config": {}, "columns": {}},
    }}
    r = import_dbt_manifest(m)
    assert r["imported"] == 0 and r["skipped"] == 1
    assert any("no description" in w for w in r["warnings"])


# ---------------------------------------------------------------------------
# merge rule through the engine store (apply_import)
# ---------------------------------------------------------------------------


def test_apply_writes_store_and_hot_reloads(engine):
    r = import_dbt_manifest(_manifest())
    out = apply_import(engine, r)
    assert out["applied"] == 2 and out["merged_total"] == 2
    # hot reload: the NEXT describe carries the imported docs (no restart)
    assert webui.api_describe(engine, "orders")["description"] == "Order headers, one row per order"
    assert webui.api_describe(engine, "orders")["columns"][0]["description"] == "Order id"
    assert engine.catalog_status()["active_source"] == "upload"


def test_merge_keeps_handwritten_entries(engine):
    engine.set_catalog_text(json.dumps({"tables": {"analytics/orders": {"description": "hand-written"}}}))
    r = import_dbt_manifest(_manifest())
    out = apply_import(engine, r)
    assert out["overwritten"] == 0
    catalog = engine._catalog()
    assert catalog["analytics/orders"]["description"] == "hand-written"
    assert any("force_overwrite" in w for w in r["warnings"])
    # the untouched import is still visible as proposed output
    assert r["catalog"]["tables"]["analytics/orders"]["description"].startswith("Order headers")


def test_merge_refreshes_previous_dbt_imports(engine):
    r1 = import_dbt_manifest(_manifest())
    apply_import(engine, r1)
    # a second import with NEW wording converges onto the same key
    m = _manifest()
    m["nodes"]["model.jaffle.orders"]["description"] = "v2 wording"
    r2 = import_dbt_manifest(m)
    out = apply_import(engine, r2)
    assert out["overwritten"] == 2  # both keys are previous dbt imports -> refreshed
    assert engine._catalog()["analytics/orders"]["description"] == "v2 wording"
    assert webui.api_describe(engine, "orders")["description"] == "v2 wording"


def test_merge_force_overwrite_beats_handwritten(engine):
    engine.set_catalog_text(json.dumps({"tables": {"analytics/orders": {"description": "hand-written"}}}))
    r = import_dbt_manifest(_manifest())
    out = apply_import(engine, r, force_overwrite=True)
    assert out["overwritten"] == 1
    assert engine._catalog()["analytics/orders"]["description"].startswith("Order headers")
    assert not any("force_overwrite" in w for w in r["warnings"])


def test_apply_disabled_with_uploads_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_CATALOG_UPLOAD", "0")
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    d = tmp_path / "analytics" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1]}), d / "part.parquet")
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    r = import_dbt_manifest(_manifest())
    with pytest.raises(ValueError, match="disabled"):
        apply_import(eng, r)


def test_apply_virtual_table_becomes_queryable(engine):
    r = import_dbt_manifest(_manifest(), allow_virtual=True)
    apply_import(engine, r)
    names = [t.name for t in engine.list_tables()]
    assert "vw_big_orders" in names
    virtual = next(t for t in engine.list_tables() if t.name == "vw_big_orders")
    assert virtual.format == "virtual"
    # ...and the SQL path resolves it (base table comes along transitively)
    out = engine.query_duckdb("SELECT count(*) AS n FROM vw_big_orders")
    assert out.column("n").to_pylist() == [0]


# ---------------------------------------------------------------------------
# REST surface: preview route (no write), apply route, token gating
# ---------------------------------------------------------------------------


def test_http_import_preview_then_apply(tmp_path, monkeypatch):
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _ApiTokenMiddleware, _transport_security
    from sqlhandler.server import mcp as mcp_server

    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    d = tmp_path / "analytics" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1, 2], "amount": [1.0, 2.0]}), d / "part.parquet")
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)

    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    webui.register_ui(app, lambda: eng)
    app.add_middleware(_ApiTokenMiddleware, token="tok-123")
    client = TestClient(app)
    auth = {"X-API-Token": "tok-123"}
    body = {"manifest": _manifest(), "allow_virtual": True}

    # auth posture: identical to the sibling catalog routes
    assert client.post("/api/semantic-catalog/import-dbt", json=body).status_code == 401
    assert client.post("/api/semantic-catalog/import-dbt/apply", json=body).status_code == 401

    # preview: 200 + counts, and NOTHING written (no store file yet)
    r = client.post("/api/semantic-catalog/import-dbt", json=body, headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["imported"] == 2 and data["virtuals"] == 1
    assert "catalog" in data and "warnings" in data
    assert not (tmp_path / "store.json").exists()
    assert eng.catalog_status()["active_source"] is None

    # apply: writes the store; describe hot-reloads
    r2 = client.post("/api/semantic-catalog/import-dbt/apply", json=body, headers=auth)
    assert r2.status_code == 200
    applied = r2.json()
    assert applied["applied"] == 2 and applied["tables"] == 2
    assert eng.catalog_status()["active_source"] == "upload"
    assert client.post("/api/describe", json={"table": "orders"}, headers=auth).json()["description"] == (
        "Order headers, one row per order"
    )

    # invalid body → 400 (ValueError mapping, same as the sibling routes)
    assert client.post("/api/semantic-catalog/import-dbt", json={"manifest": 42}, headers=auth).status_code == 400
    assert client.post("/api/semantic-catalog/import-dbt", json={}, headers=auth).status_code == 400
    assert client.post("/api/semantic-catalog/import-dbt", content=b"{broken", headers=auth).status_code == 400


def test_http_apply_respects_uploads_disabled(tmp_path, monkeypatch):
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _ApiTokenMiddleware, _transport_security
    from sqlhandler.server import mcp as mcp_server

    monkeypatch.setenv("SQLHANDLER_CATALOG_UPLOAD", "0")
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    d = tmp_path / "analytics" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1]}), d / "part.parquet")
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    webui.register_ui(app, lambda: eng)
    app.add_middleware(_ApiTokenMiddleware, token="tok-123")
    client = TestClient(app)
    auth = {"X-API-Token": "tok-123"}
    body = {"manifest": _manifest()}
    # preview still WORKS (parse-only is not a mutation); apply refuses 400
    assert client.post("/api/semantic-catalog/import-dbt", json=body, headers=auth).status_code == 200
    r = client.post("/api/semantic-catalog/import-dbt/apply", json=body, headers=auth)
    assert r.status_code == 400
    assert "disabled" in r.json()["error"]


def test_http_import_b64_body(tmp_path, monkeypatch):
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _ApiTokenMiddleware, _transport_security
    from sqlhandler.server import mcp as mcp_server

    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    d = tmp_path / "analytics" / "orders"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1]}), d / "part.parquet")
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    webui.register_ui(app, lambda: eng)
    app.add_middleware(_ApiTokenMiddleware, token="tok-123")
    client = TestClient(app)
    b64 = base64.b64encode(json.dumps(_manifest()).encode()).decode()
    r = client.post(
        "/api/semantic-catalog/import-dbt",
        json={"manifest_b64": b64},
        headers={"X-API-Token": "tok-123"},
    )
    assert r.status_code == 200 and r.json()["imported"] == 2
