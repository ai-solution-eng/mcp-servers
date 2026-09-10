"""Tests for the read-only web UI / JSON API layer (``sqlhandler.webui``)."""

import datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import FileConfig
from sqlhandler.engine import SqlEngine
from sqlhandler.file import FileProvider
from sqlhandler.webui import (
    _clamp_limit,
    api_catalog_clear,
    api_catalog_status,
    api_catalog_upload,
    api_describe,
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
    pq.write_table(pa.table({"a": [1, 2, 3, 4, 5], "s": ["a", "b", "a", "b", "a"]}), d / "part.parquet")
    return SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)


# ---------------------------------------------------------------------------
# semantic catalog: status / upload (JSON or YAML) / clear
# ---------------------------------------------------------------------------


_YAML_CATALOG = (
    b"tables:\n"
    b"  orders:\n"
    b"    description: Order headers (uploaded)\n"
    b"    columns:\n"
    b"      qty: Quantity in units\n"
)


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
    assert client.post("/api/semantic-catalog", content=yaml_catalog, headers=auth).json()["tables"] == 1
    assert client.post("/api/describe", json={"table": "orders"}, headers=auth).json()["description"] == "over HTTP"
    assert client.get("/api/semantic-catalog", headers=auth).json()["active_source"] == "upload"

    assert client.post(
        "/api/semantic-catalog", content=b'{"tables": {"orders": {"description": "v2"}}}', headers=auth
    ).status_code == 200
    assert client.post("/api/describe", json={"table": "orders"}, headers=auth).json()["description"] == "v2"

    assert client.post("/api/semantic-catalog", content=b"{broken", headers=auth).status_code == 400
    assert client.delete("/api/semantic-catalog", headers=auth).json()["removed"] is True
    assert "description" not in client.post("/api/describe", json={"table": "orders"}, headers=auth).json()
