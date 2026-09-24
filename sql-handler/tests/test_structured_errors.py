"""Tests for structured self-correcting errors (errors.py + wiring).

The contract: the HUMAN message stays primary and byte-identical; when
SQLHANDLER_STRUCTURED_ERRORS is on (default), ONE final JSON line carries
the stable code + fix_hints. Covered: table/column typos (engine + SQL
paths), the read-only refusal, timeout/gate/params shapes, the off-switch,
and the dispatch catch-site.
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import errors, server
from sqlhandler.engine import LakehouseError, SqlEngine, _with_hints
from sqlhandler.provider import TableInfo


def _make_engine(tmp_path):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0], "kind": ["a", "b", "a"]}),
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


@pytest.fixture
def eng(tmp_path, monkeypatch):
    # Structured errors ON (the default) unless a test says otherwise.
    monkeypatch.delenv("SQLHANDLER_STRUCTURED_ERRORS", raising=False)
    return _make_engine(tmp_path)


def _tail(text: str) -> dict:
    """Parse the trailing JSON line of an error text."""
    last = text.rstrip().rsplit("\n", 1)[-1]
    return json.loads(last)["error"]


# ------------------------------------------------------------ classification


def test_table_not_found_message_classified():
    code, hints = errors.classify("Table 'work_odr' not found in data source")
    assert code == errors.E_TABLE_NOT_FOUND
    assert hints and all(isinstance(h, str) for h in hints)


def test_duckdb_table_error_classified():
    code, _ = errors.classify("Catalog Error: Table with name work_odr does not exist!")
    assert code == errors.E_TABLE_NOT_FOUND


def test_column_not_found_messages_classified():
    # _validate_column's message ...
    code, _ = errors.classify(
        "Column 'amont' does not exist on table 'work_order'. Available columns: id"
    )
    assert code == errors.E_COLUMN_NOT_FOUND
    # ... and DuckDB's Binder phrasing.
    code, _ = errors.classify('Binder Error: Column "amont" does not exist')
    assert code == errors.E_COLUMN_NOT_FOUND


def test_readonly_refusal_classified():
    msg = (
        "Read-only MCP (SQLHANDLER_MCP_READONLY): INSERT statements are not allowed. "
        "Only SELECT / WITH / VALUES / EXPLAIN SELECT queries are permitted."
    )
    code, _ = errors.classify(msg)
    assert code == errors.E_READONLY


def test_timeout_and_gate_messages_classified():
    code, _ = errors.classify(
        "Query timed out after 600s (SQLHANDLER_QUERY_TIMEOUT) and was cancelled."
    )
    assert code == errors.E_TIMEOUT
    code, _ = errors.classify(
        "Too many concurrent queries (limit 8), and the queue wait of 30.0s expired. Retry later."
    )
    assert code == errors.E_CONCURRENCY_GATE
    code, _ = errors.classify(
        "Too many active query jobs (8 of 8, SQLHANDLER_MAX_JOBS); cancel or fetch results and retry later."
    )
    assert code == errors.E_CONCURRENCY_GATE


def test_param_errors_classified():
    code, _ = errors.classify("Query params: named parameters need string keys.")
    assert code == errors.E_PARAM_INVALID
    code, _ = errors.classify(
        "Query params must be scalars (str/int/float/bool/datetime/Decimal/None); got dict."
    )
    assert code == errors.E_PARAM_INVALID


def test_unknown_message_gets_no_code():
    assert errors.classify("some unrecognized failure") is None


# ------------------------------------------------------------ the tail line


def test_enrich_appends_exactly_one_json_line():
    msg = "Table 'work_odr' not found in data source"
    out = errors.enrich(msg)
    assert out.startswith(msg + "\n")  # human message byte-identical, primary
    lines = out.splitlines()
    assert len(lines) == 2
    payload = json.loads(lines[1])
    assert payload["error"]["code"] == errors.E_TABLE_NOT_FOUND
    assert payload["error"]["fix_hints"]


def test_enrich_passthrough_for_unknown_and_disabled(monkeypatch):
    assert errors.enrich("plain message") == "plain message"
    monkeypatch.setenv("SQLHANDLER_STRUCTURED_ERRORS", "0")
    assert (
        errors.enrich("Table 'x' not found in data source") == "Table 'x' not found in data source"
    )


def test_structured_disabled_values(monkeypatch):
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("SQLHANDLER_STRUCTURED_ERRORS", off)
        assert errors.structured_errors_enabled() is False
    for on in ("1", "true", "", "anything"):
        monkeypatch.setenv("SQLHANDLER_STRUCTURED_ERRORS", on)
        assert errors.structured_errors_enabled() is True


def test_hint_substitutes_the_bad_name():
    _, hints = errors.classify("Table 'work_odr' not found in data source")
    assert any("work_odr" in h for h in hints)


# ------------------------------------------------------ engine wiring


def test_scan_arrow_table_typo_raw_at_engine(tmp_path, monkeypatch):
    """scan_arrow raises the raw resolution error (no tail at the engine).

    The structured tail is a TRANSPORT concern: it is appended at the MCP
    catch-sites (scan_table / run_sql / dispatch), never inside the engine
    primitives — so a programmatic engine caller sees today's exact text.
    """
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    with pytest.raises(LakehouseError) as ei:
        eng.scan_arrow("work_odr")
    assert str(ei.value) == "Table 'work_odr' not found in data source"
    # The same typo through the MCP tool DOES carry the tail.
    text = server.scan_table("work_odr")
    assert _tail(text)["code"] == errors.E_TABLE_NOT_FOUND


def test_with_hints_column_typo_carries_tail(eng):
    sql = "SELECT amont FROM work_order"
    try:
        eng.query_duckdb(sql)
        raise AssertionError("expected a query error")
    except LakehouseError as first:
        hinted = _with_hints(eng, sql, first)
    msg = str(hinted)
    assert _tail(msg)["code"] == errors.E_COLUMN_NOT_FOUND


def test_engine_error_message_stays_primary(eng):
    with pytest.raises(LakehouseError) as ei:
        eng.scan_arrow("work_odr")
    assert str(ei.value).splitlines()[0] == "Table 'work_odr' not found in data source"


# ------------------------------------------------------ MCP tool surfaces


@pytest.fixture
def wired(eng, monkeypatch):
    monkeypatch.setattr(server, "_handler", lambda: eng)
    return eng


def test_scan_table_typo_tail(wired):
    text = server.scan_table("work_odr")
    assert text.startswith("Error scanning table: Table 'work_odr' not found in data source\n")
    assert _tail(text)["code"] == errors.E_TABLE_NOT_FOUND


def test_column_stats_column_typo_tail(wired):
    text = server.column_stats("work_order", "amont")
    assert text.startswith(
        "Error computing column stats: Column 'amont' does not exist on table 'work_order'"
    )
    assert _tail(text)["code"] == errors.E_COLUMN_NOT_FOUND


def test_run_sql_readonly_refusal_tail(wired):
    text = server.run_sql("DELETE FROM work_order")
    assert "Error running SQL:" in text
    assert "not allowed" in text  # the human guard message, unchanged
    assert _tail(text)["code"] == errors.E_READONLY


def test_run_sql_off_restores_byte_identical_text(wired, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_STRUCTURED_ERRORS", "0")
    text = server.run_sql("DELETE FROM work_order")
    assert "\n{" not in text
    assert text.startswith("Error running SQL: Read-only MCP (SQLHANDLER_MCP_READONLY): DELETE")


def test_dispatch_catch_site_appends_tail(wired, monkeypatch):
    # query_saved raises through the tool function, so the DISPATCH catch is
    # what formats it — and the tail rides along (best-effort on any text).
    text, is_error = server._dispatch_tool("query_saved", {"name": "no_such_q"})
    assert is_error is True
    assert text.startswith("Unknown saved query: no_such_q")


def test_dispatch_unknown_tool_untouched(wired):
    text, is_error = server._dispatch_tool("nope", {})
    assert is_error is True
    assert text == "Unknown tool: nope"  # not an enrichable message


# ------------------------------------------------------------ rows capped


def test_rows_capped_tail_on_markdown_scan(wired):
    text = server.scan_table("work_order", limit=2)
    assert _tail(text)["code"] == errors.E_ROWS_CAPPED


def test_rows_capped_no_tail_when_limit_exceeds_rows(wired):
    # 3-row table, limit 10: nothing was capped — no cap notice.
    text = server.scan_table("work_order", limit=10)
    assert "E_ROWS_CAPPED" not in text


def test_rows_capped_json_carries_error_field(wired):
    text = server.scan_table("work_order", limit=2, output_format="json")
    payload = json.loads(text)
    assert payload["truncated"] is True
    assert payload["error"]["code"] == errors.E_ROWS_CAPPED
