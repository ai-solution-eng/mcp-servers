"""End-to-end MCP-surface tests for the agent productivity pack (Wave 7).

Drives the REAL app stack (_build_http_app + TestClient, the fleet test
convention) over a stubbed engine: structured error codes arrive through
tools/call (table typo, read-only refusal) and the new sample_rows tool
answers through the full transport.
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import server
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import TableInfo


def _make_engine(tmp_path):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": [1, 2, 3, 4, 5],
                "kind": ["a", None, "a", "b", "a"],
                "amount": [10.0, 20.0, None, 40.0, 50.0],
            }
        ),
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SQLHANDLER_STRUCTURED_ERRORS", raising=False)
    for var in ("MCP_API_KEYS", "SQLHANDLER_API_KEYS", "SQLHANDLER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    TestClient = pytest.importorskip("starlette.testclient", reason="httpx").TestClient
    from sqlhandler.server import _build_http_app

    app = _build_http_app()
    with TestClient(app) as c:
        yield c


def _rpc(client, method, params, rid=1):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )


@pytest.fixture()
def call_tool(client):
    client.initialize = None
    init = _rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
        rid=0,
    )
    assert init.status_code == 200

    def _call(name, args, rid=1):
        body = _rpc(client, "tools/call", {"name": name, "arguments": args}, rid=rid).json()
        result = body.get("result", {})
        return result.get("isError", False), result["content"][0]["text"]

    return _call


def _tail(text: str) -> dict:
    return json.loads(text.rstrip().rsplit("\n", 1)[-1])["error"]


# ------------------------------------------------------------ sample_rows


def test_sample_rows_over_mcp(call_tool):
    err, text = call_tool("sample_rows", {"table": "work_order", "limit": 2})
    assert err is False
    assert "Sample rows: work_order (sampled 2 of 5 rows)" in text
    assert "Fill rates (this sample):" in text
    assert "80.0%" not in text  # the 2-row sample's kind is 50% filled, not the 5-row rate
    assert "50.0%" in text


def test_sample_rows_advertised_in_tools_list(client):
    body = _rpc(client, "tools/list", {}, rid=2).json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "sample_rows" in names


# ------------------------------------------------------- structured errors


def test_table_typo_code_over_mcp(call_tool):
    err, text = call_tool("scan_table", {"table": "work_odr"})
    assert err is False  # existing convention: tool functions return error TEXT
    assert text.startswith("Error scanning table: Table 'work_odr' not found in data source")
    assert _tail(text)["code"] == "E_TABLE_NOT_FOUND"


def test_readonly_refusal_code_over_mcp(call_tool):
    _err, text = call_tool("run_sql", {"sql": "DROP TABLE work_order"})
    assert "not allowed" in text  # human guard message intact
    assert _tail(text)["code"] == "E_READONLY"


def test_structured_errors_off_restores_plain_text(client, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_STRUCTURED_ERRORS", "0")
    body = _rpc(
        client,
        "tools/call",
        {"name": "run_sql", "arguments": {"sql": "DROP TABLE work_order"}},
        rid=3,
    ).json()
    text = body["result"]["content"][0]["text"]
    assert "\n{" not in text
    assert text.startswith("Error running SQL: Read-only MCP (SQLHANDLER_MCP_READONLY): DROP")


def test_structured_errors_default_on(call_tool):
    _err, text = call_tool("column_stats", {"table": "work_order", "column": "amnt"})
    assert _tail(text)["code"] == "E_COLUMN_NOT_FOUND"
