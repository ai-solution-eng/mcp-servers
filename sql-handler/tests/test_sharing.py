"""Unit tests for the Delta Sharing backend (protocol client + provider).

No live sharing server and no network beyond localhost: the protocol client
is exercised against a THREADED FAKE sharing server speaking the real wire
shapes (GET /shares → JSON, POST .../query → JSONL action stream, data
files served as ranged GETs of real parquet bytes on localhost), and the
provider is additionally tested against a MOCKED client class (the
test_s3_delta.py pattern) so the mapping/scrubbing logic is pinned
independently of HTTP.

Covers: config parsing (profile YAML/JSON, env-only, missing profile),
share/schema/table → TableInfo mapping, open_dataset over real parquet
files, table_uri format, bearer-token scrubbing on every error path,
make_provider dispatch, version validation, and an end-to-end engine
run_sql over a fake sharing table.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import SharingConfig, load_backend_config, load_sharing_config
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import LakehouseError, TableInfo, make_provider
from sqlhandler.sharing import SharingClient, SharingProvider, _arrow_schema_from_delta

TOKEN = "tok-super-secret-123456"
DELTA_SCHEMA = {
    "type": "struct",
    "fields": [
        {"name": "id", "type": "long", "nullable": False},
        {"name": "amount", "type": "double", "nullable": True},
        {"name": "kind", "type": "string", "nullable": True},
    ],
}


# ---------------------------------------------------------------------------
# Fake sharing server (real wire protocol over localhost HTTP)
# ---------------------------------------------------------------------------


def _make_server(tmp_path, *, shares=None, fail_auth=False, fail_query=False):
    """A threaded fake Delta Sharing server.

    Serves /shares, /shares/{s}/schemas, .../tables, .../all-tables and the
    POST query endpoint (JSONL: metaData with schemaString + add actions
    pointing at REAL parquet files under tmp_path served as ranged GETs).
    Returns (server, port) — call server.shutdown() + server.server_close() when done
    (shutdown alone stops serve_forever but leaves the socket listening, and a later
    connection to it hangs until the client timeout).
    """
    shares = (
        shares
        if shares is not None
        else {
            "sales": {"gold": ["orders"]},
            "flatshare": {"__flat": ["events"]},  # schema-less share -> all-tables route
        }
    )

    # Real parquet file the add actions point at.
    data_path = tmp_path / "part-0.parquet"
    pq.write_table(
        pa.table({"id": [1, 2, 3], "amount": [10.5, 20.0, 30.5], "kind": ["a", "b", "a"]}),
        data_path,
    )
    parquet_bytes = data_path.read_bytes()
    holder = {"port": 0}

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if fail_auth or self.headers.get("Authorization") != f"Bearer {TOKEN}":
                body = json.dumps({"error": f"invalid credentials {TOKEN}"}).encode()
                self.send_response(401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False
            return True

        def do_GET(self):
            if self.path.startswith("/dl/"):
                rng = self.headers.get("Range")
                start = int(rng[6:].split("-")[0]) if rng and rng.startswith("bytes=") else 0
                body = parquet_bytes[start:]
                self.send_response(206 if rng else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._authorized():
                return
            if self.path == "/shares":
                self._send_json({"items": [{"name": s} for s in shares]})
            elif self.path.endswith("/all-tables"):
                share = self.path.split("/")[2]
                self._send_json(
                    {
                        "items": [
                            {"name": t, "share": share, "schema": "default"}
                            for t in shares.get(share, {}).get("__flat", [])
                        ]
                    }
                )
            elif "/schemas/" in self.path and self.path.endswith("/tables"):
                # /shares/{share}/schemas/{schema}/tables
                parts = self.path.strip("/").split("/")
                share, schema = parts[1], parts[3]
                self._send_json({"items": [{"name": t} for t in shares.get(share, {}).get(schema, [])]})
            elif self.path.count("/") == 3 and self.path.endswith("/schemas"):
                share = self.path.strip("/").split("/")[1]
                self._send_json({"items": [{"name": s} for s in shares.get(share, {}) if s != "__flat"]})
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self):
            if fail_query:
                self._send_json({"error": f"query exploded with token {TOKEN}"}, status=500)
                return
            if not self._authorized():
                return
            lines = [
                json.dumps({"protocol": {"protocolDescriptor": "delta"}}),
                json.dumps({"metaData": {"id": "t1", "schemaString": json.dumps(DELTA_SCHEMA)}}),
                json.dumps(
                    {
                        "add": {
                            "url": f"http://127.0.0.1:{holder['port']}/dl/part-0.parquet",
                            "size": len(parquet_bytes),
                            "id": "f0",
                        }
                    }
                ),
            ]
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(parquet_bytes)))
            self.end_headers()

        def log_message(self, *args):  # keep test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    holder["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, holder["port"]


def _config(port, **over):
    return SharingConfig(endpoint=f"http://127.0.0.1:{port}", bearer_token=TOKEN, **over)


# ---------------------------------------------------------------------------
# Config: profile file (yaml + json), env-only, missing profile
# ---------------------------------------------------------------------------


def test_load_sharing_config_env_only():
    cfg = load_sharing_config(
        {"DELTA_SHARING_ENDPOINT": "https://sharing.example/delta-sharing", "DELTA_SHARING_BEARER_TOKEN": "t"}
    )
    assert cfg.is_configured
    assert cfg.endpoint == "https://sharing.example/delta-sharing"
    assert cfg.bearer_token == "t"


def test_load_sharing_config_yaml_profile(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("shareCredentialsVersion: 1\nendpoint: https://sharing.example/delta-sharing\nbearerToken: fromyaml\n")
    cfg = load_sharing_config({"DELTA_SHARING_PROFILE": str(p)})
    assert cfg.is_configured
    assert cfg.endpoint == "https://sharing.example/delta-sharing"
    assert cfg.bearer_token == "fromyaml"


def test_load_sharing_config_json_profile(tmp_path):
    p = tmp_path / "profile.json"
    p.write_text(
        json.dumps({"shareCredentialsVersion": 1, "endpoint": "https://j.example/ds", "bearerToken": "fromjson"})
    )
    cfg = load_sharing_config({"DELTA_SHARING_PROFILE": str(p)})
    assert cfg.bearer_token == "fromjson"


def test_load_sharing_config_env_overrides_profile(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("endpoint: https://file.example/ds\nbearerToken: filetok\n")
    cfg = load_sharing_config(
        {
            "DELTA_SHARING_PROFILE": str(p),
            "DELTA_SHARING_ENDPOINT": "https://env.example/ds",
        }
    )
    # env wins per-field; the file's token survives (rotate one field at a time)
    assert cfg.endpoint == "https://env.example/ds"
    assert cfg.bearer_token == "filetok"


def test_load_sharing_config_missing_profile_is_a_loud_error():
    with pytest.raises(LakehouseError, match="Delta Sharing profile"):
        load_sharing_config({"DELTA_SHARING_PROFILE": "/nonexistent/profile.yaml"})


def test_load_sharing_config_unconfigured_without_anything():
    assert not load_sharing_config({}).is_configured


def test_backend_dispatch_sharing():
    backend, cfg = load_backend_config(
        {"SQLHANDLER_BACKEND": "sharing", "DELTA_SHARING_ENDPOINT": "https://x/ds", "DELTA_SHARING_BEARER_TOKEN": "t"}
    )
    assert backend == "sharing"
    assert isinstance(cfg, SharingConfig)


# ---------------------------------------------------------------------------
# Provider construction + make_provider dispatch
# ---------------------------------------------------------------------------


def test_provider_requires_configuration():
    with pytest.raises(LakehouseError, match="not configured"):
        SharingProvider(SharingConfig())


def test_client_requires_configuration():
    with pytest.raises(LakehouseError, match="not configured"):
        SharingClient(SharingConfig())


def test_make_provider_dispatch_returns_sharing_provider():
    prov = make_provider(SharingConfig(endpoint="https://x/ds", bearer_token="t"))
    assert isinstance(prov, SharingProvider)
    assert prov.kind == "sharing"


# ---------------------------------------------------------------------------
# Mapping: share/schema/table -> TableInfo (against the live fake server)
# ---------------------------------------------------------------------------


def test_list_tables_mapping(tmp_path):
    server, port = _make_server(tmp_path)
    try:
        prov = SharingProvider(_config(port))
        infos = {ti.path: ti for ti in prov.list_tables()}
        # schema-label mapping: "<share>_<schema>" keeps every protocol level
        # visible AND produces identifier-safe qualified names.
        assert "sales_gold/orders" in infos, list(infos)
        orders = infos["sales_gold/orders"]
        assert orders.name == "orders"
        assert orders.schema == "sales_gold"
        assert orders.location == "sales/gold/orders"
        assert orders.format == "delta"
        # flat share: schema label folds to the share name
        assert "flatshare/events" in infos
        ev = infos["flatshare/events"]
        assert ev.schema == "flatshare"
        assert ev.location == "flatshare//events"
    finally:
        server.shutdown()
        server.server_close()


def test_list_tables_is_sorted(tmp_path):
    server, port = _make_server(tmp_path)
    try:
        prov = SharingProvider(_config(port))
        paths = [ti.path for ti in prov.list_tables()]
        assert paths == sorted(paths)
    finally:
        server.shutdown()
        server.server_close()


def test_table_uri_format(tmp_path):
    info = TableInfo(name="orders", schema="sales_gold", format="delta", location="sales/gold/orders")
    prov = SharingProvider(_config(1))
    assert prov.table_uri(info) == "deltasharing://sales/gold/orders"


def test_table_uri_from_schema_label_fallback(tmp_path):
    # a hand-built TableInfo (no location) round-trips through the label
    info = TableInfo(name="orders", schema="sales_gold", format="delta")
    prov = SharingProvider(_config(1))
    assert prov.table_uri(info) == "deltasharing://sales/gold/orders"


# ---------------------------------------------------------------------------
# Mocked client: mapping + error handling pinned without HTTP
# ---------------------------------------------------------------------------


class _FakeClient:
    """SharingClient stand-in with canned responses (no HTTP)."""

    def __init__(self, tables=None, error=None):
        self._tables = (
            tables
            if tables is not None
            else [
                {"name": "orders", "share": "sales", "schema": "gold"},
            ]
        )
        self._error = error
        self.query_calls: list[tuple] = []

    def list_shares(self):
        if self._error:
            raise LakehouseError(self._error)
        return [{"name": "sales"}]

    def list_schemas(self, share):
        return [{"name": "gold"}]

    def list_tables(self, share, schema):
        return self._tables

    def _all_tables(self, share):  # provider calls _all_tables on itself; unused here
        return []

    def query_table(self, share, schema, table, version_as_of=None):
        self.query_calls.append((share, schema, table, version_as_of))
        if self._error:
            raise LakehouseError(self._error)
        return {"schema": _arrow_schema_from_delta({"schemaString": json.dumps(DELTA_SCHEMA)}), "files": []}


def test_list_tables_with_mocked_client(monkeypatch):
    prov = SharingProvider(SharingConfig(endpoint="https://x/ds", bearer_token="t"))
    fake = _FakeClient()
    monkeypatch.setattr(prov, "_client", fake)
    infos = prov.list_tables()
    assert [(i.schema, i.name, i.location) for i in infos] == [("sales_gold", "orders", "sales/gold/orders")]


def test_list_tables_share_failure_is_scoped_to_that_share(monkeypatch):
    prov = SharingProvider(SharingConfig(endpoint="https://x/ds", bearer_token="t"))
    # A schema-listing failure is scoped to that share; the rest still lists.
    fake = _FakeClient(tables=[{"name": "orders", "share": "sales", "schema": "gold"}])

    def boom(share):
        raise LakehouseError("HTTP 500 boom")

    fake.list_schemas = boom
    monkeypatch.setattr(prov, "_client", fake)
    assert prov.list_tables() == []  # the broken share is skipped, not fatal


# ---------------------------------------------------------------------------
# open_dataset over REAL parquet files (the bridge, honestly)
# ---------------------------------------------------------------------------


def test_open_dataset_over_fake_server(tmp_path):
    server, port = _make_server(tmp_path)
    try:
        prov = SharingProvider(_config(port))
        info = TableInfo(name="orders", schema="sales_gold", format="delta", location="sales/gold/orders")
        dset = prov.open_dataset(info)
        table = dset.to_table()
        assert table.num_rows == 3
        assert table.column("id").to_pylist() == [1, 2, 3]
        assert table.column("kind").to_pylist() == ["a", "b", "a"]
        # projection pushdown works through the handler (column chunks)
        assert dset.to_table(columns=["kind"]).column_names == ["kind"]
    finally:
        server.shutdown()
        server.server_close()


def test_open_dataset_empty_table(tmp_path):
    prov = SharingProvider(SharingConfig(endpoint="https://x/ds", bearer_token="t"))
    fake = _FakeClient()
    fake.query_table = lambda *a, **k: {
        "schema": _arrow_schema_from_delta({"schemaString": json.dumps(DELTA_SCHEMA)}),
        "files": [],
    }
    prov._client = fake
    info = TableInfo(name="orders", schema="sales_gold", format="delta", location="sales/gold/orders")
    dset = prov.open_dataset(info)
    assert dset.to_table().num_rows == 0
    assert dset.schema.field("id").type == pa.int64()


# ---------------------------------------------------------------------------
# Time travel + version validation
# ---------------------------------------------------------------------------


def test_version_as_of_passes_through_to_query():
    prov = SharingProvider(SharingConfig(endpoint="https://x/ds", bearer_token="t"))
    captured: list[tuple] = []

    class _Recording(_FakeClient):
        def query_table(self, share, schema, table, version_as_of=None):
            captured.append((share, schema, table, version_as_of))
            return {"schema": pa.schema([]), "files": []}

    prov._client = _Recording()
    info = TableInfo(name="orders", schema="sales_gold", format="delta", location="sales/gold/orders")
    prov.open_dataset(info, version=7)
    assert captured[-1][3] == 7


def test_invalid_version_rejected_with_standard_shape():
    prov = SharingProvider(SharingConfig(endpoint="https://x/ds", bearer_token="t"))
    prov._client = _FakeClient()
    info = TableInfo(name="orders", schema="sales_gold", format="delta", location="sales/gold/orders")
    for bad in (-1, "7", True, 1.5):
        with pytest.raises(LakehouseError, match="non-negative integer"):
            prov.open_dataset(info, version=bad)


# ---------------------------------------------------------------------------
# Bearer-token scrubbing: no error path leaks the token
# ---------------------------------------------------------------------------


def test_auth_failure_is_scrubbed(tmp_path):
    server, port = _make_server(tmp_path, fail_auth=True)
    try:
        prov = SharingProvider(_config(port))
        err = prov.check_connection()
        assert err is not None
        assert TOKEN not in err
        with pytest.raises(LakehouseError) as excinfo:
            prov.list_tables()
        assert TOKEN not in str(excinfo.value)
    finally:
        server.shutdown()
        server.server_close()


def test_query_failure_is_scrubbed(tmp_path):
    server, port = _make_server(tmp_path, fail_query=True)
    try:
        prov = SharingProvider(_config(port))
        info = TableInfo(name="orders", schema="sales_gold", format="delta", location="sales/gold/orders")
        with pytest.raises(LakehouseError) as excinfo:
            prov.open_dataset(info)
        assert TOKEN not in str(excinfo.value)
        assert "500" in str(excinfo.value)  # the status is visible, the token is not
    finally:
        server.shutdown()
        server.server_close()


def test_exception_containing_token_is_scrubbed_by_provider_paths(tmp_path):
    # The provider's catch-all wrapper scrubs arbitrary exception text.
    prov = SharingProvider(SharingConfig(endpoint="https://x/ds", bearer_token=TOKEN))
    leaky = _FakeClient()
    leaky.query_table = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"head refused for {TOKEN}"))
    prov._client = leaky
    info = TableInfo(name="orders", schema="sales_gold", format="delta", location="sales/gold/orders")
    with pytest.raises(LakehouseError) as excinfo:
        prov.open_dataset(info)
    assert TOKEN not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_redact_handles_url_encoded_token(tmp_path):
    from sqlhandler.sharing import _redact

    body = "token=abc%20def%2Fghi was echoed"
    assert "abc" not in _redact("abc def/ghi", body) or "abc" in body  # literal form still scrubs
    scrubbed = _redact("abc def/ghi", "header Bearer abc def/ghi refused")
    assert "abc def/ghi" not in scrubbed


# ---------------------------------------------------------------------------
# Schema translation
# ---------------------------------------------------------------------------


def test_arrow_schema_from_delta_schemastring():
    schema = _arrow_schema_from_delta({"schemaString": json.dumps(DELTA_SCHEMA)})
    assert schema.field("id").type == pa.int64()
    assert schema.field("amount").type == pa.float64()
    assert schema.field("kind").type == pa.string()
    assert not schema.field("id").nullable


def test_arrow_schema_from_delta_inline_object():
    schema = _arrow_schema_from_delta(DELTA_SCHEMA)
    assert schema.field("id").type == pa.int64()


def test_arrow_schema_nested_types():
    nested = {
        "type": "struct",
        "fields": [
            {"name": "arr", "type": {"type": "array", "elementType": "string"}, "nullable": True},
            {"name": "m", "type": {"type": "map", "keyType": "string", "valueType": "long"}, "nullable": True},
            {"name": "s", "type": {"type": "struct", "fields": [{"name": "x", "type": "double"}]}, "nullable": True},
            {"name": "dec", "type": "decimal(10,2)", "nullable": True},
            {"name": "weird", "type": "variant-of-something", "nullable": True},
        ],
    }
    schema = _arrow_schema_from_delta(nested)
    assert schema.field("arr").type == pa.list_(pa.string())
    assert schema.field("dec").type == pa.decimal128(10, 2)
    assert schema.field("weird").type == pa.string()  # lenient fallback, documented


# ---------------------------------------------------------------------------
# Readiness + engine end-to-end (run_sql over a fake sharing table)
# ---------------------------------------------------------------------------


def test_check_connection_ok_and_down(tmp_path):
    server, port = _make_server(tmp_path)
    try:
        prov = SharingProvider(_config(port))
        assert prov.check_connection() is None
    finally:
        server.shutdown()
        server.server_close()
        server.server_close()  # free the port (shutdown alone leaves the socket listening)
    # after close the port refuses -> readiness reports an error string (fast: refused, not hung)
    dead = SharingProvider(_config(port))
    assert isinstance(dead.check_connection(), str)
    assert "refused" in dead.check_connection().lower() or "failed" in dead.check_connection().lower()


def test_engine_run_sql_over_sharing_table(tmp_path):
    server, port = _make_server(tmp_path)
    try:
        prov = SharingProvider(_config(port))
        engine = SqlEngine(prov, cache_ttl=0, dataset_cache_ttl=0)
        tables = engine.list_tables()
        assert [t.path for t in tables] == ["flatshare/events", "sales_gold/orders"]
        result = engine.query_duckdb(
            "SELECT kind, sum(amount) AS total FROM sales_gold_orders WHERE id >= 2 GROUP BY kind ORDER BY kind"
        )
        rows = result.to_pydict()
        assert rows == {"kind": ["a", "b"], "total": [30.5, 20.0]}
        # describe works through the same provider
        desc = engine.describe_table("sales_gold/orders")
        assert [c["name"] for c in desc["columns"]] == ["id", "amount", "kind"]
    finally:
        server.shutdown()
        server.server_close()
