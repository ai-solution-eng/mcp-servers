"""Tests for saved parameterized queries (Wave 5 additive feature).

Covers CRUD + persistence, save-time validation (D2 guard + parse check),
the BIND-parameter contract (an injection attempt through call-time params
must behave as a literal value, never as string interpolation), the
write-mutation auth-gating matrix (configured credential → unauthenticated
writes refused; no credential → single-user-local mode allowed), and the
run-time re-application of the read-only guard against a hand-poisoned store.
"""

import json
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import saved as saved_module
from sqlhandler import server
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import TableInfo
from sqlhandler.saved import (
    NotAuthorized,
    SavedQueryStore,
    UnknownSavedQuery,
    assert_write_allowed,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Every test gets its own store file + clean auth env + fresh singleton."""
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(tmp_path / "saved-queries.json"))
    for env in ("SQLHANDLER_API_TOKEN", "MCP_API_KEYS", "SQLHANDLER_API_KEYS"):
        monkeypatch.delenv(env, raising=False)
    saved_module.reset_saved_query_store()
    yield
    saved_module.reset_saved_query_store()


def _make_engine(tmp_path):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2, 3, 4, 5], "kind": ["a", "b", "a", "b", "a"]}),
        d / "part.parquet",
    )

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    return SqlEngine(P(), cache_ttl=0)


class FakeRequest:
    """Mimics the transport's request enough for presented_credential()."""

    def __init__(self, headers=None):
        self.headers = headers or {}


# --------------------------------------------------------------------- CRUD


def test_save_list_get_delete_roundtrip():
    store = SavedQueryStore()
    entry = store.save(
        "kind-a", "SELECT * FROM work_order WHERE kind = $k", {"k": "a"}, "kind A orders"
    )
    assert entry["name"] == "kind-a"
    assert entry["sql"] == "SELECT * FROM work_order WHERE kind = $k"
    assert entry["params"] == {"k": "a"}
    assert entry["description"] == "kind A orders"
    assert entry["created_at"] and entry["updated_at"]

    listed = store.list()
    assert [e["name"] for e in listed] == ["kind-a"]

    fetched = store.get("kind-a")
    assert fetched["sql"].endswith("$k")

    assert store.delete("kind-a") is True
    assert store.list() == []
    assert store.delete("kind-a") is False  # store-level: plain False
    # the api layer is what turns "not found" into UnknownSavedQuery (→ 404):
    with pytest.raises(UnknownSavedQuery):
        saved_module.api_saved_delete("kind-a", None)


def test_upsert_preserves_created_at():
    store = SavedQueryStore()
    first = store.save("q", "SELECT 1")
    time.sleep(0.02)
    second = store.save("q", "SELECT 2")
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]
    assert store.get("q")["sql"] == "SELECT 2"


def test_persists_across_store_instances(tmp_path):
    path = str(tmp_path / "persisted.json")
    SavedQueryStore(path).save("q1", "SELECT 1 AS one", {"x": 1})
    again = SavedQueryStore(path)
    assert [e["name"] for e in again.list()] == ["q1"]
    assert again.get("q1")["params"] == {"x": 1}


@pytest.mark.parametrize(
    "name",
    ["", "   ", "a/b", "a\\b", "with\nnewline", "x" * 129, 42],
)
def test_name_validation_refuses(name):
    with pytest.raises(ValueError, match="name"):
        SavedQueryStore().save(name, "SELECT 1")


def test_store_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    store = SavedQueryStore(str(path))
    assert store.list() == []
    store.save("q", "SELECT 1")  # recovers by overwriting
    assert [e["name"] for e in store.list()] == ["q"]


# ------------------------------------------------- save-time validation (D2)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE work_order",
        "CREATE TABLE x (a int)",
        "INSERT INTO work_order VALUES (1)",
        "ATTACH 'file.db' AS exfil",
        "SELECT 1; DROP TABLE work_order",
    ],
)
def test_save_refuses_ddl_with_d2_error(sql):
    with pytest.raises(ValueError, match="SQLHANDLER_MCP_READONLY"):
        SavedQueryStore().save("evil", sql)


def test_save_refuses_unparseable_sql():
    with pytest.raises(ValueError, match="Could not parse"):
        SavedQueryStore().save("bad", "not even sql")


def test_save_with_readonly_optout_allows_ddl(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    entry = SavedQueryStore().save("ddl", "CREATE TEMP TABLE t AS SELECT 1")  # noqa: DUO123 - opt-out posture under test
    assert "CREATE" in entry["sql"]


def test_save_refuses_nested_params():
    with pytest.raises(ValueError, match="scalars"):
        SavedQueryStore().save("q", "SELECT $x", {"x": [1, 2]})


# ----------------------------------------------- bind params + injection test


def test_run_uses_bind_params_injection_is_inert(tmp_path, monkeypatch):
    """THE injection proof: a param value that is classic SQL injection runs
    as a literal string through DuckDB's bind path — 0 rows, not all rows."""
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    store = SavedQueryStore()
    store.save("by-kind", "SELECT id FROM work_order WHERE kind = $k", {"k": "a"})
    payload = json.loads(server.query_saved("by-kind", params={"k": "a"}, output_format="json"))
    assert payload["rows"] == [[1], [3], [5]]

    # the injection attempt: stored SQL must keep its placeholder
    assert "$k" in store.get("by-kind")["sql"]
    payload = json.loads(
        server.query_saved("by-kind", params={"k": "a' OR 1=1 --"}, output_format="json")
    )
    assert payload["rows"] == [], "injection must not leak other rows (bind, not interpolate)"

    # positional-? variant too
    store.save("positional", "SELECT id FROM work_order WHERE kind = ?", ["b"])
    payload = json.loads(server.query_saved("positional", output_format="json"))
    assert payload["rows"] == [[2], [4]]
    payload = json.loads(
        server.query_saved("positional", params=["x' OR '1'='1"], output_format="json")
    )
    assert payload["rows"] == []


def test_call_params_override_stored(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    store = SavedQueryStore()
    store.save("by-kind", "SELECT id FROM work_order WHERE kind = $k", {"k": "a"})
    payload = json.loads(server.query_saved("by-kind", params={"k": "b"}, output_format="json"))
    assert payload["rows"] == [[2], [4]]


def test_run_reapplies_readonly_guard_against_poisoned_store(tmp_path, monkeypatch):
    """A hand-edited store file cannot smuggle DDL past the run-time guard."""
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    store = SavedQueryStore()
    store.save("q", "SELECT 1")
    # poison the file behind the API's back
    path = tmp_path / "saved-queries.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["queries"]["evil"] = {"sql": "DROP TABLE work_order", "created_at": "x", "updated_at": "x"}
    path.write_text(json.dumps(data), encoding="utf-8")
    text, is_error = server._dispatch_tool("query_saved", {"name": "evil"})
    assert is_error is True
    assert "SQLHANDLER_MCP_READONLY" in text
    # the lake table is untouched
    assert eng.query_duckdb("SELECT count(*) AS n FROM work_order").to_pydict() == {"n": [5]}


def test_run_unknown_name_is_clean_error():
    with pytest.raises(UnknownSavedQuery, match="Unknown saved query"):
        saved_module.api_saved_run("nope", {})


# ------------------------------------------------------------- MCP tools


def test_mcp_query_save_list_delete_roundtrip(monkeypatch):
    monkeypatch.setattr(server, "_handler", lambda: None)
    text, is_error = server._dispatch_tool(
        "query_save",
        {
            "name": "orders",
            "sql": "SELECT * FROM work_order WHERE kind = $k",
            "params": {"k": "a"},
            "description": "d",
        },
    )
    assert is_error is False
    entry = json.loads(text)
    assert entry["saved"] is True and entry["name"] == "orders"

    text, is_error = server._dispatch_tool("query_list", {})
    assert is_error is False
    assert "orders" in text and "kind = $k" in text

    text, is_error = server._dispatch_tool("query_delete", {"name": "orders"})
    assert is_error is False
    assert "Deleted" in text
    text, is_error = server._dispatch_tool("query_delete", {"name": "orders"})
    assert is_error is True and "Unknown saved query" in text


def test_mcp_query_saved_run(tmp_path, monkeypatch):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    server._dispatch_tool(
        "query_save",
        {
            "name": "b-kind",
            "sql": "SELECT id FROM work_order WHERE kind = $k",
            "params": {"k": "b"},
        },
    )
    text, is_error = server._dispatch_tool("query_saved", {"name": "b-kind"})
    assert is_error is False
    assert "4" in text and "2" in text  # ids 2 and 4 in the markdown


# ------------------------------------------------------- auth-gating matrix


def test_gate_no_auth_configured_allows_writes():
    """No credential env → single-user-local mode: writes allowed."""
    assert saved_module.auth_configured() is False
    assert_write_allowed(None)  # does not raise (stdio tool call included)
    assert_write_allowed(FakeRequest({}))  # unauthenticated HTTP caller too


def test_gate_token_configured_refuses_unauthenticated_writes():
    import os

    os.environ["SQLHANDLER_API_TOKEN"] = "sekret"
    try:
        assert saved_module.auth_configured() is True
        with pytest.raises(NotAuthorized, match="SQLHANDLER_API_TOKEN"):
            assert_write_allowed(None)  # stdio: cannot present credentials
        with pytest.raises(NotAuthorized, match="credential"):
            assert_write_allowed(FakeRequest({}))  # HTTP caller without one
        with pytest.raises(NotAuthorized):
            assert_write_allowed(FakeRequest({"authorization": "Bearer wrong"}))
        with pytest.raises(NotAuthorized):
            assert_write_allowed(FakeRequest({"x-api-key": "wrong"}))
        # correct credential passes (constant-time compared)
        assert_write_allowed(FakeRequest({"authorization": "Bearer sekret"}))
        assert_write_allowed(FakeRequest({"x-api-key": "sekret"}))
        assert_write_allowed(FakeRequest({"x-api-token": "sekret"}))
    finally:
        os.environ.pop("SQLHANDLER_API_TOKEN", None)


def test_gate_mcp_keys_configured_reach_the_mcp_tools(monkeypatch):
    """MCP keys configured: the /mcp middleware authenticates the transport,
    and the write tool re-verifies per call (defense in depth — covers
    deployments where ONLY the token is set and /mcp stays open)."""
    monkeypatch.setenv("MCP_API_KEYS", "key-1, key-2")
    # MCP over HTTP with the right key: allowed
    ok = FakeRequest({"authorization": "Bearer key-2"})
    entry = saved_module.api_saved_save({"name": "q", "sql": "SELECT 1"}, ok)
    assert entry["name"] == "q"
    # without a credential: refused
    with pytest.raises(NotAuthorized):
        saved_module.api_saved_save({"name": "q2", "sql": "SELECT 1"}, FakeRequest({}))
    with pytest.raises(NotAuthorized):
        saved_module.api_saved_delete("q", FakeRequest({}))
    # with the right key: allowed
    assert saved_module.api_saved_delete("q", ok) == {"deleted": "q"}


def test_rest_saved_queries_auth_matrix(tmp_path, monkeypatch):
    """REST matrix: no auth → open; token configured → 401 without it."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _ApiTokenMiddleware, _transport_security
    from sqlhandler.server import mcp as mcp_server

    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)

    def build_app(token=None):
        app = mcp_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=_transport_security,
        )
        from sqlhandler.webui import register_ui as reg

        reg(app, lambda: eng)
        if token:
            app.add_middleware(_ApiTokenMiddleware, token=token)
        return TestClient(app)

    # no auth configured: save/delete open (single-user-local)
    client = build_app()
    r = client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT 1 AS one"})
    assert r.status_code == 200
    assert r.json()["name"] == "q"
    assert client.get("/api/saved-queries").json()["queries"][0]["name"] == "q"
    run = client.post("/api/saved-queries/q/run", json={})
    assert run.status_code == 200
    assert run.json()["rows"] == [[1]]
    assert client.delete("/api/saved-queries/q").json() == {"deleted": "q"}
    assert client.delete("/api/saved-queries/q").status_code == 404

    # token configured: unauthenticated writes refused; valid token passes
    client = build_app(token="tok-1")
    # Two independent layers refuse an unauthenticated write: the /api token
    # middleware and (for any caller that reaches it) the saved-write gate.
    # Either refusing is the contract; the gate's own message is asserted in
    # the unit tests + the MCP-keys-only REST test below.
    assert (
        client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT 1"}).status_code == 401
    )
    assert client.delete("/api/saved-queries/whatever").status_code == 401
    auth = {"X-API-Token": "tok-1"}
    assert (
        client.post(
            "/api/saved-queries", json={"name": "q", "sql": "SELECT 1 AS one"}, headers=auth
        ).status_code
        == 200
    )
    assert client.delete("/api/saved-queries/q", headers=auth).status_code == 200
    # reads follow the /api posture (token middleware), not the write gate
    assert client.get("/api/saved-queries").status_code == 401
    assert client.get("/api/saved-queries", headers=auth).status_code == 200


def test_rest_saved_queries_with_mcp_keys_only(tmp_path, monkeypatch):
    """MCP keys (not the /api token) configured: REST writes check the key
    per request — this is the poisoning gate's real teeth, since /api has no
    middleware of its own for MCP keys."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _transport_security
    from sqlhandler.server import mcp as mcp_server
    from sqlhandler.webui import register_ui

    eng = _make_engine(tmp_path)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    register_ui(app, lambda: eng)
    client = TestClient(app)

    monkeypatch.setenv("MCP_API_KEYS", "key-9")
    r = client.post("/api/saved-queries", json={"name": "q", "sql": "SELECT 1 AS one"})
    assert r.status_code == 401
    assert "MCP_API_KEYS" in r.json()["error"]
    ok = {"X-API-Key": "key-9"}
    assert (
        client.post(
            "/api/saved-queries", json={"name": "q", "sql": "SELECT 1 AS one"}, headers=ok
        ).status_code
        == 200
    )
    # reads stay open (same posture as /mcp without its own gate here)
    assert client.get("/api/saved-queries").status_code == 200
    assert client.post("/api/saved-queries/q/run", json={}).status_code == 200
    assert client.delete("/api/saved-queries/q", headers=ok).status_code == 200


def test_rest_saved_validation_and_run_errors(tmp_path, monkeypatch):
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _transport_security
    from sqlhandler.server import mcp as mcp_server
    from sqlhandler.webui import register_ui

    eng = _make_engine(tmp_path)
    app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )
    register_ui(app, lambda: eng)
    client = TestClient(app)

    assert (
        client.post("/api/saved-queries", json={"name": "q", "sql": "DROP TABLE x"}).status_code
        == 400
    )
    assert (
        client.post("/api/saved-queries", json={"name": "a/b", "sql": "SELECT 1"}).status_code
        == 400
    )
    assert client.post("/api/saved-queries", json={"sql": "SELECT 1"}).status_code == 400
    assert client.post("/api/saved-queries/nope/run", json={}).status_code == 404
    bad = client.post(
        "/api/saved-queries", json={"name": "q", "sql": "SELECT $x", "params": {"x": [1]}}
    )
    assert bad.status_code == 400


def test_mcp_over_http_write_gate_end_to_end(tmp_path, monkeypatch):
    """The per-call write gate through the REAL /mcp transport (POST only —
    never GET /mcp, fleet convention).

    Unique coverage here: with ONLY SQLHANDLER_API_TOKEN configured the /mcp
    transport itself stays open (the middleware gates on the MCP keys), so
    the tool-level per-call credential check is the poisoning gate. This
    proves the transport's HTTP request actually reaches the tool dispatch.
    """
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _build_http_app

    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "tok-e2e")
    app = _build_http_app()
    with TestClient(app) as client:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        def rpc(method, params, rid=1, extra=None):
            h = dict(headers)
            if extra:
                h.update(extra)
            return client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                headers=h,
            )

        init = rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
            rid=0,
        )
        assert init.status_code == 200

        def call_tool(name, args, rid, extra=None):
            body = rpc("tools/call", {"name": name, "arguments": args}, rid=rid, extra=extra).json()
            result = body.get("result", {})
            return result.get("isError", False), result["content"][0]["text"]

        err, text = call_tool("query_save", {"name": "q", "sql": "SELECT 1"}, rid=2)
        assert err is True
        assert "SQLHANDLER_API_TOKEN" in text
        err, text = call_tool(
            "query_save",
            {"name": "q", "sql": "SELECT 1 AS one"},
            rid=3,
            extra={"Authorization": "Bearer tok-e2e"},
        )
        assert err is False
        assert json.loads(text)["saved"] is True
