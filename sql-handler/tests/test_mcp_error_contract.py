"""P1-7/P1-8: tool failures carry isError=true; run_sql's hint follows the write tier.

Every tool catches internally and returns an error STRING — the text is the
agent-facing contract and stays byte-identical — but the MCP tools/call
result must FLAG the failure (isError=true) so clients can branch on it. A
_ToolError marker subclass carries the flag from the tool function to
_dispatch_tool without changing any text.

Run: python -m pytest tests/test_mcp_error_contract.py -v
"""

import pytest

from sqlhandler import server
from sqlhandler.server import _listed_tools, _ToolError


@pytest.fixture()
def stub_engine(tmp_path, monkeypatch):
    """File-backed engine over one tiny parquet table (the
    test_dispatch_arg_contract stub pattern: real query path, no network)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_ADMIN_KEYS_FILE"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2], "kind": ["a", "b"], "amount": [10.0, 20.0]}),
        d / "part.parquet",
    )

    from sqlhandler.engine import SqlEngine
    from sqlhandler.provider import DataProvider, TableInfo

    class P(DataProvider):
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    monkeypatch.setattr(server, "_handler", lambda: SqlEngine(P(), cache_ttl=0))


# ------------------------------------------------------- the flag (P1-7)


def test_failing_run_sql_is_error_true_same_text(stub_engine):
    text, is_error = server._dispatch_tool("run_sql", {"sql": "SELECT * FROM no_such_table"})
    assert is_error is True
    assert text.startswith("Error running SQL:")
    assert "no_such_table" in text  # the human message, unchanged


def test_failing_scan_table_is_error_true(stub_engine):
    text, is_error = server._dispatch_tool("scan_table", {"table": "no_such_table"})
    assert is_error is True
    assert text.startswith("Error scanning table:")


def test_failing_describe_table_is_error_true(stub_engine):
    text, is_error = server._dispatch_tool("describe_table", {"table": "no_such_table"})
    assert is_error is True
    assert text.startswith("Error describing table:")


def test_successful_calls_stay_is_error_false(stub_engine):
    for name, args in (
        ("list_tables", {}),
        ("run_sql", {"sql": "SELECT 1+1 AS two"}),
        ("search_tables", {"query": "work_order"}),
    ):
        text, is_error = server._dispatch_tool(name, args)
        assert is_error is False, (name, text)
        assert not isinstance(text, _ToolError)  # normalized to plain str


def test_marker_is_a_str_with_identical_text(stub_engine):
    """The marker changes the TYPE, never the text: direct callers (the web
    UI inspector, tests, tool->tool reuse) keep plain str semantics."""
    text = server.run_sql("SELECT * FROM no_such_table")
    assert isinstance(text, _ToolError)
    assert isinstance(text, str)
    assert text.startswith("Error running SQL:")


def test_wire_result_flags_the_failure(stub_engine):
    """Over the transport: tools/call for a failing tool carries
    isError=true with the same text the direct dispatch returns."""
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _build_http_app

    direct_text, _ = server._dispatch_tool("run_sql", {"sql": "SELECT * FROM no_such_table"})
    app = _build_http_app()
    with TestClient(app) as client:
        init = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        assert init.status_code == 200
        body = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "run_sql", "arguments": {"sql": "SELECT * FROM no_such_table"}},
            },
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        ).json()
    result = body["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == direct_text  # byte-identical text


# --------------------------------------------- the annotation lie (P1-8)


def test_run_sql_hint_true_by_default_false_with_write_tier(monkeypatch):
    """The hint follows the tier: readOnly=true until SQLHANDLER_WRITES_ENABLED
    admits a classified scratch write through run_sql — then false, so
    clients do not skip confirmation for a write."""
    monkeypatch.delenv("SQLHANDLER_WRITES_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_MCP_READONLY", raising=False)
    tools = {t.name: t for t in _listed_tools()}
    assert tools["run_sql"].annotations.read_only_hint is True
    monkeypatch.setenv("SQLHANDLER_WRITES_ENABLED", "1")
    tools = {t.name: t for t in _listed_tools()}
    assert tools["run_sql"].annotations.read_only_hint is False
    # Other tools' hints are untouched by the write tier.
    assert tools["list_tables"].annotations.read_only_hint is True
    assert tools["query_save"].annotations.read_only_hint is False


def test_run_sql_and_saved_hints_false_when_mcp_readonly_off(monkeypatch):
    """The OTHER P1-8 lie, closed: with SQLHANDLER_MCP_READONLY=0, run_sql
    accepts DDL/DML and a saved DDL template runs via query_saved — neither
    may advertise readOnly=true. Default posture: both true again."""
    monkeypatch.delenv("SQLHANDLER_WRITES_ENABLED", raising=False)
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    tools = {t.name: t for t in _listed_tools()}
    assert tools["run_sql"].annotations.read_only_hint is False
    assert tools["query_saved"].annotations.read_only_hint is False
    # Unrelated tools stay honest either way.
    assert tools["list_tables"].annotations.read_only_hint is True
    monkeypatch.delenv("SQLHANDLER_MCP_READONLY", raising=False)
    tools = {t.name: t for t in _listed_tools()}
    assert tools["run_sql"].annotations.read_only_hint is True
    assert tools["query_saved"].annotations.read_only_hint is True


def test_query_submit_hint_follows_mcp_readonly(monkeypatch):
    """The same lie class, third instance: the D2 opt-out applies at SUBMIT
    time (jobs.py: the MCP_READONLY=0 opt-out applies exactly as run_sql),
    so with it off a DDL job can be submitted and the hint must not claim
    read-only. Default posture: true again."""
    monkeypatch.delenv("SQLHANDLER_WRITES_ENABLED", raising=False)
    monkeypatch.delenv("SQLHANDLER_MCP_READONLY", raising=False)
    tools = {t.name: t for t in _listed_tools()}
    assert tools["query_submit"].annotations.read_only_hint is True
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    tools = {t.name: t for t in _listed_tools()}
    assert tools["query_submit"].annotations.read_only_hint is False
    # idempotent=False is untouched (a job submit is never idempotent)
    assert tools["query_submit"].annotations.idempotent_hint is False


def test_query_saved_hint_and_description_follow_mcp_readonly(monkeypatch):
    """query_saved runs its stored SQL with the run-time guard: the readOnly
    hint must track SQLHANDLER_MCP_READONLY, and the description must say so."""
    monkeypatch.delenv("SQLHANDLER_MCP_READONLY", raising=False)
    monkeypatch.delenv("SQLHANDLER_WRITES_ENABLED", raising=False)
    tools = {t.name: t for t in _listed_tools()}
    assert tools["query_saved"].annotations.read_only_hint is True
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "0")
    tools = {t.name: t for t in _listed_tools()}
    assert tools["query_saved"].annotations.read_only_hint is False
    saved = next(t for t in server._TOOLS if t.name == "query_saved")
    assert "SQLHANDLER_MCP_READONLY=0" in saved.description
    assert "NOT read-only" in saved.description


def test_run_sql_description_names_the_write_tier():
    tool = next(t for t in server._TOOLS if t.name == "run_sql")
    assert "SQLHANDLER_WRITES_ENABLED" in tool.description
    assert "SQLHANDLER_MCP_READONLY=0" in tool.description
    assert "NOT read-only" in tool.description
    assert "SELECT-only" in tool.description  # default posture sentence kept


def test_more_tools_flag_is_error():
    """isError coverage beyond the three tools the wave pinned (P1-7 was
    verified for 3/15 sites; these are the other common failure shapes)."""
    from sqlhandler import server

    for name, args in (
        ("profile_table", {"table": "no_such_table"}),
        ("sample_rows", {"table": "no_such_table"}),
        ("column_stats", {"table": "no_such_table", "column": "x"}),
        ("explain_query", {"sql": "SELECT * FROM no_such_table"}),
    ):
        _text, is_error = server._dispatch_tool(name, args)
        assert is_error is True, f"{name} must flag isError on failure"
