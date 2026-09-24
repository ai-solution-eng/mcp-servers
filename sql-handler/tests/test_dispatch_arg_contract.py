"""Regression tests for the dispatch argument contract (the gateway incident).

Production incident (2026-09, sqlhandler v2.3.0 behind pcai-llm-gateway): a
client-side tool registry sent ``run_sql {"query": ...}`` — the server's
advertised schema (and its every version, back to 0.6.0) requires ``sql``.
Dispatch read ``args.get("sql", "")``, coerced the mis-keyed call to an
empty string, and sqlguard's "Empty SQL statement." surfaced through
run_sql's catch-all — a MASKED error that reads like a server bug. The
tests that existed passed because they all used the correct key; the gap
was the mis-keyed/mis-shape arriving over the REAL transport.

The fix (this file pins it):
  * _dispatch_tool validates REQUIRED args against the tools/list schema
    (the single source of truth) BEFORE the dispatch body and refuses a
    missing/mis-keyed call with the structured E_PARAM_INVALID tail —
    naming the expected keys and offering a did-you-mean for the keys
    that WERE sent (query → "did you mean 'sql'?").
  * the guard's own "Empty SQL statement." (an explicitly-sent empty
    string) is enriched with the same code so both failure shapes
    self-correct.

Transport tests here drive the REAL app stack (_build_http_app +
TestClient, the fleet convention) — the exact streamable-HTTP shape the
gateway relay uses: initialize → tools/call with an arguments object.
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import errors as _errors
from sqlhandler import server

# ------------------------------------------------------------- engine stub


@pytest.fixture()
def stub_engine(tmp_path, monkeypatch):
    """A file-backed engine over one tiny parquet table (real query path)."""
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2], "kind": ["a", "b"], "amount": [10.0, 20.0]}),
        d / "part.parquet",
    )

    from sqlhandler.engine import SqlEngine
    from sqlhandler.provider import TableInfo

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    monkeypatch.setattr(server, "_handler", lambda: SqlEngine(P(), cache_ttl=0))


# --------------------------------------------------- the transport incident


@pytest.fixture()
def call_tool(stub_engine, monkeypatch):
    monkeypatch.delenv("SQLHANDLER_STRUCTURED_ERRORS", raising=False)
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _build_http_app

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
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert init.status_code == 200

        def _call(name, args, rid=1):
            body = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            ).json()
            result = body.get("result", {})
            return result.get("isError", False), result["content"][0]["text"]

        yield _call


def _tail(text: str) -> dict:
    return json.loads(text.rstrip().rsplit("\n", 1)[-1])["error"]


def test_run_sql_miskeyed_query_refused_with_param_invalid(call_tool):
    """THE incident: the gateway's client manifest sends run_sql {"query": ...}.

    Must arrive as a self-correcting E_PARAM_INVALID naming the 'sql' key —
    never again as the masked "Empty SQL statement."
    """
    err, text = call_tool("run_sql", {"query": "SELECT 1+1 AS two"})
    assert err is True
    assert "Empty SQL statement." not in text
    assert "Missing required argument(s) ['sql']" in text
    assert "'sql'" in text  # the expected key is named
    tail = _tail(text)
    assert tail["code"] == _errors.E_PARAM_INVALID
    hints = " ".join(tail["fix_hints"])
    assert '"sql"' in hints
    assert "did you mean" in hints.lower()  # the sent key is acknowledged


def test_query_submit_miskeyed_query_refused_with_param_invalid(call_tool):
    """The second half of the incident: query_submit {"query": ...}."""
    err, text = call_tool("query_submit", {"query": "SELECT 1"})
    assert err is True
    assert "Empty SQL statement." not in text
    tail = _tail(text)
    assert tail["code"] == _errors.E_PARAM_INVALID
    assert '"sql"' in " ".join(tail["fix_hints"])


def test_run_sql_contract_key_still_works_over_transport(call_tool):
    """Control: the schema's own key answers normally (no gate mis-fire)."""
    err, text = call_tool("run_sql", {"sql": "SELECT 1+1 AS two"})
    assert err is False
    assert "2" in text
    assert "E_PARAM_INVALID" not in text


def test_search_tables_owns_the_query_key_over_transport(call_tool):
    """search_tables' advertised key IS 'query' — the gate must not fire there."""
    err, text = call_tool("search_tables", {"query": "work_order"})
    assert err is False
    assert "work_order" in text
    assert "E_PARAM_INVALID" not in text


def test_run_sql_missing_arguments_entirely(call_tool):
    """No arguments at all (arguments: null over MCP arrives as {}) → same gate."""
    err, text = call_tool("run_sql", {})
    assert err is True
    assert "Missing required argument(s) ['sql']" in text
    assert "sent: none" in text
    assert _tail(text)["code"] == _errors.E_PARAM_INVALID


def test_run_sql_explicitly_empty_sql_enriched_not_masked(call_tool):
    """An explicitly-sent empty string keeps the tool's own message but now
    carries the E_PARAM_INVALID tail (previously: bare "Empty SQL statement.").
    """
    _err, text = call_tool("run_sql", {"sql": ""})
    assert "Empty SQL statement." in text  # human message stays primary
    tail = _tail(text)
    assert tail["code"] == _errors.E_PARAM_INVALID
    assert '"sql"' in " ".join(tail["fix_hints"])


def test_tools_list_schema_is_the_enforced_contract(call_tool):
    """tools/list and the dispatch gate must agree — same source of truth."""
    err, _ = call_tool("run_sql", {"sql": "SELECT 1"})
    assert err is False
    # The gate derives from _TOOLS; pin the expected required-map so any
    # future schema change without a dispatch-contract check is caught.
    expected = {
        "describe_table": ("table",),
        "profile_table": ("table",),
        "search_tables": ("query",),
        "run_sql": ("sql",),
        "scan_table": ("table",),
        "column_stats": ("table", "column"),
        "sample_rows": ("table",),
        "query_submit": ("sql",),
        "query_status": ("job_id",),
        "query_result": ("job_id",),
        "query_cancel": ("job_id",),
        "query_save": ("name", "sql"),
        "query_delete": ("name",),
        "query_saved": ("name",),
        "explain_query": ("sql",),
        "ask_data": ("question",),
        "list_tables": (),
        "query_list": (),
    }
    advertised = {t.name: tuple(t.input_schema.get("required") or ()) for t in server._TOOLS}
    assert advertised == expected


# ------------------------------------------------------ dispatch unit tests


def test_param_invalid_near_miss_names_the_sent_keys(stub_engine):
    text, is_error = server._dispatch_tool("run_sql", {"query": "SELECT 1", "limit": 5})
    assert is_error is True
    assert "sent: limit, query" in text
    assert "did you mean" in text.lower()


def test_param_invalid_unknown_tool_never_fires(stub_engine):
    text, is_error = server._dispatch_tool("nope", {})
    assert is_error is True
    assert "Unknown tool" in text  # the pre-existing message, not the gate


def test_param_invalid_optional_only_args_pass(stub_engine):
    """Optional-arg tools accept partial shapes: scan_table without limit."""
    text, is_error = server._dispatch_tool("scan_table", {"table": "work_order"})
    assert is_error is False
    assert "Missing required argument" not in text


def test_param_invalid_all_validated_tools_accept_their_required_keys(stub_engine):
    """Anti-mis-fire sweep: for EVERY tool, sending exactly its required keys
    must get PAST the gate (the error text, if any, is downstream — never the
    gate's). This is the regression guard for "validation must not mis-fire
    on valid calls" — the concern that made the wave-3 patterns suspect.
    """
    probe = {
        "table": "work_order",
        "column": "amount",
        "query": "x",
        "sql": "SELECT 1",
        "job_id": "j0",
        "name": "n",
        "question": "q",
    }
    for tool in server._TOOLS:
        required = tool.input_schema.get("required") or []
        args = {k: probe[k] for k in required}
        text, _is_error = server._dispatch_tool(tool.name, args)
        assert "Missing required argument" not in text, tool.name


# ------------------------------------------------------------ errors unit


def test_errors_classify_empty_sql_statement():
    code, hints = _errors.classify("Empty SQL statement.")
    assert code == _errors.E_PARAM_INVALID
    assert any('"sql"' in h for h in hints)


def test_errors_classify_leaves_other_guard_messages_alone():
    assert _errors.classify("Read-only mode: this statement type is not allowed") is not None
    # the new pattern must not swallow the read-only classification (first match wins)
    code, _hints = _errors.classify("Read-only mode: this statement type is not allowed")
    assert code == _errors.E_READONLY


def test_structured_errors_off_restores_bare_texts(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_STRUCTURED_ERRORS", "0")
    text, is_error = server._dispatch_tool("run_sql", {"query": "SELECT 1"})
    assert is_error is True
    assert "Missing required argument(s) ['sql']" in text
    assert '"error"' not in text  # no tail when structured errors are off
