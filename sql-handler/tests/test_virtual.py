"""Tests for virtual tables: semantic-catalog entries with a SQL ``definition``.

A virtual table must behave like a real one — listed (marked VIRTUAL),
describable (schema derived from the definition, docs merged on top),
queryable (constructed on the fly as a DuckDB view whose base tables are
pulled in transitively) — while never shadowing physical data and never
letting a broken definition break the catalog.
"""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest
import yaml

from sqlhandler.engine import SqlEngine
from sqlhandler.provider import LakehouseError, TableInfo

TABLES = [
    TableInfo(name="sales", schema="shop", format="parquet"),
]

# Deterministic default definition: the view body carries its own ORDER BY so
# LIMIT-based tests are stable.
BIG_SALES_DEF = "SELECT id, amount FROM sales WHERE amount > 15 ORDER BY id"


class FakeProvider:
    """A DataProvider backed by a local temp directory of Parquet files."""

    kind = "fake"

    def __init__(self, root):
        self.root = root
        self.versions: dict[str, int] = {}  # path -> snapshot token (mutable in tests)

    def list_tables(self):
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return self.versions.get(info.path)


def _write(root: Path, rel: str, table: pa.Table) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, d / "part.parquet")


def _catalog_doc(
    name="vw_big_sales",
    definition=BIG_SALES_DEF,
    **extra,
):
    entry = {
        "description": "Big sales only",
        "aliases": ["big sales"],
        "columns": {"amount": "Order total"},
        "definition": definition,
    }
    entry.update(extra)
    return {"tables": {name: entry}}


def _make_engine(tmp_path: Path, monkeypatch, doc=None) -> tuple[SqlEngine, Path]:
    """Engine over one physical table + a catalog file; returns (engine, path)."""
    _write(tmp_path, "shop/sales", pa.table({"id": [1, 2, 3], "amount": [10.5, 20.0, 30.5]}))
    cat = tmp_path / "catalog.yaml"
    cat.write_text(yaml.safe_dump(doc if doc is not None else _catalog_doc()), encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    # cache_dir isolates the virtual materialization cache per test (the
    # metadata disk-warm layer stays off: cache_ttl=0)
    return SqlEngine(FakeProvider(tmp_path), cache_ttl=0, cache_dir=str(tmp_path)), cat


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    return _make_engine(tmp_path, monkeypatch)[0]


# ---------------------------------------------------------------------------
# listing + describing
# ---------------------------------------------------------------------------


def test_virtual_table_listed_with_virtual_format(engine):
    infos = {i.name: i for i in engine.list_tables()}
    assert set(infos) == {"sales", "vw_big_sales"}
    assert infos["vw_big_sales"].format == "virtual"
    assert infos["sales"].format == "parquet"


def test_describe_derives_real_schema_and_merges_docs(engine):
    d = engine.describe_table("vw_big_sales")
    assert d["virtual"] is True
    assert d["uri"] == "virtual://vw_big_sales"
    cols = {c["name"]: c for c in d["columns"]}
    # types come from the engine's binding of the definition, not the docs
    assert cols["id"]["type"] == "BIGINT"
    assert cols["amount"]["type"] == "DOUBLE"
    # catalog documentation merges on top, exactly like physical tables
    assert d["description"] == "Big sales only"
    assert d["aliases"] == ["big sales"]
    assert cols["amount"]["description"] == "Order total"


def test_describe_reflects_catalog_edit_without_restart(tmp_path, monkeypatch):
    eng, cat = _make_engine(tmp_path, monkeypatch)
    d1 = eng.describe_table("vw_big_sales")
    assert [c["name"] for c in d1["columns"]] == ["id", "amount"]
    cat.write_text(
        yaml.safe_dump(_catalog_doc(definition="SELECT id FROM sales WHERE amount > 25")),
        encoding="utf-8",
    )
    d2 = eng.describe_table("vw_big_sales")
    assert [c["name"] for c in d2["columns"]] == ["id"]


# ---------------------------------------------------------------------------
# querying
# ---------------------------------------------------------------------------


def test_run_sql_on_virtual_table(engine):
    out = engine.query_duckdb("SELECT * FROM vw_big_sales")
    assert out.column("id").to_pylist() == [2, 3]


def test_virtual_definition_pulls_in_base_tables(engine):
    # the user SQL names ONLY the virtual table; its base table must be
    # registered transitively from the definition.
    out = engine.query_duckdb("SELECT count(*) AS n FROM vw_big_sales WHERE amount < 25")
    assert out.column("n").to_pylist() == [1]


def test_virtual_joins_physical_in_one_query(engine):
    out = engine.query_duckdb("SELECT s.id FROM shop_sales p JOIN vw_big_sales s USING (id) ORDER BY s.id")
    assert out.column("id").to_pylist() == [2, 3]


def test_nested_virtual_tables(tmp_path, monkeypatch):
    doc = {
        "tables": {
            "vw_sales_x2": {"definition": "SELECT id, amount * 2 AS amount FROM sales"},
            "vw_sales_x2_big": {"definition": "SELECT id FROM vw_sales_x2 WHERE amount > 40"},
        }
    }
    eng, _ = _make_engine(tmp_path, monkeypatch, doc=doc)
    assert eng.describe_table("vw_sales_x2_big")["columns"][0]["name"] == "id"
    out = eng.query_duckdb("SELECT * FROM vw_sales_x2_big ORDER BY id")
    assert out.column("id").to_pylist() == [3]


def test_iff_compat_macro_for_snowflake_style_definitions(tmp_path, monkeypatch):
    doc = _catalog_doc(
        name="vw_iff_test",
        definition="SELECT id, iff(amount > 15, 'big', 'small') AS size FROM sales ORDER BY id",
    )
    eng, _ = _make_engine(tmp_path, monkeypatch, doc=doc)
    out = eng.query_duckdb("SELECT size FROM vw_iff_test")
    assert out.column("size").to_pylist() == ["small", "big", "big"]


def test_definition_cycle_raises_clear_error(tmp_path, monkeypatch):
    doc = {
        "tables": {
            "vw_a": {"definition": "SELECT * FROM vw_b"},
            "vw_b": {"definition": "SELECT * FROM vw_a"},
        }
    }
    eng, _ = _make_engine(tmp_path, monkeypatch, doc=doc)
    with pytest.raises(LakehouseError, match="cycle"):
        eng.query_duckdb("SELECT * FROM vw_a")


# ---------------------------------------------------------------------------
# collision, validation, search
# ---------------------------------------------------------------------------


def test_physical_table_shadows_virtual_with_same_name(tmp_path, monkeypatch):
    eng, _ = _make_engine(tmp_path, monkeypatch, doc=_catalog_doc(name="sales"))
    assert [i.format for i in eng.list_tables()] == ["parquet"]  # stored data wins
    out = eng.query_duckdb("SELECT count(*) AS n FROM sales")
    assert out.column("n").to_pylist() == [3]  # all rows, not the >15 definition


@pytest.mark.parametrize(
    "definition",
    [
        "SELECT 1; SELECT 2",  # more than one statement
        "CREATE TABLE t AS SELECT 1",  # not a read-only statement
        "SELEC 1",  # does not parse
    ],
)
def test_invalid_definitions_are_dropped_but_never_break_listing(tmp_path, monkeypatch, definition):
    doc = {
        "tables": {
            "vw_broken": {"description": "docs still merge", "definition": definition},
            "sales": {"description": "physical docs"},
        }
    }
    eng, _ = _make_engine(tmp_path, monkeypatch, doc=doc)
    assert [i.name for i in eng.list_tables()] == ["sales"]
    # the broken entry still documents the table it keyed
    assert eng.table_description(TableInfo(name="sales", schema="shop")) == "physical docs"


def test_virtual_true_without_definition_is_just_docs(tmp_path, monkeypatch):
    doc = {"tables": {"vw_marked": {"virtual": True, "description": "x"}}}
    eng, _ = _make_engine(tmp_path, monkeypatch, doc=doc)
    assert [i.name for i in eng.list_tables()] == ["sales"]


def test_search_tables_finds_virtual_by_alias(engine):
    hits = engine.search_tables("big sales")
    assert hits and hits[0]["name"] == "vw_big_sales"
    assert hits[0]["format"] == "virtual"


# ---------------------------------------------------------------------------
# profile + scan routes
# ---------------------------------------------------------------------------


def test_profile_virtual_runs_the_definition(engine):
    p = engine.profile_table("vw_big_sales")
    assert p["virtual"] is True
    assert p["n_rows"] == 2
    assert p["profiled_rows"] == 2
    by_name = {c["name"]: c for c in p["columns"]}
    assert float(by_name["amount"]["max"]) == 30.5  # SUMMARIZE reports min/max as strings


def test_scan_virtual_routes_through_sql(engine):
    out = engine.scan_arrow("vw_big_sales", columns=["id"], limit=1)
    assert out.column("id").to_pylist() == [2]  # deterministic: definition has ORDER BY


def test_scan_virtual_refuses_pyarrow_filters(engine):
    import pyarrow.compute as pc

    with pytest.raises(LakehouseError, match="run_sql"):
        engine.scan_arrow("vw_big_sales", filters=[pc.field("id") > 1])


# ---------------------------------------------------------------------------
# upload store + MCP surface
# ---------------------------------------------------------------------------


def test_definitions_work_through_the_upload_store(engine):
    engine.set_catalog_text(json.dumps(_catalog_doc(name="vw_uploaded")))
    d = engine.describe_table("vw_uploaded")
    assert d["virtual"] is True
    out = engine.query_duckdb("SELECT count(*) AS n FROM vw_uploaded")
    assert out.column("n").to_pylist() == [2]


def test_server_list_marks_virtual(engine, monkeypatch):
    from sqlhandler import server

    monkeypatch.setattr(server, "_handler", lambda: engine)
    text = server.list_tables()
    assert "vw_big_sales (VIRTUAL)" in text
    assert "computed on the fly" in text


def test_server_describe_marks_virtual(engine, monkeypatch):
    from sqlhandler import server

    monkeypatch.setattr(server, "_handler", lambda: engine)
    text = server.describe_table("vw_big_sales")
    assert "Kind: VIRTUAL" in text


def test_snowflake_array_construct_compact_runs_verbatim(tmp_path, monkeypatch):
    """The SV's original Snowflake definition text must run unmodified:
    ARRAY_CONSTRUCT_COMPACT is rewritten to list_filter, iff comes from the
    compat macro."""
    doc = _catalog_doc(
        name="vw_compact",
        definition=(
            "SELECT id, array_to_string(ARRAY_CONSTRUCT_COMPACT("
            "IFF(amount > 20, CAST(id AS VARCHAR) || ' big', NULL),"
            "iff(amount > 25, CAST(id AS VARCHAR) || ' huge', NULL)"
            "), ', ') AS tags FROM sales ORDER BY id"
        ),
    )
    eng, _ = _make_engine(tmp_path, monkeypatch, doc=doc)
    out = eng.query_duckdb("SELECT tags FROM vw_compact")
    # id 3 (30.5) hits both args, NULLs dropped: '3 big, 3 huge'. Rows with
    # no surviving elements get '' — exactly Snowflake's ARRAY_TO_STRING of
    # an empty array (matches the SV's "si viene vacío" wording).
    assert out.column("tags").to_pylist() == ["", "", "3 big, 3 huge"]


def test_compact_rewrite_is_string_literal_safe(tmp_path, monkeypatch):
    doc = _catalog_doc(
        name="vw_literal",
        definition=("SELECT id FROM sales WHERE 'call array_construct_compact(x, y)' <> '' ORDER BY id LIMIT 1"),
    )
    eng, _ = _make_engine(tmp_path, monkeypatch, doc=doc)
    out = eng.query_duckdb("SELECT id FROM vw_literal")
    assert out.column("id").to_pylist() == [1]  # literal untouched, query valid


# ---------------------------------------------------------------------------
# materialization cache
# ---------------------------------------------------------------------------


def _cache_files(engine) -> list[Path]:
    return list(Path(engine._virtual_cache_dir).glob("*.parquet"))


def test_virtual_cache_materializes_then_hits(engine):
    out1 = engine.query_duckdb("SELECT * FROM vw_big_sales ORDER BY id")
    files = _cache_files(engine)
    assert len(files) == 1
    assert engine._virtual_cache_writes == 1
    out2 = engine.query_duckdb("SELECT * FROM vw_big_sales ORDER BY id")
    assert engine._virtual_cache_writes == 1  # served from cache, no rewrite
    assert engine._virtual_cache_hits >= 1
    assert (
        out1.to_pylist()
        == out2.to_pylist()
        == [
            {"id": 2, "amount": 20.0},
            {"id": 3, "amount": 30.5},
        ]
    )


def test_virtual_cache_invalidated_by_base_version_change(engine):
    engine.provider.versions["shop/sales"] = 1
    engine.query_duckdb("SELECT count(*) FROM vw_big_sales")
    engine.provider.versions["shop/sales"] = 2  # ETL commit -> new snapshot
    engine.query_duckdb("SELECT count(*) FROM vw_big_sales")
    assert engine._virtual_cache_writes == 2  # re-materialized on the new snapshot


def test_virtual_cache_invalidated_by_definition_change(tmp_path, monkeypatch):
    eng, cat = _make_engine(tmp_path, monkeypatch)
    eng.query_duckdb("SELECT count(*) FROM vw_big_sales")
    cat.write_text(
        yaml.safe_dump(_catalog_doc(definition="SELECT id FROM sales WHERE amount > 25")),
        encoding="utf-8",
    )
    out = eng.query_duckdb("SELECT count(*) FROM vw_big_sales")
    assert out.column("count_star()").to_pylist() == [1]  # the NEW definition's rows
    assert eng._virtual_cache_writes == 2
    assert len(_cache_files(eng)) == 2  # both entries coexist; the old one is just stale


def test_virtual_cache_disabled_by_ttl_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_VIRTUAL_CACHE_TTL", "0")
    eng, _ = _make_engine(tmp_path, monkeypatch)
    eng.query_duckdb("SELECT count(*) FROM vw_big_sales")
    assert _cache_files(eng) == []
    assert eng._virtual_cache_writes == 0


def test_virtual_cache_bypassed_for_time_travel(engine):
    engine.query_duckdb("SELECT count(*) FROM vw_big_sales", version_as_of=1)
    assert _cache_files(engine) == []  # historical snapshots never materialize


def test_describe_does_not_materialize(engine):
    engine.describe_table("vw_big_sales")
    assert _cache_files(engine) == []  # describe binds the view, never executes it
    engine.query_duckdb("SELECT count(*) FROM vw_big_sales")
    assert len(_cache_files(engine)) == 1


def test_materialization_failure_falls_back_to_live_view(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupies the path", encoding="utf-8")
    monkeypatch.setenv("SQLHANDLER_VIRTUAL_CACHE_DIR", str(blocker))
    eng, _ = _make_engine(tmp_path, monkeypatch)
    out = eng.query_duckdb("SELECT count(*) FROM vw_big_sales")  # must not raise
    assert out.column("count_star()").to_pylist() == [2]
    assert eng._virtual_cache_writes == 0


# ---------------------------------------------------------------------------
# count(*) metadata fast-path + query result cache
# (engine-level speed features; tested here against the same fixtures)
# ---------------------------------------------------------------------------


def test_count_star_fastpath_matches_duckdb_shape(engine):
    plain = engine.query_duckdb("SELECT COUNT(*) FROM vw_big_sales")  # virtual: normal path
    phys = engine.query_duckdb("SELECT COUNT(*) FROM sales")  # physical: fast-path
    assert phys.column("count_star()").to_pylist() == [3]
    assert plain.column("count_star()").to_pylist() == [2]
    aliased = engine.query_duckdb("SELECT COUNT(*) AS n FROM sales")
    assert aliased.column("n").to_pylist() == [3]
    # a filtered count must NOT take the fast-path and must stay correct
    filtered = engine.query_duckdb("SELECT COUNT(*) FROM sales WHERE id = 1")
    assert filtered.column("count_star()").to_pylist() == [1]


def test_count_star_fastpath_respects_time_travel(engine):
    engine.provider.versions["shop/sales"] = 5
    out = engine.query_duckdb("SELECT COUNT(*) FROM sales", version_as_of=5)
    assert out.column("count_star()").to_pylist() == [3]  # snapshot opened, metadata counted


def test_result_cache_serves_identical_queries(engine):
    q = "SELECT id, amount FROM sales WHERE amount > 15 ORDER BY id"
    first = engine.query_duckdb(q)
    assert engine._result_cache_writes == 1
    second = engine.query_duckdb(q)
    assert engine._result_cache_hits == 1
    assert second.to_pylist() == first.to_pylist()
    # different params/limits are different identities
    engine.query_duckdb(q, limit=1)
    assert engine._result_cache_writes == 2


def test_result_cache_invalidated_by_base_version(engine):
    q = "SELECT count(*) AS c FROM sales WHERE amount > 15"
    engine.query_duckdb(q)
    engine.provider.versions["shop/sales"] = 7  # new ETL snapshot
    engine.query_duckdb(q)
    assert engine._result_cache_writes == 2  # re-ran, not served stale


def test_result_cache_disabled_and_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "0")
    eng, _ = _make_engine(tmp_path, monkeypatch)
    eng.query_duckdb("SELECT id FROM sales")
    assert eng._result_cache_writes == 0

    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "3600")
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_MAX_BYTES", "1")  # nothing fits
    eng2, _ = _make_engine(tmp_path, monkeypatch)
    eng2.query_duckdb("SELECT id FROM sales")
    assert eng2._result_cache_writes == 0  # too big for the cap -> served uncached


def test_result_cache_skips_virtual_tables(engine):
    engine.query_duckdb("SELECT count(*) AS c FROM vw_big_sales")
    assert engine._result_cache_writes == 0  # virtual: materialization layer owns speed
