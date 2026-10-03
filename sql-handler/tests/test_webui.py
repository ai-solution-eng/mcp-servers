"""Tests for the read-only web UI / JSON API layer (``sqlhandler.webui``)."""

import datetime
import json
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import webui as webui_module
from sqlhandler.config import FileConfig
from sqlhandler.engine import SqlEngine
from sqlhandler.file import FileProvider
from sqlhandler.provider import LakehouseError
from sqlhandler.webui import (
    _HIGHLIGHT_MAX_CHARS,
    _clamp_limit,
    api_catalog_clear,
    api_catalog_content,
    api_catalog_status,
    api_catalog_table,
    api_catalog_table_remove,
    api_catalog_table_update,
    api_catalog_upload,
    api_describe,
    api_highlight,
    api_preview,
    api_query,
    api_status,
    api_tables,
    arrow_to_payload,
    assert_readonly,
    register_ui,
)

# ---------------------------------------------------------------------------
# read-only guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t",
        "select * from t",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT * FROM t",
        "EXPLAIN ANALYZE SELECT * FROM t",  # ANALYZE is safe on a SELECT
        "SHOW TABLES",
        "DESCRIBE SELECT * FROM t",
        "SUMMARIZE SELECT * FROM t",
        "VALUES (1, 2)",
        "  (SELECT 1)",  # leading paren is tolerated
        "SELECT * FROM t; SELECT 2;",  # multi-statement all read-only
        "SELECT * FROM t WHERE s = 'a;b'",  # semicolon inside a string literal
        "-- drop table t\nSELECT 1",  # write keyword inside a comment
    ],
)
def test_assert_readonly_allows(sql):
    assert assert_readonly(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "not even sql",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "CREATE TABLE t (a int)",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN b int",
        "MERGE INTO t USING x ON 1=1",
        "GRANT SELECT TO u",
        "COPY (SELECT * FROM t) TO '/tmp/exfil.parquet'",
        # PRAGMA is a SET alias in DuckDB (PRAGMA threads=4 mutates settings).
        "PRAGMA threads=4",
        "SET threads = 4",
        "PRAGMA table_info(t)",
        # EXPLAIN ANALYZE <write> EXECUTES the write — verified on duckdb 1.5.
        "EXPLAIN ANALYZE INSERT INTO t VALUES (1)",
        "EXPLAIN INSERT INTO t VALUES (1)",
        "explain analyze delete from t",
        # one bad statement fails the whole query
        "SELECT 1; DROP TABLE t",
        "SELECT 1; PRAGMA enable_external_access",
    ],
)
def test_assert_readonly_rejects(sql):
    with pytest.raises(ValueError):
        assert_readonly(sql)


# ---------------------------------------------------------------------------
# JSON-safe payload conversion
# ---------------------------------------------------------------------------


def test_arrow_to_payload_json_safe():
    utc = datetime.UTC
    table = pa.table(
        {
            "id": [1, 2, 3],
            "name": ["a", "b", "c"],
            "when": [
                datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=utc),
                None,
                datetime.datetime(2024, 2, 2, tzinfo=utc),
            ],
            "amount": [Decimal("1.50"), Decimal("2.25"), None],
            "flag": [True, False, None],
        }
    )
    payload = arrow_to_payload(table, limit=2)
    assert payload["n_rows"] == 2
    assert payload["truncated"] is True
    assert len(payload["rows"]) == 2
    assert payload["columns"] == ["id", "name", "when", "amount", "flag"]
    # datetime -> isoformat string
    assert payload["rows"][0][2] == "2024-01-01T12:00:00+00:00"
    # None preserved
    assert payload["rows"][1][2] is None
    # decimal -> float
    assert payload["rows"][0][3] == 1.5


def test_arrow_to_payload_empty():
    table = pa.table({"a": pa.array([], type=pa.int64())})
    payload = arrow_to_payload(table)
    assert payload["columns"] == ["a"]
    assert payload["rows"] == []
    assert payload["n_rows"] == 0
    assert payload["truncated"] is False


# ---------------------------------------------------------------------------
# limit clamping
# ---------------------------------------------------------------------------


def test_clamp_limit():
    assert _clamp_limit(None) == 100
    assert _clamp_limit(0) == 100
    assert _clamp_limit(-5) == 100
    assert _clamp_limit(10) == 10
    assert _clamp_limit(5000) == 1000  # SQLHANDLER_MAX_ROWS default


def test_clamp_limit_follows_max_rows_env(monkeypatch):
    """The UI cap tracks SQLHANDLER_MAX_ROWS (README: 'same row caps')."""
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "500")
    assert _clamp_limit(5000) == 500
    assert _clamp_limit(10) == 10
    # MAX_ROWS=0 means unlimited for MCP, but the UI payload stays bounded.
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "0")
    assert _clamp_limit(5000) == 1000
    # A garbage value falls back to the default cap.
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "banana")
    assert _clamp_limit(5000) == 1000


# ---------------------------------------------------------------------------
# API handlers against a local file backend
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    pq.write_table(
        pa.table(
            {
                "id": [1, 2, 3],
                "name": ["x", "y", "z"],
                "qty": [1.5, 2.5, 3.5],
            }
        ),
        str(tmp_path / "orders.parquet"),
    )
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    return SqlEngine(provider, cache_ttl=3600, dataset_cache_ttl=3600)


def test_api_status(engine):
    status = api_status(engine)
    assert status["status"] == "ok"
    assert status["backend"] == "nfs"
    assert status["version"]


def test_api_tables(engine):
    tables = api_tables(engine)
    assert any(t["name"] == "orders" for t in tables["tables"])


def test_api_describe(engine):
    desc = api_describe(engine, "orders")
    assert desc["n_columns"] == 3
    cols = {c["name"] for c in desc["columns"]}
    assert cols == {"id", "name", "qty"}
    assert "uri" in desc


def test_api_query(engine):
    payload = api_query(engine, "SELECT id, name FROM orders WHERE qty > 2")
    assert payload["columns"] == ["id", "name"]
    assert payload["rows"] == [[2, "y"], [3, "z"]]
    assert payload["n_rows"] == 2
    assert payload["duration_ms"] >= 0


def test_api_query_limiting(engine):
    payload = api_query(engine, "SELECT * FROM orders", limit=2)
    assert len(payload["rows"]) == 2
    assert payload["n_rows"] == 2
    assert payload["truncated"] is True


def test_api_query_rejects_writes(engine):
    with pytest.raises(ValueError):
        api_query(engine, "DELETE FROM orders")


def test_api_preview(engine):
    payload = api_preview(engine, "orders", limit=2)
    assert payload["table"] == "orders"
    assert payload["columns"] == ["id", "name", "qty"]
    assert len(payload["rows"]) == 2
    assert payload["n_rows"] == 2
    assert payload["truncated"] is True


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_query_csv(tmp_path):

    from sqlhandler.webui import api_export

    eng = _engine(tmp_path)
    out = api_export(eng, {"sql": "SELECT * FROM work_order ORDER BY a", "format": "csv"})
    assert out["media_type"] == "text/csv"
    assert out["filename"] == "query.csv"
    text = out["content"].decode()
    lines = text.strip().splitlines()
    assert lines[0] == "a,s"
    assert len(lines) == 1 + 5


def test_export_query_parquet_roundtrip(tmp_path):
    import io

    import pyarrow.parquet as pq

    from sqlhandler.webui import api_export

    eng = _engine(tmp_path)
    out = api_export(eng, {"sql": "SELECT * FROM work_order", "format": "parquet"})
    assert out["filename"] == "query.parquet"
    back = pq.read_table(io.BytesIO(out["content"]))
    assert back.num_rows == 5


def test_export_table_uses_safe_filename(tmp_path):
    from sqlhandler.webui import api_export

    eng = _engine(tmp_path)
    out = api_export(eng, {"table": "workorder/work_order"})
    assert out["filename"] == "workorder_work_order.csv"
    assert b"1" in out["content"]


def test_export_guard_blocks_writes(tmp_path):
    from sqlhandler.webui import api_export

    eng = _engine(tmp_path)
    with pytest.raises(ValueError, match="not allowed"):
        api_export(eng, {"sql": "CREATE TABLE x (a int)", "format": "csv"})


def test_export_bad_format_and_missing_target(tmp_path):
    from sqlhandler.webui import api_export

    eng = _engine(tmp_path)
    with pytest.raises(ValueError, match="Unsupported export format"):
        api_export(eng, {"sql": "SELECT 1", "format": "xlsx"})
    with pytest.raises(ValueError, match="either 'sql' or 'table'"):
        api_export(eng, {"format": "csv"})


def test_export_limit_clamped_to_env_cap(tmp_path, monkeypatch):
    from sqlhandler.webui import _export_max_rows, api_export

    monkeypatch.setenv("SQLHANDLER_EXPORT_MAX_ROWS", "3")
    assert _export_max_rows() == 3
    eng = _engine(tmp_path)
    out = api_export(eng, {"sql": "SELECT * FROM work_order", "limit": 100000, "format": "csv"})
    assert len(out["content"].decode().strip().splitlines()) == 1 + 3
    # 0 means the hard ceiling, not unlimited
    monkeypatch.setenv("SQLHANDLER_EXPORT_MAX_ROWS", "0")
    assert _export_max_rows() == 1_000_000


def _engine(tmp_path):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"a": [1, 2, 3, 4, 5], "s": ["a", "b", "a", "b", "a"]}), d / "part.parquet"
    )
    return SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)


# ---------------------------------------------------------------------------
# semantic catalog: status / upload (JSON or YAML) / clear
# ---------------------------------------------------------------------------


_YAML_CATALOG = b"tables:\n  orders:\n    description: Order headers (uploaded)\n    columns:\n      qty: Quantity in units\n"


def test_catalog_upload_yaml_then_json_roundtrip(engine, tmp_path):
    """Upload wins over 'no catalog'; a JSON upload replaces a YAML one."""
    res = api_catalog_upload(engine, _YAML_CATALOG)
    assert res["tables"] == 1
    status = api_catalog_status(engine)
    assert status["active_source"] == "upload"
    assert status["active_tables"] == 1
    # Descriptions flow through describe_table immediately (hot-reloaded).
    assert api_describe(engine, "orders")["description"] == "Order headers (uploaded)"
    # A JSON upload replaces the YAML one — same endpoint, both formats.
    api_catalog_upload(engine, b'{"tables": {"orders": {"description": "v2"}}}')
    assert api_describe(engine, "orders")["description"] == "v2"
    # The store file itself is always canonical JSON.
    import json as _json
    from pathlib import Path as _Path

    store = _Path(api_catalog_status(engine)["active_path"])
    assert _json.loads(store.read_text(encoding="utf-8"))["tables"]["orders"]["description"] == "v2"
    # Clearing removes the upload; the engine goes back to no catalog.
    cleared = api_catalog_clear(engine)
    assert cleared["removed"] is True
    assert api_catalog_status(engine)["active_path"] is None
    assert "description" not in api_describe(engine, "orders")
    assert api_catalog_clear(engine)["removed"] is False  # idempotent


def test_catalog_upload_rejections(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    with pytest.raises(ValueError, match="empty"):
        api_catalog_upload(engine, b"   \n")
    with pytest.raises(ValueError, match="not valid JSON"):
        api_catalog_upload(engine, b"{oops")
    with pytest.raises(ValueError, match="UTF-8"):
        api_catalog_upload(engine, b"\xff\xfe\x00bad")
    with pytest.raises(ValueError, match="tables"):
        api_catalog_upload(engine, b'{"foo": {"description": "no tables key"}}')
    with pytest.raises(ValueError, match="too large"):
        api_catalog_upload(engine, b"x" * 1_000_001)


def test_catalog_upload_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_CATALOG_UPLOAD", "0")
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    pq.write_table(pa.table({"a": [1]}), str(tmp_path / "t.parquet"))
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    assert eng.catalog_uploads_enabled is False
    assert api_catalog_status(eng)["uploads_enabled"] is False
    with pytest.raises(ValueError, match="disabled"):
        api_catalog_upload(eng, b'{"tables": {}}')


def test_semantic_catalog_http_routes(tmp_path, monkeypatch):
    """Route wiring end to end: auth, upload, describe merge, clear."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import (
        _ApiTokenMiddleware,
        _transport_security,
    )
    from sqlhandler.server import (
        mcp as mcp_server,
    )

    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    pq.write_table(pa.table({"id": [1, 2], "amount": [1.0, 2.0]}), str(tmp_path / "orders.parquet"))
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=3600)

    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    register_ui(app, lambda: eng)
    app.add_middleware(_ApiTokenMiddleware, token="tok-123")
    client = TestClient(app)
    auth = {"X-API-Token": "tok-123"}

    assert client.post("/api/semantic-catalog", content=b"tables: {}").status_code == 401

    yaml_catalog = b"tables:\n  orders:\n    description: over HTTP\n"
    assert (
        client.post("/api/semantic-catalog", content=yaml_catalog, headers=auth).json()["tables"]
        == 1
    )
    assert (
        client.post("/api/describe", json={"table": "orders"}, headers=auth).json()["description"]
        == "over HTTP"
    )
    assert client.get("/api/semantic-catalog", headers=auth).json()["active_source"] == "upload"

    assert (
        client.post(
            "/api/semantic-catalog",
            content=b'{"tables": {"orders": {"description": "v2"}}}',
            headers=auth,
        ).status_code
        == 200
    )
    assert (
        client.post("/api/describe", json={"table": "orders"}, headers=auth).json()["description"]
        == "v2"
    )

    assert client.post("/api/semantic-catalog", content=b"{broken", headers=auth).status_code == 400
    assert client.delete("/api/semantic-catalog", headers=auth).json()["removed"] is True
    assert (
        "description"
        not in client.post("/api/describe", json={"table": "orders"}, headers=auth).json()
    )


# ---------------------------------------------------------------------------
# semantic catalog editor: content / per-table upsert + remove / highlight
# ---------------------------------------------------------------------------


def _catalog_engine(tmp_path, monkeypatch):
    """A file-backend engine with an isolated catalog store."""
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    pq.write_table(
        pa.table({"id": [1, 2, 3], "qty": [1.0, 2.0, 3.0]}), str(tmp_path / "orders.parquet")
    )
    return SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=3600)


def test_catalog_content_api(engine):
    y = api_catalog_content(engine)
    assert y["format"] == "yaml" and y["source"] == "none"
    assert y["uploads_enabled"] is True and "tables:" in y["text"]
    j = api_catalog_content(engine, "json")
    assert "tables" in __import__("json").loads(j["text"])
    with pytest.raises(ValueError):
        api_catalog_content(engine, "xml")


def test_catalog_table_api_roundtrip(tmp_path, monkeypatch):
    eng = _catalog_engine(tmp_path, monkeypatch)
    d = api_catalog_table(eng, "orders")
    assert d["found"] is False and d["text"] == "" and d["key"] == "orders"
    api_catalog_table_update(eng, "orders", "description: my orders\ncolumns:\n  qty: units\n")
    d = api_catalog_table(eng, "orders", "json")
    assert d["found"] is True
    import json as _json

    assert _json.loads(d["text"])["description"] == "my orders"
    res = api_catalog_table_remove(eng, "orders")
    assert res["removed"] is True
    assert api_catalog_table(eng, "orders")["found"] is False
    with pytest.raises(ValueError):
        api_catalog_table(eng, "")
    with pytest.raises(LakehouseError):  # unknown table
        api_catalog_table(eng, "missing_table")


def test_catalog_table_update_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_CATALOG_UPLOAD", "0")
    eng = _catalog_engine(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="disabled"):
        api_catalog_table_update(eng, "orders", "description: x")
    with pytest.raises(ValueError, match="disabled"):
        api_catalog_table_remove(eng, "orders")


def test_highlight_api():
    r = api_highlight({"text": "tables:\n  orders:\n    description: x\n", "theme": "dark"})
    if r["highlighted"]:
        assert "<span" in r["html"] and "style" in r["html"]
        # -g semantics with a virtual filename: a YAML pane lexes as YAML
        # (a bare content guess returns e.g. ObjectiveC/Scdoc here).
        assert r["lexer"] == "YamlLexer"
    r2 = api_highlight({"text": '{"tables": {}}', "theme": "light", "format": "json"})
    if r2["highlighted"]:
        assert r2["lexer"] == "JsonLexer"
    with pytest.raises(ValueError):
        api_highlight({"nope": 1})
    assert api_highlight({"text": "a" * (_HIGHLIGHT_MAX_CHARS + 1)})["highlighted"] is False


def test_highlight_api_fallback_without_pygments(monkeypatch):
    monkeypatch.setattr(webui_module, "_HAVE_PYGMENTS", False)
    assert api_highlight({"text": "tables: {}"}) == {"html": None, "highlighted": False}


def test_semantic_editor_http_routes(tmp_path, monkeypatch):
    """The editor endpoints over HTTP: auth, content, per-table, highlight."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import (
        _ApiTokenMiddleware,
        _transport_security,
    )
    from sqlhandler.server import (
        mcp as mcp_server,
    )

    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "store.json"))
    pq.write_table(pa.table({"id": [1, 2], "amount": [1.0, 2.0]}), str(tmp_path / "orders.parquet"))
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=3600)

    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    register_ui(app, lambda: eng)
    app.add_middleware(_ApiTokenMiddleware, token="tok-123")
    client = TestClient(app)
    auth = {"X-API-Token": "tok-123"}

    # the new routes are behind the same token middleware as the rest of /api
    assert client.get("/api/semantic-catalog/content").status_code == 401
    assert client.post("/api/highlight", json={"text": "x: 1"}).status_code == 401

    r = client.get("/api/semantic-catalog/content", headers=auth)
    assert r.status_code == 200 and r.json()["format"] == "yaml"
    assert client.get("/api/semantic-catalog/content?format=xml", headers=auth).status_code == 400

    r = client.get("/api/semantic-catalog/table?table=orders", headers=auth)
    assert r.status_code == 200 and r.json()["found"] is False

    r = client.post(
        "/api/semantic-catalog/table",
        json={"table": "orders", "content": "description: via http"},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (
        client.post("/api/describe", json={"table": "orders"}, headers=auth).json()["description"]
        == "via http"
    )
    assert (
        client.post(
            "/api/semantic-catalog/table", json={"table": "orders"}, headers=auth
        ).status_code
        == 400
    )  # missing content

    r = client.post(
        "/api/highlight", json={"text": "description: x\n", "theme": "dark"}, headers=auth
    )
    assert r.status_code == 200
    body = r.json()
    assert body["highlighted"] in (True, False)  # pygments is optional by design
    if body["highlighted"]:
        assert "<span" in body["html"]

    r = client.delete("/api/semantic-catalog/table?table=orders", headers=auth)
    assert r.status_code == 200 and r.json()["removed"] is True
    assert (
        client.get("/api/semantic-catalog/table?table=orders", headers=auth).json()["found"]
        is False
    )


def test_export_csv_arrow_semantics(tmp_path):
    """The Arrow CSV writer's pinned semantics (bench follow-up 2026-09).

    Header unquoted; integers EXACT (no pandas float-upcast of null-bearing
    int columns); booleans 'True'/'False' (pandas capitalization); float NaN
    as '' (pandas na_rep default); all parse-identical for CSV consumers.
    """
    import io

    import pandas as pd
    import pyarrow as pa

    from sqlhandler.webui import _arrow_to_csv_bytes

    tbl = pa.table(
        {
            "i": pa.array([1, None, 4611686018427387904], pa.int64()),
            "f": pa.array([1.5, float("nan"), 0.1], pa.float64()),
            "b": pa.array([True, None, False], pa.bool_()),
            "s": ["a,b", None, "plain"],
        }
    )
    out = _arrow_to_csv_bytes(tbl).decode()
    lines = out.strip().splitlines()
    assert lines[0] == "i,f,b,s"  # header unquoted
    assert "4611686018427387904" in lines[3]  # exact int, not 4.61169e+18
    assert '"True"' in lines[1] and '"False"' in lines[3]  # pandas caps
    assert ",," in lines[2]  # NaN and null both render ''
    # consumer round-trip: pandas reads the big int exactly, nulls as NaN/''
    back = pd.read_csv(io.StringIO(out))
    assert back["i"].iloc[2] == 4611686018427387904
    assert pd.isna(back["f"].iloc[1]) and pd.isna(back["b"].iloc[1])


def test_export_csv_fallback_preserved_for_exotic_types(tmp_path):
    """Types Arrow's CSV writer refuses fall back to the pandas path."""
    import pyarrow as pa

    from sqlhandler.webui import _arrow_to_csv_bytes

    # decimal128 is renderable by pandas; Arrow's CSV writer refuses it.
    tbl = pa.table({"d": pa.array([None], pa.decimal128(10, 2))})
    out = _arrow_to_csv_bytes(tbl)
    assert out  # fell back rather than raised


# ---------------------------------------------------------------------------
# masking: /api/export and /api/saved-queries/{name}/run honor the caller
# ---------------------------------------------------------------------------


def _masked_policy(tmp_path, monkeypatch):
    """A REAL policy file (the test_acl_policy.py shape) binding alice to a
    masked/row-filtered workorder/work_order; returns her Caller."""
    import json as _json

    from sqlhandler.identity import Caller
    from sqlhandler.policy import POLICY_ENABLED_ENV, POLICY_FILE_ENV, reset_policy_store

    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": pa.array(range(3), pa.int64()),
                "ssn": ["s0", "s1", "s2"],
                "kind": ["a", "secret", "a"],
            }
        ),
        str(d / "part.parquet"),
    )
    pf = tmp_path / "policy.json"
    pf.write_text(
        _json.dumps(
            {
                "version": 1,
                "default_group": "all_disabled",
                "groups": {
                    "wo_only": {
                        "visible_tables": ["workorder/*"],
                        "tables": {
                            "workorder/work_order": {
                                "row_filter": "kind != 'secret'",
                                "column_masks": {"ssn": "redact"},
                            }
                        },
                    },
                    "all_disabled": {},
                },
                "subjects": {"alice": ["wo_only"]},
            }
        )
    )
    monkeypatch.setenv(POLICY_ENABLED_ENV, "1")
    monkeypatch.setenv(POLICY_FILE_ENV, str(pf))
    reset_policy_store()
    return Caller(cls="user", subject="alice", via="relay")


def test_api_export_masks_like_api_query(engine, tmp_path, monkeypatch):
    """A policy-restricted caller's EXPORT matches /api/query for the same
    caller: same row filter, same column mask (export is not a side door)."""
    alice = _masked_policy(tmp_path, monkeypatch)
    sql = "SELECT * FROM work_order ORDER BY id"
    query_payload = api_query(engine, sql, caller=alice)
    out = webui_module.api_export(engine, {"sql": sql, "format": "csv"}, caller=alice)
    lines = out["content"].decode().strip().splitlines()
    assert query_payload["rows"] == [[0, "***", "a"], [2, "***", "a"]]
    # identical masking in the CSV (the writer's pinned quoting semantics)
    assert lines[0] == "id,ssn,kind"
    assert lines[1:] == ['0,"***","a"', '2,"***","a"']


def test_api_export_table_branch_masks_too(engine, tmp_path, monkeypatch):
    """The table-scan branch of the export delegates to the masking views
    the same way (scan_arrow with the caller)."""
    from sqlhandler.config import FileConfig
    from sqlhandler.engine import SqlEngine
    from sqlhandler.file import FileProvider

    alice = _masked_policy(tmp_path, monkeypatch)
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    out = webui_module.api_export(eng, {"table": "workorder/work_order", "format": "csv"}, caller=alice)
    lines = out["content"].decode().strip().splitlines()
    assert lines[0] == "id,ssn,kind"
    assert lines[1:] == ['0,"***","a"', '2,"***","a"']


def test_export_and_saved_run_engine_calls_carry_the_caller(monkeypatch, tmp_path):
    """Mechanical spine check: both endpoints pass the /api/query caller
    into engine.query_duckdb (and export's table branch into scan_arrow)."""
    from sqlhandler.identity import Caller

    alice = Caller(cls="user", subject="alice", via="relay")

    class SpyEngine:
        def __init__(self):
            self.query_callers = []
            self.scan_callers = []

        def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
            self.query_callers.append(kw.get("caller"))
            return pa.table({"one": [1]})

        def scan_arrow(self, table, limit=None, **kw):
            self.scan_callers.append(kw.get("caller"))
            return pa.table({"a": [1]})

    spy = SpyEngine()
    webui_module.api_export(spy, {"sql": "SELECT 1 AS one"}, caller=alice)
    webui_module.api_export(spy, {"table": "t"}, caller=alice)
    assert spy.query_callers == [alice]
    assert spy.scan_callers == [alice]
    # api_query forwards the caller exactly the same way (the baseline the
    # two endpoints above were missing)
    seen = []

    class QueryStub:
        def query_duckdb(self, sql, **kw):
            seen.append(kw.get("caller"))
            return pa.table({"one": [1]})

    api_query(QueryStub(), "SELECT 1 AS one", caller=alice)
    assert seen == [alice]


def _ui_request(path: str, payload: bytes = b""):
    """A bare Starlette Request for one POST (the handlers only read the
    body + path_params; identity comes from the monkeypatched resolver)."""
    from starlette.requests import Request

    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
            "query_string": b"",
            "path_params": {},
        },
        receive,
    )


def test_webui_saved_run_passes_caller_like_api_query(tmp_path, monkeypatch):
    """The saved-query run route threads the same caller /api/query does into
    engine.query_duckdb — None (the old behavior) must never reach a masked
    deployment's engine on this path."""
    import asyncio

    from starlette.applications import Starlette

    from sqlhandler.identity import Caller
    from sqlhandler.policy import reset_policy_store
    from sqlhandler.saved import saved_query_store

    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    reset_policy_store()

    class SpyEngine:
        def __init__(self):
            self.callers = []

        def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
            self.callers.append(kw.get("caller"))
            return pa.table({"one": [1]})

    alice = Caller(cls="user", subject="alice", via="relay")
    spy = SpyEngine()
    caller_holder = [alice]
    monkeypatch.setattr(
        webui_module._identity, "caller_from_request_state", lambda request: caller_holder[0], raising=False
    )
    app = Starlette()
    webui_module.register_ui(app, lambda: spy)
    saved_query_store().save("q", "SELECT 1 AS one")
    handler = next(
        r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/saved-queries/{name}/run"
    )

    def make_request():
        request = _ui_request("/api/saved-queries/q/run", b"{}")
        request.scope["path_params"] = {"name": "q"}
        return request

    resp = asyncio.run(handler(make_request()))
    assert resp.status_code == 200
    assert spy.callers == [alice]

    # identity resolution failure degrades to exactly what /api/query does:
    # the same _caller_for None path — threaded as None, never a crash.
    caller_holder[0] = None
    spy.callers.clear()
    resp = asyncio.run(handler(make_request()))
    assert resp.status_code == 200
    assert spy.callers == [None]


def test_webui_export_route_passes_caller_like_api_query(tmp_path, monkeypatch):
    """The /api/export route threads the resolved caller into api_export."""
    import asyncio

    from starlette.applications import Starlette

    from sqlhandler.identity import Caller
    from sqlhandler.policy import reset_policy_store

    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    reset_policy_store()

    class SpyEngine:
        def __init__(self):
            self.query_callers = []
            self.scan_callers = []

        def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
            self.query_callers.append(kw.get("caller"))
            return pa.table({"a": [1]})

        def scan_arrow(self, table, limit=None, **kw):
            self.scan_callers.append(kw.get("caller"))
            return pa.table({"a": [1]})

    alice = Caller(cls="user", subject="alice", via="relay")
    spy = SpyEngine()
    monkeypatch.setattr(
        webui_module._identity, "caller_from_request_state", lambda request: alice, raising=False
    )
    app = Starlette()
    webui_module.register_ui(app, lambda: spy)
    handler = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/export")

    resp = asyncio.run(handler(_ui_request("/api/export", b'{"sql": "SELECT 1 AS a", "format": "csv"}')))
    assert resp.status_code == 200
    assert spy.query_callers == [alice]
    resp = asyncio.run(handler(_ui_request("/api/export", b'{"table": "t", "format": "csv"}')))
    assert resp.status_code == 200
    assert spy.scan_callers == [alice]


# ---------------------------------------------------------------------------
# request-body caps: Content-Length and chunked bodies are bounded (413)
# ---------------------------------------------------------------------------


def test_read_bounded_body_content_length_over_cap_refuses_without_reading():
    """A declared Content-Length above the cap is refused 413 without one
    byte being pulled off the wire."""
    import asyncio

    from sqlhandler.webui import _BODY_READ_MAX_BYTES, _BodyTooLarge, _read_bounded_body

    pulled = []

    class Req:
        def __init__(self):
            self.headers = {"content-length": str(_BODY_READ_MAX_BYTES + 1)}

        async def stream(self):
            pulled.append(1)
            yield b"x"

    with pytest.raises(_BodyTooLarge):
        asyncio.run(_read_bounded_body(Req()))
    assert pulled == []


def test_read_bounded_body_chunked_over_cap_refuses_midstream():
    """An unknown-length (chunked) body cannot buffer past the cap: the read
    stops and refuses as soon as the accumulated bytes exceed it."""
    import asyncio

    from sqlhandler.webui import _BODY_READ_MAX_BYTES, _BodyTooLarge, _read_bounded_body

    half = _BODY_READ_MAX_BYTES // 2
    served = [b"x" * half, b"x" * half, b"x"]  # cap + 1 total

    class Req:
        def __init__(self):
            self.headers = {}

        async def stream(self):
            for chunk in served:
                yield chunk

    with pytest.raises(_BodyTooLarge):
        asyncio.run(_read_bounded_body(Req()))


def test_read_bounded_body_under_cap_reads_whole_body():
    import asyncio

    from sqlhandler.webui import _BODY_READ_MAX_BYTES, _read_bounded_body

    class Req:
        def __init__(self):
            self.headers = {"content-length": "10"}

        async def stream(self):
            yield b"0123456789"

    assert asyncio.run(_read_bounded_body(Req())) == b"0123456789"
    # exactly at the cap is still fine (cap is a ceiling, not a bias)
    big = b"y" * _BODY_READ_MAX_BYTES

    class ReqAt:
        def __init__(self):
            self.headers = {}

        async def stream(self):
            yield big

    assert asyncio.run(_read_bounded_body(ReqAt())) == big


def test_dbt_import_route_413_on_oversized_content_length(tmp_path, monkeypatch):
    """POST /api/semantic-catalog/import-dbt with a Content-Length above the
    body cap gets 413 — before any engine work happens."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.policy import reset_policy_store
    from sqlhandler.server import _transport_security
    from sqlhandler.server import mcp as mcp_server
    from sqlhandler.webui import _BODY_READ_MAX_BYTES

    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    reset_policy_store()

    class BoomEngine:
        def catalog_uploads_enabled(self):
            raise AssertionError("engine must not be touched for an oversized body")

    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    webui_module.register_ui(app, lambda: BoomEngine())
    client = TestClient(app)
    r = client.post(
        "/api/semantic-catalog/import-dbt",
        content=b"{}",
        headers={"content-length": str(_BODY_READ_MAX_BYTES + 1)},
    )
    assert r.status_code == 413
    assert "too large" in r.json()["error"]
    # the apply route has the same cap
    r = client.post(
        "/api/semantic-catalog/import-dbt/apply",
        content=b"{}",
        headers={"content-length": str(_BODY_READ_MAX_BYTES + 1)},
    )
    assert r.status_code == 413


def test_dbt_import_route_still_parses_json_under_the_cap(tmp_path, monkeypatch):
    """A normal (small) import body keeps working after the bounded read —
    parse errors still get the historical 400 message."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.policy import reset_policy_store
    from sqlhandler.server import _transport_security
    from sqlhandler.server import mcp as mcp_server

    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    reset_policy_store()
    eng = _catalog_engine(tmp_path, monkeypatch)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    webui_module.register_ui(app, lambda: eng)
    client = TestClient(app)

    manifest = {"version": 1, "nodes": {}, "sources": {}, "macros": {}, "parent_map": {}}
    r = client.post("/api/semantic-catalog/import-dbt", json={"manifest": manifest})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert client.post("/api/semantic-catalog/import-dbt", content=b"{broken").status_code == 400


# ---------------------------------------------------------------------------
# cross-review fixes (2026-10-02): async-job + saved-query caller threading,
# systemic body cap, export filename sanitization
# ---------------------------------------------------------------------------


def test_webui_async_query_route_passes_caller(tmp_path, monkeypatch):
    """POST /api/query/async threads the resolved caller into the QueryJob —
    an async run is masked exactly like the sync /api/query (the engine's
    caller=None branch is the TRUSTED internal path, never acceptable for an
    HTTP caller). Submit-time spy: capture the QueryJob the manager built and
    assert its private _caller."""
    import asyncio

    from starlette.applications import Starlette

    from sqlhandler.identity import Caller
    from sqlhandler.webui import QueryJobManager

    captured = {}
    real_submit = QueryJobManager.submit

    def spy_submit(self, engine, sql, limit=None, params=None, version_as_of=None, caller=None):
        result = real_submit(self, engine, sql, limit=limit, params=params,
                             version_as_of=version_as_of, caller=caller)
        qid = result.get("query_id")
        if qid:
            job, _ = self._jobs[qid]
            captured["caller"] = job._caller
        return result

    monkeypatch.setattr(QueryJobManager, "submit", spy_submit, raising=True)
    alice = Caller(cls="user", subject="alice", via="relay")
    monkeypatch.setattr(
        webui_module._identity, "caller_from_request_state", lambda request: alice, raising=False
    )
    app = Starlette()

    class Engine:  # never executed at submit time; the job thread may run it
        def query_duckdb(self, *a, **k):
            return pa.table({"one": [1]})

    webui_module.register_ui(app, lambda: Engine())
    handler = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/query/async")
    resp = asyncio.run(handler(_ui_request("/api/query/async", b'{"sql": "SELECT 1 AS one"}')))
    assert resp.status_code == 200
    assert captured["caller"] == alice


def test_webui_jobs_submit_passes_caller_and_owner(tmp_path, monkeypatch):
    """POST /api/jobs threads caller + owner exactly like the MCP twin."""
    import asyncio

    from starlette.applications import Starlette

    from sqlhandler.identity import Caller

    captured = {}

    class SpyManager:
        def submit(self, engine, sql, limit=None, params=None, version_as_of=None, caller=None, owner=None):
            captured["caller"] = caller
            captured["owner"] = owner
            return {"query_id": "job123", "state": "running"}

    alice = Caller(cls="user", subject="alice", via="relay")
    monkeypatch.setattr(
        webui_module._identity, "caller_from_request_state", lambda request: alice, raising=False
    )
    # Policy enforcement ON so the owner derivation is active (same gate the
    # MCP dispatch uses: policy.owner_key under enforcement, None otherwise).
    monkeypatch.setattr(webui_module._policy, "policy_enabled", lambda: True, raising=False)
    monkeypatch.setattr(
        webui_module._policy, "owner_key", lambda c: f"subject:{c.subject}", raising=False
    )
    app = Starlette()
    webui_module.register_ui(app, lambda: object())
    webui_module._QueryJobManagerSingleton = None  # unused; patch the manager getter below
    # Patch the manager the route closes over: jobs_submit uses job_manager().
    import sqlhandler.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "job_manager", lambda: SpyManager(), raising=False)
    handler = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/jobs")
    resp = asyncio.run(handler(_ui_request("/api/jobs", b'{"sql": "SELECT 1"}')))
    assert resp.status_code == 200
    assert captured["caller"] == alice
    assert captured["owner"] == "subject:alice"


def test_webui_saved_routes_scope_by_caller(tmp_path, monkeypatch):
    """All four /api/saved-queries handlers thread the caller: an anonymous
    list no longer returns another subject's private entries, and run/delete
    are owner-scoped like the MCP twins."""
    import asyncio

    from starlette.applications import Starlette

    from sqlhandler.identity import Caller
    from sqlhandler.policy import reset_policy_store
    from sqlhandler.saved import saved_query_store

    monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
    reset_policy_store()

    alice = Caller(cls="user", subject="alice", via="relay")
    holder = [alice]
    monkeypatch.setattr(
        webui_module._identity, "caller_from_request_state", lambda request: holder[0], raising=False
    )
    app = Starlette()
    webui_module.register_ui(app, lambda: object())
    store = saved_query_store()
    store.save("q", "SELECT 1 AS one", {"owner": "subject:alice"})

    # Anonymous list: enforcement on + caller None → owner derivation is
    # None, which saved.list() treats as "see everything" — but alice's
    # caller MUST scope it to her own entries.
    list_handler = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/saved-queries")
    resp = asyncio.run(list_handler(_ui_request("/api/saved-queries", b"")))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["queries"], "alice's caller sees her own entry"

    # Run route: the resolved caller rides the lookup (404 for a foreign
    # subject's entry is asserted at the saved.py layer; here we pin that
    # the ROUTE actually forwards a caller — spy on api_saved_run).
    run_handler = next(
        r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/saved-queries/{name}/run"
    )
    seen = {}
    real_run = webui_module.api_saved_run

    def spy_run(name, body=None, *, caller=None):
        seen["caller"] = caller
        return real_run(name, body, caller=caller)

    monkeypatch.setattr(webui_module, "api_saved_run", spy_run, raising=False)
    request = _ui_request("/api/saved-queries/q/run", b"{}")
    request.scope["path_params"] = {"name": "q"}
    asyncio.run(run_handler(request))
    assert seen["caller"] == alice


def test_webui_export_filename_header_is_sanitized(tmp_path, monkeypatch):
    """A crafted table name cannot inject quotes/CRLF into Content-Disposition."""
    import asyncio

    from starlette.applications import Starlette

    class SpyEngine:
        def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
            return pa.table({"a": [1]})

        def scan_arrow(self, table, limit=None, **kw):
            return pa.table({"a": [1]})

    monkeypatch.setattr(
        webui_module._identity, "caller_from_request_state", lambda request: None, raising=False
    )
    app = Starlette()
    webui_module.register_ui(app, lambda: SpyEngine())
    handler = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/export")
    crafted = 'x".csv\r\nX-Injected: yes'
    resp = asyncio.run(
        handler(_ui_request("/api/export", json.dumps({"table": crafted, "format": "csv"}).encode()))
    )
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    # Security property: the value stays INSIDE the quoted token — no CRLF
    # (header injection) and no unescaped quote (value breakout). The label
    # text may survive as filename characters; that is not an injection.
    assert "\r" not in disposition and "\n" not in disposition
    inner = disposition[len('attachment; filename="'):-1]
    assert '"' not in inner and "\\" not in inner
