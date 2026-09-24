"""Tests for the write tier (writes.py + engine.execute_write + server.run_sql).

The contract (DECISIONS.md + review §4):

* GLOBAL FLAG, DEFAULT FALSE — with SQLHANDLER_WRITES_ENABLED unset, every
  refusal is byte-identical to today's read-only service (double default:
  the read-only guard also still governs multi-statement/DDL).
* Classification on the SAME DuckDB-parser spans as the read guard —
  read | write_scratch | write_other; write_other refused in v1 (MERGE
  recognized and NAMED, refused until the dedicated writer connection).
* Subject-scoped namespaces: <root>/<subject-slug>/... only; anonymous and
  key-fingerprint callers get NO write capability, ever.
* Single-writer: in-process lock + advisory lease (O_EXCL + TTL);
  contention = retryable E_WRITE_CONFLICT.
* Never-cached invariant: classification precedes the cache check; a read
  cached before a write is never served after it.
* Delta round-trip: write via delta-rs, read back through the engine (the
  written table lists as an ordinary Delta table).
* Audit event:"write" + sqlhandler_writes_total{backend,outcome} additive.
"""

import json
import threading

import pyarrow as pa
import pytest

from sqlhandler import errors as _errors
from sqlhandler import observability, server, writes
from sqlhandler.config import FileConfig
from sqlhandler.engine import LakehouseError, SqlEngine
from sqlhandler.file import FileProvider
from sqlhandler.identity import ANONYMOUS, Caller

# --------------------------------------------------------------- fixtures


def _delta_engine(tmp_path, monkeypatch, *, scratch=True, audit=True):
    """A FileProvider engine over one Delta source table + a scratch root.

    Source: workorder/work_order (Delta). Scratch: <tmp>/scratch with the
    SQLHANDLER_* envs set. Returns (engine, alice-caller, scratch-root).
    """
    from deltalake import write_deltalake

    src = tmp_path / "workorder" / "work_order"
    src.mkdir(parents=True)
    write_deltalake(
        str(src),
        pa.table(
            {
                "id": pa.array([1, 2, 3], type=pa.int64()),
                "amount": pa.array([10.0, 20.0, 30.0], type=pa.float64()),
            }
        ),
        mode="overwrite",
    )
    scratch_root = tmp_path / "scratch"
    monkeypatch.setenv("SQLHANDLER_WRITES_ENABLED", "1")
    if scratch:
        monkeypatch.setenv("SQLHANDLER_WRITE_SCRATCH_ROOTS", f"main={scratch_root}")
    monkeypatch.setenv("SQLHANDLER_MCP_READONLY", "1")
    if audit:
        monkeypatch.setenv("SQLHANDLER_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SQLHANDLER_RESULT_CACHE_TTL", "3600")
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    alice = Caller(cls="user", subject="alice", via="relay")
    return eng, alice, scratch_root


# ------------------------------------------------------------ classification


def test_classification_matrix():
    """read / write_scratch / write_other for every statement shape."""
    reads = [
        "SELECT 1",
        "SELECT * FROM t WHERE x > 1",
        "WITH c AS (SELECT 1) SELECT * FROM c",
        "SHOW TABLES",
        "DESCRIBE SELECT 1",
        "EXPLAIN SELECT 1",
        "SUMMARIZE SELECT 1",
        "VALUES (1), (2)",
    ]
    for sql in reads:
        assert writes.classify_sql(sql)[0].kind == writes.CLASS_READ, sql

    scratch = [
        "CREATE TABLE out AS SELECT 1",
        "CREATE OR REPLACE TABLE s.out AS SELECT 1",
        "INSERT INTO s.out SELECT 1",
        "INSERT INTO s.out (a, b) SELECT 1, 2",
        "COPY (SELECT 1) TO 'out.parquet'",
    ]
    for sql in scratch:
        assert writes.classify_sql(sql)[0].kind == writes.CLASS_WRITE_SCRATCH, sql

    others = [
        ("UPDATE t SET a = 1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a", "MERGE_INTO"),
        ("CREATE TABLE t (a INT)", "CREATE"),  # DDL without AS SELECT
        ("INSERT INTO t VALUES (1)", "INSERT"),  # literal write
        ("ATTACH 'x.db' AS x", None),
        ("PRAGMA version", None),
    ]
    for sql, _typ in others:
        assert writes.classify_sql(sql)[0].kind == writes.CLASS_WRITE_OTHER, sql


def test_classification_merges_into_is_named_and_refused():
    """MERGE is RECOGNIZED (the error names it) but refused in v1."""
    c = writes.classify_sql("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a")[0]
    assert c.kind == writes.CLASS_WRITE_OTHER
    assert c.stmt_type == "MERGE_INTO"
    assert c.target == "t"
    assert "MERGE" in c.reason


def test_classification_explain_of_write_is_write_other():
    c = writes.classify_sql("EXPLAIN INSERT INTO t SELECT 1")[0]
    assert c.kind == writes.CLASS_WRITE_OTHER


def test_classification_ctas_extracts_source_select():
    c = writes.classify_sql("CREATE TABLE out AS SELECT id FROM work_order WHERE id > 1")[0]
    assert c.source_sql == "SELECT id FROM work_order WHERE id > 1"


def test_classification_insert_payload_rewrites_to_select():
    """INSERT INTO <target> ... -> the SELECT payload (DuckDB cannot INSERT
    into a registered view — the writer appends the ROWS via delta-rs)."""
    c = writes.classify_sql("INSERT INTO s.out (a, b) SELECT 1, 2")[0]
    assert c.source_sql == "SELECT 1, 2"


# ---------------------------------------------------------- flag / defaults


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("SQLHANDLER_WRITES_ENABLED", raising=False)
    assert writes.writes_enabled() is False
    monkeypatch.setenv("SQLHANDLER_WRITES_ENABLED", "0")
    assert writes.writes_enabled() is False
    monkeypatch.setenv("SQLHANDLER_WRITES_ENABLED", "1")
    assert writes.writes_enabled() is True
    monkeypatch.setenv("SQLHANDLER_WRITES_ENABLED", "true")
    assert writes.writes_enabled() is True


def test_flag_off_run_sql_refusals_byte_identical(monkeypatch, tmp_path):
    """With the tier OFF, run_sql's refusals are EXACTLY today's."""
    eng, _alice, _scratch = _delta_engine(tmp_path, monkeypatch)
    monkeypatch.delenv("SQLHANDLER_WRITES_ENABLED", raising=False)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    out = server.run_sql("CREATE TABLE x AS SELECT 1", caller=ANONYMOUS)
    assert out.startswith("Error running SQL:")
    assert "CREATE statements are not allowed" in out
    assert "SQLHANDLER_MCP_READONLY" in out  # the D2 escape-hatch note
    # and the engine-level gate refuses too (defense in depth)
    with pytest.raises(LakehouseError, match="write tier is disabled"):
        eng.execute_write("CREATE TABLE x AS SELECT 1", caller=Caller(cls="user", subject="a", via="relay"))


def test_readonly_guard_still_governs_with_writes_on(monkeypatch, tmp_path):
    """SQLHANDLER_MCP_READONLY semantics byte-identical with the tier ON:
    a multi-statement script is refused by the D2 guard BEFORE any write
    classification (the new flag gates only the NEW capability)."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    out = server.run_sql("CREATE TABLE t AS SELECT 1; DROP TABLE t", caller=alice)
    assert "not allowed" in out
    # single-statement writes flow through:
    summary = server.run_sql("CREATE TABLE ok_table AS SELECT 1 AS a", caller=alice)
    assert "Write complete." in summary


def test_no_roots_fail_closed(monkeypatch, tmp_path):
    """Flag ON + no allowlist = every target refuses (fail closed)."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch, scratch=False)
    with pytest.raises(LakehouseError, match="SQLHANDLER_WRITE_SCRATCH_ROOTS"):
        eng.execute_write("CREATE TABLE t AS SELECT 1", caller=alice)


# --------------------------------------------------------- subject scoping


def test_subject_slug_rules():
    assert writes.subject_slug(None) is None
    assert writes.subject_slug(ANONYMOUS) is None
    # fingerprint-class callers: NO write capability (rotating pseudonym)
    keyfp = Caller(cls="key", key_fp="sha256:abcd1234ef56", via="key")
    assert writes.subject_slug(keyfp) is None
    # attributed subjects slug + sanitize
    assert writes.subject_slug(Caller(cls="user", subject="Alice Smith", via="relay")) == "alice-smith"
    assert writes.subject_slug(Caller(cls="browser", subject="../etc/passwd", via="browser")) == "etc-passwd"


def test_anonymous_write_refused(monkeypatch, tmp_path):
    eng, _alice, _ = _delta_engine(tmp_path, monkeypatch)
    with pytest.raises(LakehouseError, match="no attributed subject"):
        eng.execute_write("CREATE TABLE t AS SELECT 1", caller=ANONYMOUS)
    with pytest.raises(LakehouseError, match="no attributed subject"):
        eng.execute_write("CREATE TABLE t AS SELECT 1", caller=None)


def test_key_fingerprint_write_refused(monkeypatch, tmp_path):
    eng, _alice, _ = _delta_engine(tmp_path, monkeypatch)
    fp_caller = Caller(cls="key", key_fp="sha256:abcd", via="key")
    with pytest.raises(LakehouseError, match="no attributed subject"):
        eng.execute_write("CREATE TABLE t AS SELECT 1", caller=fp_caller)


def test_target_scoped_under_subject_namespace(monkeypatch, tmp_path):
    _eng, alice, scratch_root = _delta_engine(tmp_path, monkeypatch)
    backend, canonical, _uri = writes.resolve_write_target("reports.daily", alice)
    assert backend == "delta"
    assert canonical == f"{scratch_root}/alice/reports/daily"


def test_target_traversal_refused(monkeypatch, tmp_path):
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    with pytest.raises(writes.WriteError, match="traversal"):
        writes.resolve_write_target("..", alice)
    for bad in ("a..b",):
        # a plain name containing dots-as-text is fine (single dir part)
        writes.resolve_write_target(bad, alice)
    del eng


def test_sibling_slug_not_aliased(monkeypatch, tmp_path):
    """<root>/alice must never match <root>/alice2 — trailing-slash prefix."""
    monkeypatch.setenv("SQLHANDLER_WRITE_SCRATCH_ROOTS", "main=/srv/scratch")
    alice = Caller(cls="user", subject="alice", via="relay")
    _b, canonical, _u = writes.resolve_write_target("t", alice)
    assert canonical == "/srv/scratch/alice/t"


# ------------------------------------------------------------- lease/locks


def test_lease_conflict_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_WRITE_LEASE_TTL", "60")
    target = str(tmp_path / "tbl")
    lease = writes.WriteLease(target)
    lease.acquire()
    try:
        with pytest.raises(writes.WriteError) as exc:
            writes.WriteLease(target).acquire()
        assert exc.value.code == writes.E_WRITE_CONFLICT
        assert exc.value.retryable is True
        # the structured tail carries the retryable code
        assert "E_WRITE_CONFLICT" in exc.value.structured()
    finally:
        lease.release()
    # after release a second writer acquires
    writes.WriteLease(target).acquire()


def test_lease_stale_break(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_WRITE_LEASE_TTL", "1")
    target = str(tmp_path / "tbl")
    lease = writes.WriteLease(target)
    lease.acquire()
    import time

    time.sleep(1.1)
    writes.WriteLease(target).acquire()  # stale lease broken, not refused


def test_in_process_lock_serializes(tmp_path):
    eng_lock = writes.single_writer_lock(("delta", str(tmp_path / "t")))
    acquired = []

    def hold():
        with eng_lock:
            acquired.append("holder")

    t = threading.Thread(target=hold)
    t.start()
    t.join(timeout=5)
    # same key returns the SAME lock object
    assert writes.single_writer_lock(("delta", str(tmp_path / "t"))) is eng_lock
    assert acquired == ["holder"]


# ------------------------------------------------------- delta round trip


def test_delta_ctas_roundtrip(monkeypatch, tmp_path):
    """CTAS into subject scratch; read back through the engine (the written
    table lists as an ordinary Delta table and queries normally)."""
    eng, alice, scratch_root = _delta_engine(tmp_path, monkeypatch)
    summary = eng.execute_write(
        "CREATE TABLE daily AS SELECT id, amount FROM work_order WHERE id > 1", caller=alice
    )
    row = summary.to_pydict()
    assert row["target"][0] == f"{scratch_root}/alice/daily"
    assert row["backend"][0] == "delta"
    assert row["rows_written"][0] == 2
    assert (scratch_root / "alice" / "daily" / "_delta_log").is_dir()

    infos = [t for t in eng.list_tables() if t.name == "daily" and t.schema == "alice"]
    assert infos and infos[0].format == "delta"
    res = eng.query_duckdb("SELECT id, amount FROM alice_daily ORDER BY id")
    assert res.to_pydict() == {"id": [2, 3], "amount": [20.0, 30.0]}


def test_delta_ctas_rerun_replaces(monkeypatch, tmp_path):
    """A re-run of the same CTAS REPLACES the target, schema included."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    eng.execute_write("CREATE TABLE t AS SELECT id, amount FROM work_order", caller=alice)
    eng.execute_write("CREATE TABLE t AS SELECT id FROM work_order", caller=alice)
    res = eng.query_duckdb("SELECT count(*) AS n FROM alice_t")
    assert res.to_pydict() == {"n": [3]}
    schema_cols = [f.name for f in eng._open_dataset(eng._resolve("alice/t")).schema]
    assert schema_cols == ["id"]


def test_delta_insert_appends(monkeypatch, tmp_path):
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    eng.execute_write("CREATE TABLE t AS SELECT id FROM work_order", caller=alice)
    eng.execute_write(
        "INSERT INTO t SELECT CAST(id + 10 AS BIGINT) AS id FROM work_order", caller=alice
    )
    res = eng.query_duckdb("SELECT count(*) AS n, max(id) AS mx FROM alice_t")
    assert res.to_pydict() == {"n": [6], "mx": [13]}


def test_delta_insert_schema_mismatch_refused(monkeypatch, tmp_path):
    """An append that does not match the target's schema is REFUSED, not
    silently schema-evolved."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    eng.execute_write("CREATE TABLE t AS SELECT id FROM work_order", caller=alice)
    with pytest.raises(LakehouseError, match="schema"):
        eng.execute_write(
            "INSERT INTO t SELECT CAST(id AS BIGINT) AS a, CAST(amount AS DOUBLE) AS b FROM work_order",
            caller=alice,
        )


def test_read_after_write_never_stale(monkeypatch, tmp_path):
    """THE never-cached invariant, read-side: a read cached before a write
    is never served after it (the CTAS drop-create RESETS the Delta log to
    version 0, so the snapshot-token invalidation cannot see it — the write
    keys its own eviction)."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    eng.execute_write("CREATE TABLE t AS SELECT id FROM work_order", caller=alice)
    assert eng.query_duckdb("SELECT count(*) AS n FROM alice_t").to_pydict() == {"n": [3]}
    eng.execute_write("CREATE TABLE t AS SELECT id + 100 AS id FROM work_order", caller=alice)
    assert eng.query_duckdb("SELECT count(*) AS n, min(id) AS mn FROM alice_t").to_pydict() == {
        "n": [3],
        "mn": [101],
    }


def test_write_never_stores_into_result_cache(monkeypatch, tmp_path):
    """The write itself leaves the result cache untouched (no summary row
    is cacheable, and a repeat write does not hit any cached entry)."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    before = len(eng._result_cache)
    eng.execute_write("CREATE TABLE t AS SELECT id FROM work_order", caller=alice)
    eng.execute_write("CREATE TABLE t AS SELECT id FROM work_order", caller=alice)
    assert len(eng._result_cache) == before


def test_read_after_insert_append_never_stale(monkeypatch, tmp_path):
    """The append shape too: a cached count before an INSERT is never
    served after it (the Delta version token advances — the snapshot-token
    invalidation catches it; this pins that the write path doesn't break
    the normal mechanism)."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    eng.execute_write("CREATE TABLE t AS SELECT id FROM work_order", caller=alice)
    assert eng.query_duckdb("SELECT count(*) AS n FROM alice_t").to_pydict() == {"n": [3]}
    eng.execute_write("INSERT INTO t SELECT CAST(id + 10 AS BIGINT) AS id FROM work_order", caller=alice)
    assert eng.query_duckdb("SELECT count(*) AS n FROM alice_t").to_pydict() == {"n": [6]}


def test_write_evicts_l2_entries_for_target(monkeypatch, tmp_path):
    """The L2 (shared-disk) layer is evicted with L1: a stale sidecar on
    the PVC must never serve another replica the pre-write content."""
    eng, alice, _scratch_root = _delta_engine(tmp_path, monkeypatch)
    monkeypatch.setenv("SQLHANDLER_L2_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_L2_DIR", str(tmp_path / "l2"))
    from sqlhandler.l2cache import L2ResultCache

    eng._l2_cache = L2ResultCache(str(tmp_path / "l2"), ttl=3600)
    eng._l2_min_bytes = 0  # publish even tiny results for this test
    eng.execute_write("CREATE TABLE t AS SELECT id FROM work_order", caller=alice)
    # a non-count read (the count fastpath bypasses the caches by design)
    # gets cached into L1+L2
    assert eng.query_duckdb("SELECT id FROM alice_t ORDER BY id").to_pydict()["id"] == [1, 2, 3]
    assert eng._l2_cache.stats()["writes"] >= 1
    # the write must evict the L2 entry for the written table
    eng.execute_write("CREATE TABLE t AS SELECT id + 100 AS id FROM work_order", caller=alice)
    assert eng.query_duckdb("SELECT id FROM alice_t ORDER BY id").to_pydict()["id"] == [101, 102, 103]


def test_write_summary_via_run_sql(monkeypatch, tmp_path):
    """MCP surface: the write SUMMARY returns in place of rows."""
    eng, alice, scratch_root = _delta_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_handler", lambda: eng)
    out = server.run_sql("CREATE TABLE s1 AS SELECT id FROM work_order", caller=alice)
    assert "Write complete." in out
    assert f"{scratch_root}/alice/s1" in out
    assert "delta" in out


def test_write_target_collision_with_source_refused(monkeypatch, tmp_path):
    """Write targets are ALWAYS outside the covered source tables: a target
    resolving ONTO a source table's location refuses."""
    from deltalake import write_deltalake

    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    # point the scratch ROOT at the provider root: subject slug + name can
    # then resolve onto the source table's location
    monkeypatch.setenv(
        "SQLHANDLER_WRITE_SCRATCH_ROOTS", f"main={tmp_path}"
    )
    workorder_caller = Caller(cls="user", subject="workorder", via="relay")
    with pytest.raises(LakehouseError, match="resolves onto a configured source"):
        eng.execute_write("CREATE TABLE work_order AS SELECT id FROM work_order", caller=workorder_caller)
    # the benign same-name-different-location case still works
    eng.execute_write("CREATE TABLE work_order AS SELECT id FROM work_order", caller=alice)
    assert eng.query_duckdb("SELECT count(*) AS n FROM alice_work_order").to_pydict() == {"n": [3]}
    del write_deltalake


def test_write_source_query_policy_masked(monkeypatch, tmp_path):
    """The documented policy decision: the mask applies to reads INSIDE the
    write — what lands in scratch is exactly what the caller could read."""
    eng, alice, _scratch_root = _delta_engine(tmp_path, monkeypatch)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "groups": {
                    "analysts": {"tables": {"workorder/*": {"column_masks": {"amount": "redact"}}}}
                },
                "subjects": {"alice": ["analysts"]},
            }
        )
    )
    monkeypatch.setenv("SQLHANDLER_POLICY_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_POLICY_FILE", str(policy_file))
    from sqlhandler.policy import reset_policy_store

    reset_policy_store()
    try:
        eng.execute_write("CREATE TABLE masked AS SELECT id, amount FROM work_order", caller=alice)
        res = eng.query_duckdb("SELECT id, amount FROM alice_masked ORDER BY id")
        pd = res.to_pydict()
        assert pd["amount"] == ["***", "***", "***"]  # masked in the WRITE too (redact = '***')
    finally:
        monkeypatch.delenv("SQLHANDLER_POLICY_ENABLED", raising=False)
        monkeypatch.delenv("SQLHANDLER_POLICY_FILE", raising=False)
        reset_policy_store()


def test_write_refuses_attach_touched_source(monkeypatch, tmp_path):
    """Source-never-sink holds for reads inside the write too: an
    attach-touching SELECT payload refuses (the exfiltration-shape guard)."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    eng.attaches = eng.attaches or []
    # No real attach configured — simulate the guard's decision function
    # directly (the refusal text is what the write path produces).
    orig = eng._sql_needs_external
    eng._sql_needs_external = lambda sql: True
    try:
        with pytest.raises(LakehouseError, match="attached external"):
            eng.execute_write("CREATE TABLE t AS SELECT 1", caller=alice)
    finally:
        eng._sql_needs_external = orig


# ---------------------------------------------------------------- pyiceberg


def test_pyiceberg_write_smoke(monkeypatch, tmp_path):
    """Iceberg write smoke (skipped cleanly when pyiceberg is missing)."""
    pytest.importorskip("pyiceberg")
    from deltalake import write_deltalake  # noqa: F401  (engine import shape)

    src = tmp_path / "workorder" / "work_order"
    src.mkdir(parents=True)
    pq = pytest.importorskip("pyarrow.parquet")
    pq.write_table(pa.table({"id": pa.array([1, 2], type=pa.int64())}), src / "part.parquet")
    scratch_root = tmp_path / "wh"
    monkeypatch.setenv("SQLHANDLER_WRITES_ENABLED", "1")
    monkeypatch.setenv(
        "SQLHANDLER_WRITE_SCRATCH_ROOTS", f"iceberg://ice={scratch_root}"
    )
    monkeypatch.setenv("SQLHANDLER_WRITE_ICEBERG_WAREHOUSE", f"file://{scratch_root}/warehouse")
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    alice = Caller(cls="user", subject="alice", via="relay")
    summary = eng.execute_write("CREATE TABLE ict AS SELECT id FROM work_order", caller=alice)
    row = summary.to_pydict()
    assert row["backend"][0] == "iceberg"
    assert row["rows_written"][0] == 2
    # a second write appends via the same catalog
    summary2 = eng.execute_write(
        "INSERT INTO ict SELECT CAST(id + 5 AS BIGINT) AS id FROM work_order", caller=alice
    )
    assert summary2.to_pydict()["rows_written"][0] == 2


# ------------------------------------------------------------ audit/metrics


def test_audit_write_lines_and_metrics(monkeypatch, tmp_path):
    eng, alice, scratch_root = _delta_engine(tmp_path, monkeypatch)
    from sqlhandler.policy import reset_policy_store

    reset_policy_store()
    eng.execute_write("CREATE TABLE m1 AS SELECT id FROM work_order", caller=alice)
    lines = [json.loads(l) for l in open(tmp_path / "audit.jsonl")]
    write_lines = [l for l in lines if l["event"] == "write"]
    assert write_lines and write_lines[0]["target"] == f"{scratch_root}/alice/m1"
    assert write_lines[0]["backend"] == "delta"
    assert write_lines[0]["n_rows"] == 3
    assert write_lines[0]["caller"]["subject"] == "alice"
    # query lines are untouched (additive family)
    assert all(l["event"] == "query" for l in lines if l["event"] != "write")
    reset_policy_store()

def test_metrics_write_series_additive(monkeypatch, tmp_path):
    """The write counters increment (process-global registry — assert on
    the DELTA, not absolute values, so suite order cannot flip this)."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    from sqlhandler.policy import reset_policy_store

    reset_policy_store()
    before = observability.metrics.writes.snapshot().get("delta", 0)
    before_ok = observability.metrics.write_outcomes.snapshot().get("ok", 0)
    eng.execute_write("CREATE TABLE m2 AS SELECT id FROM work_order", caller=alice)
    after = observability.metrics.writes.snapshot().get("delta", 0)
    after_ok = observability.metrics.write_outcomes.snapshot().get("ok", 0)
    assert after == before + 1
    assert after_ok == before_ok + 1
    text = observability.metrics.render(eng)
    assert 'sqlhandler_writes_total{backend="delta"}' in text
    assert "# HELP sqlhandler_queries_total" in text  # historical family intact
    reset_policy_store()


def test_multi_statement_write_refused(monkeypatch, tmp_path):
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    with pytest.raises(LakehouseError, match="exactly one statement"):
        eng.execute_write("CREATE TABLE a AS SELECT 1; CREATE TABLE b AS SELECT 2", caller=alice)


def test_read_statement_refused_on_write_path(monkeypatch, tmp_path):
    """The write tier ADDS capability — a read on execute_write is refused
    (reads belong to run_sql/query_duckdb, which classify first)."""
    eng, alice, _ = _delta_engine(tmp_path, monkeypatch)
    with pytest.raises(LakehouseError, match="is a read"):
        eng.execute_write("SELECT 1", caller=alice)


def test_errors_enrich_recognizes_write_conflict():
    msg = "Write conflict: another writer holds the lease on /x/y (held 3s, TTL 300s). Retry after it completes."
    out = _errors.enrich(msg)
    assert "E_WRITE_CONFLICT" in out
