"""End-to-end MCP-surface tests for ask_data (agent productivity pack §2d).

Drives the REAL app stack (_build_http_app + TestClient, the fleet test
convention) over the small physical fixture: the tool answers through the
full transport, its output is markdown, the default (and any value of)
``execute`` NEVER executes anything, the token-budget caps hold, and the
drafted SQL is valid per the sqlguard parser.
"""

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler import server
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import TableInfo
from sqlhandler.sqlguard import extract_statement_spans


def _make_engine(tmp_path, n_cols=3):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    cols = {
        "id": [1, 2, 3, 4, 5],
        "kind": ["a", None, "a", "b", "a"],
        "amount": [10.0, 20.0, 30.0, 40.0, 50.0],
    }
    pq.write_table(pa.table({k: v[:5] for k, v in list(cols.items())[:n_cols]}), d / "part.parquet")

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            return pad.dataset(str(d), format="parquet")

    return SqlEngine(P(), cache_ttl=0)


@pytest.fixture()
def client(tmp_path, monkeypatch):
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


# ------------------------------------------------------------ end-to-end


def test_ask_data_end_to_end_markdown(call_tool):
    err, text = call_tool("ask_data", {"question": "work order amounts"})
    assert err is False
    assert text.startswith("Question: work order amounts")
    assert "Candidate tables" in text
    assert "workorder/work_order (parquet)" in text
    assert "Schema of workorder/work_order:" in text
    assert "Drafted SQL (NOT executed" in text
    assert "run this with run_sql" in text.lower() or "Run this with run_sql" in text
    assert "Suggested follow-up" in text
    # The no-execution footer, always present.
    assert "Nothing was executed" in text


def test_ask_data_default_never_executes(call_tool, monkeypatch, tmp_path):
    """execute:false default — and ANY execute value must not run the draft.

    The spy records EVERY SQL text reaching query_duckdb (the tool's only
    execution surface); the drafted query's text must never be among them.
    What legitimately does reach query_duckdb is the profile sampler's
    bounded sample (a `SELECT ... LIMIT n` shape over the registered view) —
    which is why the assertion is on the DRAFT TEXT, not on call counts.
    """
    eng = _make_engine(tmp_path)
    executed_sqls: list[str] = []
    real_query = eng.query_duckdb

    def spy_query(sql, *a, **k):
        executed_sqls.append(str(sql))
        return real_query(sql, *a, **k)

    monkeypatch.setattr(eng, "query_duckdb", spy_query)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    outputs = []
    for args in (
        {"question": "work order amounts"},
        {"question": "work order amounts", "execute": False},
        {"question": "work order amounts", "execute": True},
    ):
        err, text = call_tool("ask_data", args)
        assert err is False
        assert "NOT executed" in text
        outputs.append(text)
    for text in outputs:
        draft = text.split("```sql")[1].split("```")[0].strip()
        assert not draft.startswith("--"), "the fixture should produce a real draft"
        assert draft not in executed_sqls  # the draft itself never ran
    # Byte-identical output across execute values: the flag changes nothing.
    assert outputs[0] == outputs[1] == outputs[2]


def test_ask_data_execute_true_is_symmetric_noop(call_tool):
    _err_default, text_default = call_tool("ask_data", {"question": "work order amounts"})
    _err_true, text_true = call_tool(
        "ask_data", {"question": "work order amounts", "execute": True}
    )
    # Byte-identical output: execute changes nothing.
    assert text_default == text_true


def test_ask_data_draft_sql_is_valid_per_sqlguard(call_tool):
    _err, text = call_tool("ask_data", {"question": "work order amounts"})
    draft = text.split("```sql")[1].split("```")[0].strip()
    types = [t for t, _ in extract_statement_spans(draft)]
    assert types == ["SELECT"]
    assert "workorder_work_order" in draft  # the SQL-addressable (qualified) name
    assert "LIMIT" in draft


def test_ask_data_draft_actually_runs(call_tool, monkeypatch, tmp_path):
    """The drafted SQL, handed to run_sql separately, returns rows."""
    _err, text = call_tool("ask_data", {"question": "work order amounts"})
    draft = text.split("```sql")[1].split("```")[0].strip()
    err, result = call_tool("run_sql", {"sql": draft, "limit": 5})
    assert err is False
    assert "id" in result


def test_ask_data_token_budget_caps(call_tool, monkeypatch, tmp_path):
    """Candidate cap 5, described columns cap 20, profiled columns cap 6."""
    # 30-column table: describe lists everything, the draft uses <= 6, and the
    # profile call only ever asked for 6 columns.
    d = tmp_path / "wide" / "wide_table"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({f"c{i:02d}": [float(i)] * 4 for i in range(30)}),
        d / "part.parquet",
    )

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="wide_table", schema="wide", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            return pad.dataset(str(d), format="parquet")

    eng = SqlEngine(P(), cache_ttl=0)
    profiled: list[list[str]] = []
    real_profile = eng.profile_table

    def spy_profile(table, columns=None):
        if columns:
            profiled.append(list(columns))
        return real_profile(table, columns=columns)

    monkeypatch.setattr(eng, "profile_table", spy_profile)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    err, text = call_tool("ask_data", {"question": "wide table"})
    assert err is False
    assert profiled and len(profiled[0]) <= 6
    draft = text.split("```sql")[1].split("```")[0].strip()
    # The projection names at most 6 columns (plus LIMIT).
    body = draft.split("FROM")[0]
    assert body.count(",") <= 5
    # And the drafted SQL still parses.
    assert [t for t, _ in extract_statement_spans(draft)] == ["SELECT"]


def test_ask_data_search_cap_five(monkeypatch, tmp_path):
    """search_tables is asked for at most 5 candidates."""
    eng = _make_engine(tmp_path)
    asked = []
    real_search = eng.search_tables

    def spy_search(query, limit=20):
        asked.append(limit)
        return real_search(query, limit=limit)

    monkeypatch.setattr(eng, "search_tables", spy_search)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    server.ask_data("work order")
    assert asked == [5]


def test_ask_data_no_match_degrades_honestly(monkeypatch, tmp_path):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text = server.ask_data("quantum entanglement flux")
    assert "No tables matched" in text
    assert "NOTHING WAS EXECUTED" in text


def test_ask_data_empty_question_param_invalid(monkeypatch, tmp_path):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text, _is_error = server._dispatch_tool("ask_data", {"question": "   "})
    # Tool convention: error text returned (is_error False, like scan_table's typo path);
    # the structured tail carries the machine-parseable code.
    assert text.startswith("Error planning question:")
    assert "E_PARAM_INVALID" in text


def test_ask_data_non_bool_execute_refused(monkeypatch, tmp_path):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text, _is_error = server._dispatch_tool(
        "ask_data", {"question": "work order", "execute": "yes please"}
    )
    assert text.startswith("Error planning question:")
    assert "E_PARAM_INVALID" in text


def test_ask_data_advertised_in_tools_list(client):
    body = _rpc(client, "tools/list", {}, rid=2).json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "ask_data" in names
    assert "explain_query" in names
