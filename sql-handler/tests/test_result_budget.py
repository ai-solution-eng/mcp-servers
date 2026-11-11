"""Result-size budget + pagination (no tool result may blow a model context).

Three bounds are pinned here:

* SQLHANDLER_MAX_OUTPUT_ROWS — the hard ROW ceiling (unchanged behavior, now
  pageable: a capped page names the next offset instead of dead-ending).
* SQLHANDLER_MAX_RESULT_BYTES — the BYTE ceiling, with a DETERMINISTIC
  truncation marker that states the total, the returned count and how to
  fetch more (rows are never dropped silently).
* `offset` — the continuation cursor on run_sql / scan_table / query_saved.

Run: python -m pytest tests/test_result_budget.py -v
"""

import json

import pyarrow as pa
import pytest

from sqlhandler import limits, server

ROWS = 60


class PagingEngine:
    """Engine stub that honors the limit the way the real one does (a prefix).

    run_sql/scan_table ask for offset+page rows and slice the page out, so the
    stub must return a PREFIX of the table — exactly what DuckDB/scan.head do.
    """

    def __init__(self, n=ROWS):
        self.table = pa.table({"id": list(range(n))})

    def query_duckdb(self, sql, limit=None, params=None, version_as_of=None, **kw):
        return self._prefix(limit)

    def scan_arrow(self, table, columns=None, limit=None, version_as_of=None, **kw):
        return self._prefix(limit)

    def _prefix(self, limit):
        if limit is None or limit < 0:
            return self.table
        return self.table.slice(0, min(limit, self.table.num_rows))


@pytest.fixture
def engine(monkeypatch):
    eng = PagingEngine()
    monkeypatch.setattr(server, "_handler", lambda: eng)
    monkeypatch.delenv("SQLHANDLER_MAX_OUTPUT_ROWS", raising=False)
    monkeypatch.delenv("SQLHANDLER_MAX_RESULT_BYTES", raising=False)
    monkeypatch.delenv("SQLHANDLER_MAX_ROWS", raising=False)
    return eng


@pytest.fixture
def big(engine):
    """A result far larger than a small byte budget (forces truncation)."""
    engine.table = pa.table({"id": list(range(500))})
    return engine


def _tail(text):
    """The whole trailing JSON line of a truncated result."""
    return json.loads(text.rstrip().rsplit("\n", 1)[-1])


def _marker(text):
    return _tail(text)["truncated"]


def _ids(text):
    """The integer ids rendered as markdown body rows (header skipped)."""
    out = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == 1 and cells[0].isdigit():
            out.append(int(cells[0]))
    return out


# ------------------------------------------------------------------- budget


def test_byte_budget_truncates_with_a_deterministic_marker(big, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "2000")
    text = server.scan_table("t", limit=600)
    assert len(text.encode("utf-8")) <= 2000
    marker = _marker(text)
    assert marker["reason"] == "byte_budget"
    assert marker["unit"] == "rows"
    assert marker["total"] == 500  # every row this call fetched
    assert 0 < marker["returned"] < 500
    assert marker["offset"] == 0
    assert marker["next_offset"] == marker["returned"]
    assert marker["resume"] == {"tool": "scan_table", "arguments": {"offset": marker["returned"]}}
    assert marker["byte_limit"] == 2000
    assert marker["fix_hints"]
    # deterministic: the same call returns byte-identical text
    assert server.scan_table("t", limit=600) == text


def test_byte_budget_marker_states_total_returned_and_how_to_fetch_more(big, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "2000")
    text = server.run_sql("SELECT * FROM t")
    human, machine = text.rstrip().rsplit("\n", 2)[-2:]
    assert human.startswith("Result truncated (byte_budget): showing ")
    assert "continue with offset=" in human
    payload = json.loads(machine)["truncated"]
    assert {"total", "returned", "offset", "next_offset", "resume", "fix_hints"} <= set(payload)


def test_rows_are_never_silently_dropped(engine, monkeypatch):
    """Every row is reachable by paging: the pages partition the result."""
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "2000")
    seen = []
    offset = 0
    for _page in range(100):  # bounded so a regression fails instead of hanging
        text = server.scan_table("t", limit=ROWS, offset=offset)
        seen.extend(_ids(text))
        if '"truncated"' not in text:
            break
        marker = _marker(text)
        if marker["next_offset"] is None:
            break
        offset = marker["next_offset"]
    assert seen == list(range(ROWS))  # no gaps, no duplicates, no silent drops


def test_offset_pages_the_same_result(engine):
    first = server.scan_table("t", limit=5)
    assert _ids(first) == [0, 1, 2, 3, 4]
    assert _marker(first)["next_offset"] == 5
    second = server.scan_table("t", limit=5, offset=5)
    assert _ids(second) == [5, 6, 7, 8, 9]
    assert _marker(second)["offset"] == 5
    assert _marker(second)["next_offset"] == 10


def test_offset_beyond_the_result_is_an_empty_announced_page(engine):
    text = server.scan_table("t", limit=5, offset=ROWS + 10)
    assert _ids(text) == []
    marker = _marker(text)
    assert marker["returned"] == 0
    assert marker["next_offset"] is None
    assert "no rows at offset" in text


def test_negative_offset_is_a_clear_error(engine):
    text = server.scan_table("t", limit=5, offset=-1)
    assert text.startswith("Error scanning table: offset must be a non-negative integer")
    text = server.run_sql("SELECT 1", offset="banana")
    assert text.startswith("Error running SQL: offset must be a non-negative integer")


def test_a_short_page_carries_no_marker(engine):
    # limit larger than the result: nothing withheld, nothing announced.
    text = server.scan_table("t", limit=ROWS + 1)
    assert '"truncated"' not in text
    assert len(_ids(text)) == ROWS


def test_row_cap_still_applies_and_now_pages(engine, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_OUTPUT_ROWS", "10")
    text = server.scan_table("t", limit=ROWS)
    marker = _marker(text)
    assert marker["reason"] == "row_budget"
    assert marker["returned"] == 10
    assert marker["total"] == ROWS
    assert marker["next_offset"] == 10  # paging continues past the row cap
    assert _tail(text)["error"]["code"] == "E_ROWS_CAPPED"
    # page 2 is reachable and carries the next slice
    assert _ids(server.scan_table("t", limit=ROWS, offset=10)) == list(range(10, 20))


def test_budget_zero_disables_the_byte_ceiling(engine, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "0")
    text = server.scan_table("t", limit=ROWS + 1)
    assert '"truncated"' not in text


def test_configured_budget_below_the_marker_floor_is_raised(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "10")
    assert limits.max_result_bytes() == limits.MIN_RESULT_BYTES


def test_json_carries_the_page_inside_the_payload(engine, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "2000")
    payload = json.loads(server.scan_table("t", limit=ROWS, output_format="json"))
    assert payload["truncated"] is True
    assert payload["page"]["total"] == ROWS
    assert payload["page"]["returned"] == payload["n_rows"]
    assert payload["page"]["next_offset"] == payload["n_rows"]


def test_csv_and_arrow_carry_the_marker(big, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "2000")
    csv_text = server.scan_table("t", limit=600, output_format="csv")
    assert '"truncated"' in csv_text
    arrow_text = server.scan_table("t", limit=600, output_format="arrow")
    assert "# Result truncated" in arrow_text
    # the arrow marker is comment-prefixed, so the base64 body still decodes
    import base64
    import io

    body = "".join(line for line in arrow_text.splitlines() if not line.startswith("#"))
    with pa.ipc.open_stream(io.BytesIO(base64.b64decode(body))) as reader:
        assert reader.read_all().num_rows < 500


# ------------------------------------------------------ the generic guard


def test_non_tabular_results_are_bounded_too(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "2000")
    text = "\n".join(f"  - table_{i}" for i in range(500))
    capped = limits.cap_tool_text(text, "list_tables")
    assert len(capped.encode("utf-8")) <= 2000
    payload = json.loads(capped.rstrip().rsplit("\n", 1)[-1])["truncated"]
    assert payload["unit"] == "lines"
    assert payload["total"] == 500
    assert payload["returned"] < 500
    assert payload["next_offset"] is None  # not pageable: the hints say narrow
    assert any("does not paginate" in hint for hint in payload["fix_hints"])


def test_generic_guard_leaves_json_and_marked_text_alone(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "1024")
    payload = json.dumps({"columns": ["a"], "rows": [["x"] * 200]})
    assert limits.cap_tool_text(payload, "run_sql") == payload
    marked = 'x\n{"truncated":{"tool":"t","returned":0}}'
    assert limits.cap_tool_text(marked, "t") == marked


def test_call_tool_applies_the_generic_guard(monkeypatch):
    """The MCP boundary bounds EVERY tool result, not just the row tools."""
    import asyncio

    from mcp_types import CallToolRequestParams

    monkeypatch.setenv("SQLHANDLER_MAX_RESULT_BYTES", "2000")
    monkeypatch.setattr(server, "list_tables", lambda: "\n".join(f"  - table_{i}" for i in range(400)))
    result = asyncio.run(server._handle_call_tool(None, CallToolRequestParams(name="list_tables", arguments={})))
    text = result.content[0].text
    assert len(text.encode("utf-8")) <= 2000
    assert '"truncated"' in text
    assert result.is_error is False
