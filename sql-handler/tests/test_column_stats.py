"""Tests for the per-column column_stats tool (Wave 5 additive feature).

Covers the result shape and values on known fixtures, the bounded sampling
(SQLHANDLER_PROFILE_MAX_ROWS cap — never a full-table scan beyond the
existing profile cap), column validation, virtual tables, caching, and the
MCP markdown rendering.
"""

import json

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import server
from sqlhandler.engine import (
    LakehouseError,
    SqlEngine,
    _column_stats_queries,
    _profile_max_rows,
    _validate_column,
)
from sqlhandler.provider import TableInfo


def _make_engine(tmp_path, catalog=None, monkeypatch=None):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": [1, 2, 3, 4, 5],
                "kind": ["a", "b", "a", "b", "a"],
                "amount": [10.0, None, 30.5, 40.0, 50.0],
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

    if catalog is not None:
        cat = tmp_path / "catalog.json"
        cat.write_text(json.dumps(catalog), encoding="utf-8")
        monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    return SqlEngine(P(), cache_ttl=0)


@pytest.fixture
def eng(tmp_path):
    return _make_engine(tmp_path)


# ---------------------------------------------------------------- shape


def test_numeric_column_shape_and_values(eng):
    s = eng.column_stats("work_order", "amount")
    assert s["table"] == "work_order"
    assert s["column"] == "amount"
    assert s["type"] == "double"
    assert s["n_rows"] == 5  # metadata count: no data scan for the row count
    assert s["sampled_rows"] == 5
    assert s["sample_cap"] == _profile_max_rows()
    assert s["distinct_count"] == 4
    assert s["null_count"] == 1
    assert s["null_pct"] == 20.0
    assert s["min"] == 10.0
    assert s["max"] == 50.0
    # quantiles over [10, 30.5, 40, 50]
    assert s["q25"] == 25.375
    assert s["q50"] == 35.25
    assert s["q75"] == 42.5
    assert [t["value"] for t in s["top_values"]] == [10.0, 30.5, 40.0, 50.0]
    assert all(t["count"] == 1 for t in s["top_values"])


def test_string_column_top_values_and_null_quantiles(eng):
    s = eng.column_stats("work_order", "kind", top_n=1)
    assert s["type"] == "string"
    assert s["distinct_count"] == 2
    assert s["null_count"] == 0
    assert s["min"] == "a" and s["max"] == "b"
    assert s["q25"] is None and s["q50"] is None and s["q75"] is None  # strings: no quantiles
    assert s["top_values"] == [{"value": "a", "count": 3}]  # top_n=1, count desc


def test_top_values_order_count_desc_then_value(tmp_path):
    eng = _make_engine(tmp_path)
    s = eng.column_stats("work_order", "kind")
    assert s["top_values"] == [{"value": "a", "count": 3}, {"value": "b", "count": 2}]


# ---------------------------------------------------------------- bounds


def test_sample_cap_bounds_the_scan(tmp_path, monkeypatch):
    """The profile cap bounds the scan: a 5-row table sampled with cap 3
    reports 3 sampled rows (an unbounded scan would report 5)."""
    monkeypatch.setenv("SQLHANDLER_PROFILE_MAX_ROWS", "3")
    eng = _make_engine(tmp_path)
    s = eng.column_stats("work_order", "id")
    assert s["sample_cap"] == 3
    assert s["sampled_rows"] == 3
    assert s["n_rows"] == 5  # the metadata count still reports the full table
    # min over the first 3 rows only
    assert s["min"] == 1 and s["max"] == 3


def test_sample_cap_zero_means_full_column(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_PROFILE_MAX_ROWS", "0")
    eng = _make_engine(tmp_path)
    s = eng.column_stats("work_order", "id")
    assert s["sample_cap"] == 0
    assert s["sampled_rows"] == 5
    assert s["min"] == 1 and s["max"] == 5


def test_top_n_clamped(eng):
    assert eng.column_stats("work_order", "kind", top_n=0)["top_values"] == [
        {"value": "a", "count": 3}
    ]  # clamp to 1
    assert (
        len(eng.column_stats("work_order", "kind", top_n=99)["top_values"]) == 2
    )  # clamp to 20, table has 2


# ------------------------------------------------------------ validation


def test_missing_column_error_names_available(eng):
    with pytest.raises(LakehouseError, match="does not exist on table") as exc:
        eng.column_stats("work_order", "nope")
    assert "id" in str(exc.value) and "amount" in str(exc.value)


def test_column_resolution_is_case_insensitive(eng):
    s = eng.column_stats("work_order", "AMOUNT")
    assert s["column"] == "amount"  # the table's actual casing


def test_empty_column_name_refused(eng):
    with pytest.raises(LakehouseError, match="Provide the column"):
        eng.column_stats("work_order", " ")


def test_unknown_table_error(eng):
    with pytest.raises(LakehouseError):
        eng.column_stats("no_such_table", "id")


def test_validate_column_helper():
    info = {"columns": [{"name": "Id", "type": "int64"}, {"name": "amount", "type": "double"}]}
    assert _validate_column(info, "id", "t") == ("Id", "int64")
    assert _validate_column(info, "AMOUNT", "t") == ("amount", "double")
    with pytest.raises(LakehouseError, match="Available columns"):
        _validate_column(info, "missing", "t")


# -------------------------------------------------------- virtual + cache


def test_virtual_table_column_stats(tmp_path, monkeypatch):
    catalog = {
        "tables": {
            "kind_a_view": {
                "definition": "SELECT id, kind FROM work_order WHERE kind = 'a'",
                "description": "kind-A rows only",
            }
        }
    }
    eng = _make_engine(tmp_path, catalog=catalog, monkeypatch=monkeypatch)
    s = eng.column_stats("kind_a_view", "id")
    assert s["virtual"] is True
    assert s["n_rows"] is None  # the definition's full count is not paid here
    assert s["sampled_rows"] == 3
    assert s["min"] == 1 and s["max"] == 5
    with pytest.raises(LakehouseError, match="does not exist"):
        eng.column_stats("kind_a_view", "amount")  # not in the definition's schema


def test_results_are_cached_like_profile(tmp_path):
    eng = _make_engine(tmp_path)
    eng.cache_ttl = 3600  # enable the profile cache (the fixture disables it)
    first = eng.column_stats("work_order", "amount")
    second = eng.column_stats("work_order", "amount")
    assert second["sampled_rows"] == first["sampled_rows"]
    assert eng._profile_hits == 1 and eng._profile_misses == 1


# ------------------------------------------------- the pure query helper


def test_column_stats_queries_against_raw_duckdb():
    """The bounded-sample SQL core, unit-tested against a raw connection."""
    con = duckdb.connect()
    con.register("t", pa.table({"x": [1, 2, 3, 4, 5, None], "s": ["a", "a", "b", None, "c", "a"]}))
    stats = _column_stats_queries(con, "t", "x", cap=4, top_n=2, col_type="int64")
    assert stats["sampled_rows"] == 4
    assert stats["distinct_count"] == 4
    assert stats["null_count"] == 0
    assert stats["min"] == 1 and stats["max"] == 4
    assert stats["q50"] == 2.5
    assert stats["top_values"] == [{"value": 1, "count": 1}, {"value": 2, "count": 1}]
    strs = _column_stats_queries(con, "t", "s", cap=0, top_n=5, col_type="string")
    assert strs["sampled_rows"] == 6
    assert strs["null_count"] == 1
    assert strs["q25"] is None
    assert strs["top_values"][0] == {"value": "a", "count": 3}


# ------------------------------------------------------------- MCP surface


def test_mcp_column_stats_markdown(eng, monkeypatch):
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text, is_error = server._dispatch_tool(
        "column_stats", {"table": "work_order", "column": "amount"}
    )
    assert is_error is False
    assert "Column stats: work_order.amount (double)" in text
    assert "distinct_count" in text and "null_count" in text
    assert "Top values" in text

    # same error convention as profile_table: the message carries the reason
    text, _is_error = server._dispatch_tool(
        "column_stats", {"table": "work_order", "column": "nope"}
    )
    assert "does not exist" in text and "Available columns" in text

    # top_n passes through the tool boundary (check the Top values section only)
    text, is_error = server._dispatch_tool(
        "column_stats", {"table": "work_order", "column": "kind", "top_n": 1}
    )
    assert is_error is False
    top_section = text.split("Top values")[1]
    assert "| a | 3 |" in top_section and "| b |" not in top_section


def test_markdown_cell_escaping(eng, monkeypatch):
    """A value containing markdown-breaking characters cannot corrupt the table."""
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text = server._column_stats_markdown(
        {
            "table": "t",
            "column": "c",
            "type": "string",
            "n_rows": 2,
            "sampled_rows": 2,
            "sample_cap": 0,
            "distinct_count": 1,
            "approx_unique": 1,
            "null_count": 0,
            "null_pct": 0.0,
            "min": "a|b",
            "max": "x",
            "q25": None,
            "q50": None,
            "q75": None,
            "top_values": [{"value": "a|b", "count": 2}],
        }
    )
    assert "a\\|b" in text  # escaped, not a broken row
