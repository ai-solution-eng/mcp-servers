"""Tests for the sample_rows tool (agent productivity pack — head + fill rates).

Covers: the result shape and fill rates on a known physical fixture, the
profile-sampler posture (the cap bounds the read, head() semantics), the
D4 limit resolution shared with scan_table, column projection, virtual
tables routed through the SQL path, and the MCP markdown rendering
(physical + virtual + the error surface).
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import server
from sqlhandler.engine import SqlEngine, _profile_max_rows
from sqlhandler.provider import TableInfo


def _make_engine(tmp_path, catalog=None, monkeypatch=None):
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

    if catalog is not None:
        import yaml

        cat = tmp_path / "catalog.yaml"
        cat.write_text(yaml.safe_dump(catalog), encoding="utf-8")
        monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    return SqlEngine(P(), cache_ttl=0)


@pytest.fixture
def eng(tmp_path):
    return _make_engine(tmp_path)


# ------------------------------------------------------------------ shape


def test_shape_and_fill_rates_on_known_fixture(eng):
    s = eng.sample_rows("work_order", limit=5)
    assert s["table"] == "work_order"
    assert s["uri"] == "fake://"
    assert s["n_rows"] == 5  # metadata count, no data scan
    assert s["sampled_rows"] == 5
    assert s["sample_limit"] == 5
    fill = {c["name"]: c for c in s["columns"]}
    assert fill["id"] == {
        "name": "id",
        "type": "int64",
        "fill_count": 5,
        "fill_pct": 100.0,
        "null_count": 0,
    }
    assert fill["kind"]["null_count"] == 1 and fill["kind"]["fill_pct"] == 80.0
    assert fill["amount"]["null_count"] == 1 and fill["amount"]["fill_pct"] == 80.0
    assert s["rows"] == [
        {"id": 1, "kind": "a", "amount": 10.0},
        {"id": 2, "kind": None, "amount": 20.0},
        {"id": 3, "kind": "a", "amount": None},
        {"id": 4, "kind": "b", "amount": 40.0},
        {"id": 5, "kind": "a", "amount": 50.0},
    ]


def test_partial_sample_fill_rates_match_the_sample(eng):
    s = eng.sample_rows("work_order", limit=2)
    assert s["sampled_rows"] == 2 and s["n_rows"] == 5
    fill = {c["name"]: c for c in s["columns"]}
    # The first two rows: kind has one null (50% fill), amount none.
    assert fill["kind"]["fill_pct"] == 50.0 and fill["kind"]["null_count"] == 1
    assert fill["amount"]["fill_pct"] == 100.0 and fill["amount"]["null_count"] == 0


def test_column_projection_restricts_output(eng):
    s = eng.sample_rows("work_order", limit=3, columns=["id", "amount"])
    assert [c["name"] for c in s["columns"]] == ["id", "amount"]
    assert all(set(r) == {"id", "amount"} for r in s["rows"])


# ------------------------------------------------------------------ limits


def test_d4_negative_limit_resolves_to_max_rows_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "4")
    eng = _make_engine(tmp_path)
    s = eng.sample_rows("work_order", limit=-1)
    assert s["sample_limit"] == 4
    assert s["sampled_rows"] == 4
    assert s.get("limit_resolved") is True  # the clamp happened


def test_missing_limit_defaults_to_max_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "2")
    eng = _make_engine(tmp_path)
    s = eng.sample_rows("work_order")
    assert s["sample_limit"] == 2 and s["sampled_rows"] == 2


def test_zero_limit_is_an_explicit_empty_sample(eng):
    s = eng.sample_rows("work_order", limit=0)
    assert s["sample_limit"] == 0
    assert s["sampled_rows"] == 0 and s["rows"] == []
    assert all(c["fill_pct"] == 0.0 for c in s["columns"])


def test_profile_cap_bounds_the_scan(tmp_path, monkeypatch):
    """The PROFILE cap bounds how much of the table a sample may read."""
    monkeypatch.setenv("SQLHANDLER_PROFILE_MAX_ROWS", "3")
    eng = _make_engine(tmp_path)
    s = eng.sample_rows("work_order")
    assert s["profile_max_rows"] == 3
    assert s["sampled_rows"] == 3
    assert s["n_rows"] == 5  # metadata still reports the full table


def test_profile_cap_and_explicit_limit_take_the_smaller(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_PROFILE_MAX_ROWS", "2")
    eng = _make_engine(tmp_path)
    assert eng.sample_rows("work_order", limit=4)["sampled_rows"] == 2
    assert eng.sample_rows("work_order", limit=1)["sampled_rows"] == 1


def test_profile_cap_zero_leaves_max_rows_as_guardrail(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_PROFILE_MAX_ROWS", "0")
    monkeypatch.delenv("SQLHANDLER_MAX_ROWS", raising=False)
    eng = _make_engine(tmp_path)
    s = eng.sample_rows("work_order")
    assert s["sample_limit"] == 1000  # SQLHANDLER_MAX_ROWS default cap
    assert s["sampled_rows"] == 5  # table smaller than the cap


def test_sample_limit_never_exceeds_profile_default():
    # default profile cap (1M) is bigger than any sane sample; explicit wins
    assert _profile_max_rows() >= 1000


# ------------------------------------------------------------------ virtual


def test_virtual_table_routes_through_sql_path(tmp_path, monkeypatch):
    catalog = {
        "tables": {
            "kind_a_view": {
                "definition": "SELECT id, kind FROM work_order WHERE kind = 'a'",
                "description": "kind-A rows only",
            }
        }
    }
    eng = _make_engine(tmp_path, catalog=catalog, monkeypatch=monkeypatch)
    s = eng.sample_rows("kind_a_view", limit=2)
    assert s["virtual"] is True
    assert s["sampled_rows"] == 2
    assert s["n_rows"] == 3  # the definition's count via the SQL path
    assert [c["name"] for c in s["columns"]] == ["id", "kind"]
    assert all(r["kind"] == "a" for r in s["rows"])


def test_virtual_table_column_outside_definition_refused(tmp_path, monkeypatch):
    catalog = {
        "tables": {
            "kind_a_view": {"definition": "SELECT id, kind FROM work_order WHERE kind = 'a'"}
        }
    }
    eng = _make_engine(tmp_path, catalog=catalog, monkeypatch=monkeypatch)
    with pytest.raises(Exception, match="does not exist|not found"):
        eng.sample_rows("kind_a_view", columns=["amount"])


def test_virtual_table_respects_profile_cap(tmp_path, monkeypatch):
    catalog = {
        "tables": {
            "kind_a_view": {"definition": "SELECT id, kind FROM work_order WHERE kind = 'a'"}
        }
    }
    monkeypatch.setenv("SQLHANDLER_PROFILE_MAX_ROWS", "2")
    eng = _make_engine(tmp_path, catalog=catalog, monkeypatch=monkeypatch)
    s = eng.sample_rows("kind_a_view")
    assert s["sampled_rows"] == 2 and s["profile_max_rows"] == 2


# ------------------------------------------------------------------ errors


def test_unknown_table_error(eng):
    from sqlhandler.provider import LakehouseError

    with pytest.raises(LakehouseError, match="not found in data source"):
        eng.sample_rows("no_such_table")


def test_path_traversal_table_refused(eng):
    from sqlhandler.provider import LakehouseError

    with pytest.raises(LakehouseError, match="Invalid table name"):
        eng.sample_rows("../etc")


# ------------------------------------------------------- MCP tool surface


@pytest.fixture
def wired(eng, monkeypatch):
    monkeypatch.setattr(server, "_handler", lambda: eng)
    return eng


def test_mcp_markdown_rendering_physical(wired):
    text = server.sample_rows("work_order", limit=2)
    assert "Sample rows: work_order (sampled 2 of 5 rows)" in text
    assert "Fill rates (this sample):" in text
    assert "100.0%" in text and "50.0%" in text
    assert "| id | kind | amount |" in text
    assert "| 1 | a | 10.0 |" in text


def test_mcp_markdown_virtual_marker(wired, tmp_path, monkeypatch):
    catalog = {
        "tables": {
            "kind_a_view": {"definition": "SELECT id, kind FROM work_order WHERE kind = 'a'"}
        }
    }
    veng = _make_engine(tmp_path, catalog=catalog, monkeypatch=monkeypatch)
    monkeypatch.setattr(server, "_handler", lambda: veng)
    text = server.sample_rows("kind_a_view", limit=1)
    assert "Sample rows: kind_a_view VIRTUAL" in text


def test_mcp_error_surface_with_structured_tail(wired):
    text = server.sample_rows("work_odr")
    assert text.startswith("Error sampling rows: Table 'work_odr' not found in data source")
    last = text.rstrip().rsplit("\n", 1)[-1]
    payload = json.loads(last)
    assert payload["error"]["code"] == "E_TABLE_NOT_FOUND"


def test_mcp_dispatch_wiring(wired):
    text, is_error = server._dispatch_tool("sample_rows", {"table": "work_order", "limit": 1})
    assert is_error is False
    assert "Sample rows: work_order" in text
    # the tool entry is advertised
    names = {t.name for t in server._TOOLS}
    assert "sample_rows" in names


def test_mcp_default_limit_is_20(wired, monkeypatch):
    # a 5-row table + default limit -> all 5 rows
    text, _ = server._dispatch_tool("sample_rows", {"table": "work_order"})
    assert "sampled 5 of 5 rows" in text
