"""Tests for the MCP Inspector tab bridge (``/api/inspector/*``).

The web UI's Inspector tab is a thin MCP-inspector: it lists the SAME tool
surface an MCP client's tools/list sees (server.mcp_tool_specs — one source of
truth, ``_TOOLS``) and dispatches calls through the server's own tools/call
dispatcher (``server._dispatch_tool``), so identity, policy, caching, the
audit trail and the saved-query write gate behave exactly as over /mcp.

Covered here:
  * /api/inspector/tools → 18 tools, MCP-shaped (name/description/inputSchema);
    describe_table's advertised schema requires ``table``.
  * /api/inspector/call describe_table on a local-file engine (the same app
    construction test_webui uses: streamable_http_app + register_ui) returns
    the column name in content[0].text.
  * a bad table name surfaces the engine's did-you-mean / fix_hints text
    (run_sql path; describe_table's own handler catches and reports without
    the hint layer — both render as tool text, HTTP 200).
  * an unknown tool name and a malformed bridge request are the ONLY HTTP
    4xx shapes (bridge validation); unknown tools still answer MCP-shaped
    isError content with HTTP 200 when dispatched.
  * query_save/query_delete through the bridge behave exactly as the MCP
    tools do in the same posture: no credential configured → single-user
    local mode allows the write (matching tests/test_saved_queries.py); a
    configured credential gates it (NotAuthorized → isError content).
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import saved as saved_module
from sqlhandler import server as server_module
from sqlhandler.config import FileConfig
from sqlhandler.engine import SqlEngine
from sqlhandler.file import FileProvider
from sqlhandler.webui import register_ui


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    """Own catalog/saved-query stores + clean auth env (conftest shape)."""
    monkeypatch.setenv("SQLHANDLER_CATALOG_STORE", str(tmp_path / "catalog-store.json"))
    monkeypatch.setenv("SQLHANDLER_SAVED_QUERIES_PATH", str(tmp_path / "saved-queries.json"))
    for env in ("SQLHANDLER_API_TOKEN", "MCP_API_KEYS", "SQLHANDLER_API_KEYS"):
        monkeypatch.delenv(env, raising=False)
    saved_module.reset_saved_query_store()
    yield
    saved_module.reset_saved_query_store()


@pytest.fixture()
def eng(tmp_path):
    pq.write_table(
        pa.table({"id": [1, 2, 3], "name": ["x", "y", "z"], "qty": [1.5, 2.5, 3.5]}),
        str(tmp_path / "orders.parquet"),
    )
    return SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=3600)


@pytest.fixture()
def client(eng, monkeypatch):
    """The real app stack (streamable HTTP + register_ui) over the file engine.

    The dispatcher is server._dispatch_tool — which reads the process-wide
    ``_handler`` — so the tests patch it to the local engine exactly the way
    tests/test_saved_queries.py drives the MCP tools.
    """
    monkeypatch.setattr(server_module, "_handler", lambda: eng)
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    app = server_module.mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=server_module._transport_security,
    )
    register_ui(app, lambda: eng)
    # _CallerIdentityMiddleware is part of the production stack (_build_http_app);
    # added here so the bridge's caller resolution is exercised as deployed.
    app.add_middleware(server_module._CallerIdentityMiddleware)
    return TestClient(app)


def call_tool(client, name, arguments=None):
    r = client.post("/api/inspector/call", json={"name": name, "arguments": arguments or {}})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert set(payload) == {"content", "isError"}
    assert isinstance(payload["content"], list) and payload["content"]
    assert payload["content"][0]["type"] == "text"
    return payload["isError"], payload["content"][0]["text"]


# ---------------------------------------------------------------------------
# GET /api/inspector/tools — the tools/list payload
# ---------------------------------------------------------------------------


def test_inspector_tools_payload(client):
    r = client.get("/api/inspector/tools")
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert len(tools) == 18
    for t in tools:
        assert set(t) == {"name", "description", "inputSchema"}
        assert isinstance(t["name"], str) and t["name"]
        assert isinstance(t["description"], str)
        assert t["inputSchema"].get("type") == "object"


def test_inspector_tools_describe_table_requires_table(client):
    tools = client.get("/api/inspector/tools").json()["tools"]
    describe = next(t for t in tools if t["name"] == "describe_table")
    assert describe["inputSchema"].get("required") == ["table"]
    assert "table" in describe["inputSchema"]["properties"]


def test_inspector_tools_matches_mcp_tools_list(client):
    """One source of truth: the bridge payload IS the MCP tools/list payload."""
    from sqlhandler.server import _TOOLS

    tools = client.get("/api/inspector/tools").json()["tools"]
    assert [t["name"] for t in tools] == [t.name for t in _TOOLS]


def test_inspector_tools_no_auth_configured_is_open(client):
    # /api posture: without SQLHANDLER_API_TOKEN the route is open — same as
    # every other GET /api/* on a token-less deployment.
    assert client.get("/api/inspector/tools").status_code == 200


# ---------------------------------------------------------------------------
# POST /api/inspector/call — the tools/call bridge
# ---------------------------------------------------------------------------


def test_inspector_call_describe_table_returns_columns(client):
    is_err, text = call_tool(client, "describe_table", {"table": "orders"})
    assert is_err is False
    assert "id" in text and "qty" in text and "Table: orders" in text


def test_inspector_call_run_sql_roundtrip(client):
    is_err, text = call_tool(
        client, "run_sql", {"sql": "SELECT COUNT(*) AS n FROM orders", "output_format": "json"}
    )
    assert is_err is False
    assert json.loads(text.rstrip().rsplit("\n", 1)[-1] if text.lstrip().startswith("{") is False else text)
    assert "3" in text


def test_inspector_call_run_sql_bad_table_did_you_mean(client):
    """sqlguard/did-you-mean errors render as tool text, HTTP 200."""
    _is_err, text = call_tool(client, "run_sql", {"sql": "SELECT * FROM orderz"})
    assert "orderz" in text
    assert "Did you mean" in text or "not found" in text or "E_TABLE_NOT_FOUND" in text


def test_inspector_call_describe_bad_table_reports_cleanly(client):
    # describe_table's own handler catches the resolution error and returns it
    # as tool text (the same shape an MCP client sees — an inspector shows it,
    # it does not HTTP-fail).
    _is_err, text = call_tool(client, "describe_table", {"table": "orderz"})
    assert "Error describing table" in text
    assert "orderz" in text


def test_inspector_call_unknown_tool_is_mcp_shaped_not_http(client):
    """An unknown TOOL is the dispatcher's error (isError content, HTTP 200);
    only BRIDGE validation (below) is a 4xx."""
    is_err, text = call_tool(client, "definitely_not_a_tool")
    assert is_err is True
    assert "Unknown tool" in text or "definitely_not_a_tool" in text


def test_inspector_call_missing_required_arg_is_param_invalid(client):
    is_err, text = call_tool(client, "describe_table", {})
    assert is_err is True
    assert "Missing required argument" in text and "'table'" in text


def test_inspector_call_bridge_validation_http_4xx(client):
    # malformed body / wrong shapes are the BRIDGE's errors — HTTP 4xx.
    assert client.post("/api/inspector/call", content=b"{not json").status_code == 400
    assert client.post("/api/inspector/call", content=b"[1,2]", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/inspector/call", json={"arguments": {}}).status_code == 400  # no name
    assert client.post("/api/inspector/call", json={"name": "   "}).status_code == 400
    assert client.post("/api/inspector/call", json={"name": "run_sql", "arguments": "oops"}).status_code == 400
    # arguments omitted entirely is VALID (tools with no args are callable)
    assert client.post("/api/inspector/call", json={"name": "list_tables"}).status_code == 200


def test_inspector_call_list_tables_no_args(client):
    is_err, text = call_tool(client, "list_tables")
    assert is_err is False
    assert "orders" in text


# ---------------------------------------------------------------------------
# query_save / query_delete through the bridge — the shared mutation gate
# ---------------------------------------------------------------------------


def test_inspector_query_save_open_local_mode(client):
    """No credential configured: the bridge behaves exactly as the MCP tool
    does in the same posture — single-user local mode allows the write."""
    is_err, text = call_tool(client, "query_save", {"name": "q", "sql": "SELECT 1 AS one"})
    assert is_err is False
    payload = json.loads(text)
    assert payload["saved"] is True
    assert payload["name"] == "q"
    # and query_list sees it (same store the MCP tools use)
    is_err, text = call_tool(client, "query_list")
    assert is_err is False
    assert "q" in text


def test_inspector_query_save_gated_with_credential(client, monkeypatch):
    """With SQLHANDLER_API_TOKEN configured, an unauthenticated bridge call is
    refused exactly as the MCP tool refuses it (mutation gate, not HTTP)."""
    monkeypatch.setenv("SQLHANDLER_API_TOKEN", "tok-inspector")
    is_err, text = call_tool(client, "query_save", {"name": "q", "sql": "SELECT 1"})
    assert is_err is True
    assert "SQLHANDLER_API_TOKEN" in text
    # the write never landed
    is_err, text = call_tool(client, "query_list")
    assert is_err is False
    assert "No saved queries" in text


def test_inspector_query_save_select_only_guard(client):
    """Save-time read-only guard: the same guard the MCP tool applies."""
    is_err, text = call_tool(client, "query_save", {"name": "bad", "sql": "DROP TABLE x"})
    assert is_err is True
    assert "read-only" in text.lower() or "SELECT" in text


def test_inspector_routes_behind_api_token(tmp_path, monkeypatch):
    """The shared /api posture: with SQLHANDLER_API_TOKEN set, the inspector
    routes are gated by the SAME middleware as every other /api/* route."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _ApiTokenMiddleware

    pq.write_table(
        pa.table({"id": [1, 2]}), str(tmp_path / "orders.parquet")
    )
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=3600)
    monkeypatch.setattr(server_module, "_handler", lambda: eng)
    app = server_module.mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=server_module._transport_security,
    )
    register_ui(app, lambda: eng)
    app.add_middleware(_ApiTokenMiddleware, token="tok-123")
    c = TestClient(app)
    assert c.get("/api/inspector/tools").status_code == 401
    assert c.post("/api/inspector/call", json={"name": "list_tables"}).status_code == 401
    auth = {"X-API-Token": "tok-123"}
    assert c.get("/api/inspector/tools", headers=auth).status_code == 200
    assert len(c.get("/api/inspector/tools", headers=auth).json()["tools"]) == 18
    r = c.post("/api/inspector/call", json={"name": "list_tables"}, headers=auth)
    assert r.status_code == 200 and r.json()["isError"] is False
