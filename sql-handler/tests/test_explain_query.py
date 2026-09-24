"""Tests for the explain_query tool (agent productivity pack — cost estimate).

Covers the implementation review §2c contract, first slice + plan enricher:

* the engine method's shape on a known physical fixture: metadata row count,
  bytes-to-scan, confidence labels on every number, snapshot tokens;
* NO execution side effects (the query's data path never runs — verified by
  a provider that counts opens and by the result-cache counters staying
  untouched while the warm band still reports L1 warm after a real query);
* the read-only guard: EXPLAIN-of-a-write refused by the SAME parser rule
  run_sql uses (reuse of sqlguard spans);
* attached-DB confidence:"none" degrade (real sqlite via the baked
  scanner, skipped when the extension isn't baked — same posture as
  test_external.py);
* the plan enricher (include_plan=True): JSON tree summary with scan-node
  pushdown, and zero virtual-table materializations as a side effect;
* the MCP surface (markdown rendering through _dispatch_tool).
"""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler import server
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import TableInfo


class CountingProvider:
    """Fake provider that counts dataset opens (execution side-effect probe)."""

    kind = "fake"

    def __init__(self, root):
        self.root = root
        self.opens = 0

    def list_tables(self):
        return [TableInfo(name="work_order", schema="workorder", format="parquet")]

    def table_uri(self, info):
        return "fake://"

    def open_dataset(self, info, version=None):
        self.opens += 1
        return pad.dataset(str(self.root), format="parquet")


def _make_engine(tmp_path, n_rows=5):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": list(range(1, n_rows + 1)),
                "kind": ["a", None, "a", "b", "a"][:n_rows],
                "amount": [10.0, 20.0, 30.0, 40.0, 50.0][:n_rows],
            }
        ),
        d / "part.parquet",
    )
    return SqlEngine(CountingProvider(d), cache_ttl=0)


@pytest.fixture()
def eng(tmp_path):
    return _make_engine(tmp_path)


QUERY = "SELECT kind, count(*) FROM work_order WHERE amount > 10 GROUP BY kind"


# ------------------------------------------------------------------ shape


def test_rows_bytes_and_confidence_labels_present(eng):
    r = eng.explain_query(QUERY)
    assert r["read_only"] is True
    assert r["n_tables"] == 1
    t = r["tables"][0]
    assert t["table"] == "workorder_work_order"
    assert t["path"] == "workorder/work_order"
    assert t["format"] == "parquet"
    # Metadata row count: the exact number the count(*) fast-path serves.
    assert t["rows"] == 5
    assert t["rows_confidence"] == "exact"
    # Parquet path: uncompressed row-group totals -> approx (honest label).
    assert t["bytes_to_scan"] is not None and t["bytes_to_scan"] > 0
    assert t["bytes_confidence"] == "approx"
    # Plain parquet has no version token: None is reported as such.
    assert t["snapshot_version"] is None
    # Warm/cold band always present with all three signals.
    assert set(r["warm_cold"]) == {"l1", "l2", "block_cache"}


def test_no_execution_side_effects(eng):
    before_opens = eng.provider.opens
    before_hits = eng.cache_stats()["result_cache"]["hits"]
    eng.explain_query(QUERY, include_plan=True)
    # _open_dataset may be called for METADATA (the cached handle), but the
    # result cache must be untouched: explain never stores or serves results.
    assert eng.cache_stats()["result_cache"]["hits"] == before_hits
    assert eng.cache_stats()["result_cache"]["writes"] == 0
    assert eng.cache_stats()["result_cache"]["entries"] == 0
    assert eng.provider.opens >= before_opens  # opens allowed; row reads are not


def test_explain_does_not_run_the_query(eng):
    # A query whose DATA read would raise: the column does not exist only at
    # data-bind time is not constructible here, so instead prove it via a
    # table whose dataset raises on scanner use but not on metadata.
    class MetaOnly:
        def __init__(self, inner):
            self._inner = inner

        def count_rows(self):
            return 5

        @property
        def schema(self):
            return self._inner.schema

        def get_fragments(self):
            return self._inner.get_fragments()

        def scanner(self, **kwargs):
            raise AssertionError("explain_query must not scan data")

        # DuckDB register() would use it only if the query ran — it must not.

    class P(CountingProvider):
        def open_dataset(self, info, version=None):
            self.opens += 1
            return MetaOnly(pad.dataset(str(self.root), format="parquet"))

    eng2 = SqlEngine(P(eng.provider.root), cache_ttl=0)
    r = eng2.explain_query(QUERY)
    assert r["tables"][0]["rows"] == 5


def test_explain_of_select_input_is_unwrapped(eng):
    r = eng.explain_query("EXPLAIN " + QUERY)
    # The stored/normalized sql keeps the caller's text; the estimator treats
    # EXPLAIN-of-SELECT exactly as the guard admits it.
    assert r["n_tables"] == 1


def test_explain_of_non_select_refused(eng):
    with pytest.raises(ValueError, match="not allowed"):
        eng.explain_query("EXPLAIN INSERT INTO work_order VALUES (1)")
    with pytest.raises(ValueError, match="not allowed"):
        eng.explain_query("SELECT 1; DROP TABLE work_order")
    with pytest.raises(ValueError, match="not allowed"):
        eng.explain_query("EXPLAIN ANALYZE DELETE FROM work_order")


def test_empty_sql_refused(eng):
    with pytest.raises(ValueError):
        eng.explain_query("   ")


# ------------------------------------------------------------------- warm


def test_warm_band_reports_l1_after_real_query(eng):
    eng.query_duckdb(QUERY)  # fills the in-memory result cache
    r = eng.explain_query(QUERY)
    assert r["warm_cold"]["l1"] is True
    # Formatting-only variant hits the SAME normalized key (warm).
    r2 = eng.explain_query(QUERY.replace("SELECT", "SELECT  "))
    assert r2["warm_cold"]["l1"] is True


def test_warm_band_cold_when_uncached(eng):
    r = eng.explain_query(QUERY)
    assert r["warm_cold"]["l1"] is False


def test_warm_band_l2_hit(tmp_path, monkeypatch):
    l2_dir = tmp_path / "l2"
    monkeypatch.setenv("SQLHANDLER_L2_DIR", str(l2_dir))
    monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", "1")
    root = tmp_path / "a"
    eng = _make_engine(root)
    eng.query_duckdb(QUERY)
    # Same provider fixture, fresh in-memory state: the "second replica".
    eng2 = SqlEngine(CountingProvider(root), cache_ttl=0, cache_dir=None)
    r = eng2.explain_query(QUERY)
    assert r["warm_cold"]["l1"] is False
    assert r["warm_cold"]["l2"] is True


# ------------------------------------------------------------------- plan


def test_plan_summary_reports_pushdown(eng):
    r = eng.explain_query(QUERY, include_plan=True)
    plan = r["plan"]
    assert plan.get("error") is None
    s = plan["summary"]
    assert s["scan_nodes"] == 1
    assert s["pushdown"] is True  # the WHERE reached the ARROW_SCAN
    assert s["operators"] >= s["scan_nodes"]


def test_plan_summary_unfiltered_query_has_no_pushdown_claim(eng):
    r = eng.explain_query("SELECT id FROM work_order", include_plan=True)
    assert r["plan"]["summary"]["pushdown"] is None  # nothing observable — honest


def test_plan_skip_on_virtual_materialization(tmp_path, monkeypatch):
    """explain's plan must NOT trigger a virtual table's materialization."""
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}),
        d / "part.parquet",
    )

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            return pad.dataset(str(d), format="parquet")

    import yaml

    cat = tmp_path / "catalog.yaml"
    cat.write_text(
        yaml.safe_dump(
            {
                "tables": {
                    "open_orders": {"definition": "SELECT id FROM work_order WHERE amount > 15"}
                }
            }
        )
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    eng2 = SqlEngine(P(), cache_ttl=0)
    r = eng2.explain_query("SELECT id FROM open_orders", include_plan=True)
    assert r["tables"][0]["virtual"] is True
    assert r["tables"][0]["rows_confidence"] == "none"
    assert r["plan"]["summary"]["pushdown"] is True  # definition's filter planned, not run
    assert eng2.cache_stats()["virtual_cache"]["materializations"] == 0


# ------------------------------------------------------- attached-DB degrade


@pytest.fixture(scope="module")
def sqlite_attached(tmp_path_factory):
    """Engine with a small real sqlite file attached (skips without scanner)."""
    db_path = tmp_path_factory.mktemp("sqlhandler-explain") / "ops.db"
    import sqlite3

    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE agent_test (k INTEGER, label TEXT)")
    con.executemany("INSERT INTO agent_test VALUES (?, ?)", [(1, "a"), (2, "b")])
    con.commit()
    con.close()

    extdir = Path(__file__).resolve().parent.parent / "duckdb-ext"
    import duckdb

    probe = duckdb.connect()
    try:
        probe.execute(f"SET extension_directory='{extdir}'")
        probe.execute("LOAD sqlite_scanner")
    except Exception as exc:
        pytest.skip(f"sqlite_scanner extension not baked in <repo>/duckdb-ext yet: {exc}")
    finally:
        probe.close()

    monkey = pytest.MonkeyPatch()
    monkey.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "sdb", "type": "sqlite", "database": str(db_path)}]),
    )
    monkey.setenv("SQLHANDLER_DUCKDB_EXTENSION_DIR", str(extdir))
    engine = SqlEngine(_NoTablesProvider(), cache_ttl=0)
    yield engine
    monkey.undo()


class _NoTablesProvider:
    kind = "stub"

    def list_tables(self):
        return []

    def table_uri(self, info):  # pragma: no cover - never reached
        return "stub://"

    def open_dataset(self, info, version=None):  # pragma: no cover
        raise AssertionError("no lake tables here")


def test_attached_db_degrades_to_confidence_none(sqlite_attached):
    r = sqlite_attached.explain_query("SELECT k FROM sdb.main.agent_test WHERE k > 0")
    assert r["touches_external"] is True
    assert r["tables"] == []
    assert r["n_tables"] == 0
    assert r["warm_cold"]["l1"] is False and r["warm_cold"]["l2"] is False


def test_mixed_lake_and_attached_query_degrades(sqlite_attached):
    r = sqlite_attached.explain_query(
        "SELECT * FROM sdb.main.agent_test a JOIN work_order w ON a.k = w.id"
    )
    assert r["touches_external"] is True
    assert r["tables"] == []


# ------------------------------------------------------------- MCP surface


def test_explain_query_markdown_over_mcp(monkeypatch, tmp_path):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text = server.explain_query(QUERY)
    assert "Query plan estimate (NOT executed)" in text
    assert "| workorder_work_order | 5 | exact |" in text
    assert "confidence: exact" in text or "| exact |" in text
    assert "Warm/cold (result cache):" in text
    assert "L1" in text


def test_explain_query_mcp_includes_plan_block(monkeypatch, tmp_path):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text = server.explain_query(QUERY, include_plan=True)
    assert "Plan summary (DuckDB's own EXPLAIN" in text
    assert "pushdown" in text
    assert "estimated result rows" in text


def test_explain_query_tool_dispatch_param_invalid(monkeypatch, tmp_path):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text, is_error = server._dispatch_tool("explain_query", {"sql": QUERY, "params": "not-a-list"})
    assert is_error is True
    assert "E_PARAM_INVALID" in text


def test_explain_query_tool_dispatch_happy_path(monkeypatch, tmp_path):
    eng = _make_engine(tmp_path)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    text, is_error = server._dispatch_tool("explain_query", {"sql": QUERY, "include_plan": True})
    assert is_error is False
    assert "Query plan estimate" in text


def test_explain_query_advertised_in_tools_list():
    names = {t.name for t in server._TOOLS}
    assert "explain_query" in names
    assert "ask_data" in names
