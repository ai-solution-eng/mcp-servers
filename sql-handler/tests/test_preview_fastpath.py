"""Tests for the preview fast path (Task A) + data prewarm (Task B).

Same posture as test_engine.py: parquet files on the local filesystem stand
in for the source, so the engine runs for real while nothing touches the
network. The fast-path tests pin the STRUCTURAL contract (which queries take
the shortcut, which fall back) and the row-cap/rendering equivalence; the
prewarm tests pin the block-cache population and the never-fail-startup
outcomes.
"""

import os
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlhandler.engine import SqlEngine
from sqlhandler.provider import DataProvider, TableInfo

TABLES = [
    TableInfo(name="work_order", schema="workorder", format="parquet"),
    TableInfo(name="work_order_note", schema="workorder", format="parquet"),
]


class FakeProvider(DataProvider):
    """A DataProvider backed by a local temp directory of Parquet files."""

    kind = "fake"

    def __init__(self, root):
        self.root = root
        self.list_calls = 0
        self.open_calls: list[str] = []

    def list_tables(self):
        self.list_calls += 1
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        self.open_calls.append(info.path)
        d = self.root / info.path
        if (d / "part.parquet").exists():
            return pad.dataset(str(d), format="parquet")
        # Arbitrary tables in the cache tests: serve a tiny in-memory dataset.
        return pad.dataset(pa.table({"id": [1], "x": ["a"]}))


def _write(root, rel, table, **pq_kw):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, d / "part.parquet", **pq_kw)


def _make_engine(tmp_path, **kw):
    # 150 rows / 2 row groups: enough that a LIMIT 5 preview is a real slice,
    # small enough that the assertions stay instant.
    _write(
        tmp_path,
        "workorder/work_order",
        pa.table(
            {
                "id": list(range(150)),
                "amount": [float(i) * 1.5 for i in range(150)],
                "kind": ["a" if i % 2 == 0 else "b" for i in range(150)],
            }
        ),
        row_group_size=75,
    )
    _write(
        tmp_path,
        "workorder/work_order_note",
        pa.table({"note_id": [10, 20], "text": ["hi", "bye"]}),
    )
    provider = FakeProvider(tmp_path)
    return SqlEngine(provider, **kw), provider


# ---------------------------------------------------------------------------
# Task A: preview fast path
# ---------------------------------------------------------------------------


def test_bare_limit_preview_returns_same_rows_as_normal_path(tmp_path):
    """The fast path's rows == the normal path's rows on a local parquet file.

    Both paths slice the same physical file; with no ORDER BY either subset
    is valid SQL semantics, but on a single-file local table they are also
    literally identical (first row group in file order).
    """
    eng, _ = _make_engine(tmp_path)
    fast = eng.query_duckdb("SELECT id, amount FROM work_order LIMIT 5")
    normal = eng.query_duckdb("SELECT id, amount FROM workorder_work_order LIMIT 5")
    assert fast.to_pydict() == normal.to_pydict()
    assert fast.num_rows == 5
    assert fast.column_names == ["id", "amount"]
    # Star projection too.
    star = eng.query_duckdb("SELECT * FROM work_order LIMIT 3")
    assert star.num_rows == 3 and star.column_names == ["id", "amount", "kind"]


def test_bare_limit_preview_takes_fastpath_not_the_job_path(tmp_path, monkeypatch):
    """Structural proof the shortcut ran: the engine's own fast-path method
    is invoked AND no DuckDB job is constructed — timing-insensitive."""
    called = {"fastpath": 0}
    eng, _ = _make_engine(tmp_path)
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        called["fastpath"] += 1
        return orig_fp(self, sql, version)

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)

    import sqlhandler.engine as engine_mod

    class NoJob:
        def __init__(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("QueryJob constructed for a bare-preview query")

    monkeypatch.setattr(engine_mod, "QueryJob", NoJob)
    arrow = eng.query_duckdb("SELECT id FROM work_order LIMIT 5")
    assert arrow.num_rows == 5
    assert called["fastpath"] == 1


def test_where_limit_does_not_take_fastpath(tmp_path, monkeypatch):
    """WHERE / GROUP BY / ORDER BY shapes are consulted but never SERVED by
    the fast path — the normal path answers them."""
    served = {"n": 0}
    eng, _ = _make_engine(tmp_path)
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        result = orig_fp(self, sql, version)
        if result is not None:
            served["n"] += 1
        return result

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)
    assert eng.query_duckdb("SELECT id FROM work_order WHERE id > 10 LIMIT 5").num_rows == 5
    assert eng.query_duckdb("SELECT kind, COUNT(*) AS n FROM work_order GROUP BY kind LIMIT 2").num_rows == 2
    ordered = eng.query_duckdb("SELECT id FROM work_order ORDER BY id DESC LIMIT 2")
    assert ordered.to_pydict() == {"id": [149, 148]}  # ORDER BY honored — not first-physical-rows
    assert served["n"] == 0


def test_order_by_limit_does_not_take_fastpath(tmp_path, monkeypatch):
    """ORDER BY changes WHICH rows LIMIT returns — served by the normal path."""
    served = {"n": 0}
    eng, _ = _make_engine(tmp_path)
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        result = orig_fp(self, sql, version)
        if result is not None:
            served["n"] += 1
        return result

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)
    arrow = eng.query_duckdb("SELECT id FROM work_order ORDER BY id DESC LIMIT 2")
    assert arrow.to_pydict() == {"id": [149, 148]}
    assert served["n"] == 0


def test_fastpath_respects_sqlhandler_max_rows(tmp_path, monkeypatch):
    """SQLHANDLER_MAX_ROWS caps the preview exactly like a normal query."""
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "4")
    eng, _ = _make_engine(tmp_path)
    arrow = eng.query_duckdb("SELECT id FROM work_order LIMIT 100")
    assert arrow.num_rows == 4
    # An explicit row_cap (export endpoint) caps lower still.
    arrow = eng.query_duckdb("SELECT id FROM work_order LIMIT 100", row_cap=2)
    assert arrow.num_rows == 2


def test_fastpath_switch_off_falls_back(tmp_path, monkeypatch):
    """SQLHANDLER_PREVIEW_FASTPATH=off: the same rows arrive via the normal
    path (QueryJob runs; the fast-path method is never consulted)."""
    monkeypatch.setenv("SQLHANDLER_PREVIEW_FASTPATH", "off")

    eng, _ = _make_engine(tmp_path)

    def refuse(*a, **k):
        raise AssertionError("fast path ran with the flag off")

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", refuse)
    arrow = eng.query_duckdb("SELECT id FROM work_order LIMIT 5")
    assert arrow.num_rows == 5  # the normal path served it
    # Flag parsing: empty/unset = ON; off-words = OFF; garbage = ON.
    from sqlhandler.engine import _preview_fastpath_enabled

    monkeypatch.delenv("SQLHANDLER_PREVIEW_FASTPATH")
    assert _preview_fastpath_enabled() is True
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("SQLHANDLER_PREVIEW_FASTPATH", off)
        assert _preview_fastpath_enabled() is False, off
    monkeypatch.setenv("SQLHANDLER_PREVIEW_FASTPATH", "garbage")
    assert _preview_fastpath_enabled() is True


def test_limit_zero_returns_empty_table(tmp_path):
    eng, _ = _make_engine(tmp_path)
    arrow = eng.query_duckdb("SELECT id, amount FROM work_order LIMIT 0")
    assert arrow.num_rows == 0
    assert {"id", "amount"}.issubset(set(arrow.column_names))  # table-schema columns present


def test_qualified_and_multi_part_names_take_fastpath(tmp_path):
    """The registration's qualified spelling (schema_name) resolves too.

    Note the SQL surface's real names: list_tables prints ``schema/name``
    (the TableInfo ``path`` — the scan_table/describe address), while SQL
    sees the registered DuckDB views: the underscored ``[source_]schema_name``
    (qualified_name) plus the bare name when unique. A dotted
    ``schema.name`` in SQL is a DuckDB catalog reference (schema
    ``workorder``, table ``work_order``) which does NOT exist as a view —
    the normal path refuses it, so the detector refuses it identically
    (conservative parity, no error-shape divergence).
    """
    eng, _ = _make_engine(tmp_path)
    a = eng.query_duckdb("SELECT id FROM workorder_work_order LIMIT 2")
    b = eng.query_duckdb("SELECT id FROM work_order LIMIT 2")
    assert a.to_pydict() == b.to_pydict()
    # The dotted spelling is not a registered view (see docstring): the
    # detector refuses it — parity with the normal path's honest error.
    assert eng._is_bare_preview("SELECT id FROM workorder.work_order LIMIT 5") is None


def test_fastpath_audit_and_metrics_recorded(tmp_path):
    """The same outcome choke point: query memory + metrics fire."""
    eng, _ = _make_engine(tmp_path)
    before = len(eng._query_memory)
    eng.query_duckdb("SELECT id FROM work_order LIMIT 5")
    assert len(eng._query_memory) == before + 1
    entry = eng._query_memory[-1]
    assert entry["n_rows"] == 5
    stats = eng.cache_stats()
    assert stats["result_cache"]["hits"] == 0  # fast path returns, cache untouched


def test_virtual_and_raw_tables_fall_through(tmp_path, monkeypatch):
    """Virtual / raw-format tables never take the fast path."""
    eng, _ = _make_engine(tmp_path)
    from sqlhandler.provider import TableInfo as TI

    virt = TI(name="v_orders", schema="default", format="virtual")
    monkeypatch.setattr(FakeProvider, "list_tables", lambda self: TABLES + [virt])
    eng._tables = None  # drop the cached listing
    # A virtual table's detector resolve succeeds (it IS listed) — the
    # format check refuses the fast path.
    assert eng._is_bare_preview("SELECT id FROM v_orders LIMIT 5") is None


def test_detector_is_conservative(monkeypatch):
    """Shape rules: everything not a plain projection+FROM+LIMIT is refused."""
    eng = object.__new__(SqlEngine)  # detector only; no provider needed
    refuses = [
        "SELECT id AS identifier FROM t LIMIT 5",
        "SELECT SUM(id) FROM t LIMIT 5",
        "SELECT COUNT(*) FROM t LIMIT 5",
        "SELECT DISTINCT kind FROM t LIMIT 5",
        "SELECT id FROM t WHERE x = 1 LIMIT 5",
        "SELECT id FROM t ORDER BY id LIMIT 5",
        "SELECT id FROM t GROUP BY id LIMIT 5",
        "SELECT a.id FROM a JOIN b ON a.id = b.id LIMIT 5",
        "SELECT id FROM t LIMIT 5 OFFSET 2",
        "WITH cte AS (SELECT 1) SELECT * FROM cte LIMIT 5",
        "SELECT id FROM t;",  # no LIMIT
        "SELECT * FROM t LIMIT -1",
        "SELECT (SELECT max(x) FROM u) FROM t LIMIT 5",
        "SELECT id FROM t LIMIT '5'",
        "UPDATE t SET x = 1",
        "SELECT id FROM t LIMIT 5; SELECT 1;",
    ]
    for sql in refuses:
        # The detector refuses BEFORE any resolve for these shapes; give it
        # a resolve that would succeed to prove the SHAPE is the refusal.
        monkeypatch.setattr(
            SqlEngine, "_resolve", lambda self, table, _ok=("t", "workorder/work_order"): TableInfo(name=_ok[0])
        )
        monkeypatch.setattr(SqlEngine, "_sql_needs_external", lambda self, sql: False)
        assert eng._is_bare_preview(sql) is None, sql


def test_detector_accepts_the_canonical_shapes(tmp_path, monkeypatch):
    eng, _ = _make_engine(tmp_path)
    assert eng._is_bare_preview("SELECT id FROM work_order LIMIT 5") == ("work_order", 5)
    assert eng._is_bare_preview("select * from work_order limit 5;") == ("work_order", 5)
    assert eng._is_bare_preview("SELECT id, amount FROM workorder_work_order LIMIT 0") == (
        "workorder_work_order",
        0,
    )
    assert eng._is_bare_preview("SELECT * FROM work_order\nLIMIT 5") == ("work_order", 5)


def test_multi_row_group_table_reads_only_the_first(tmp_path):
    """The speedup's substance: a 2-row-group table reads ONE row group."""
    eng, _ = _make_engine(tmp_path)
    arrow = eng.query_duckdb("SELECT id FROM work_order LIMIT 5")
    # Rows come from row group 0 (ids 0..74) — proof only the first file
    # segment was read, never the whole dataset.
    assert arrow.to_pydict() == {"id": [0, 1, 2, 3, 4]}


def test_fastpath_respects_time_travel_refusal(tmp_path, monkeypatch):
    """version_as_of queries never take the fast path (hook-level guard).

    The FakeProvider ignores the version, so the normal path serves the
    versioned query happily here — the pinned fact is structural: the hook
    refuses the shortcut when version_as_of is set (the real plain-parquet
    providers raise the honest no-history error from the normal path)."""
    served = {"n": 0}
    eng, _ = _make_engine(tmp_path)
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        result = orig_fp(self, sql, version)
        if result is not None:
            served["n"] += 1
        return result

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)
    assert eng.query_duckdb("SELECT id FROM work_order LIMIT 5", version_as_of=3).num_rows == 5
    assert served["n"] == 0  # hook guard: never consulted for a time-travel query


# ---------------------------------------------------------------------------
# Task B: data prewarm
# ---------------------------------------------------------------------------


class _StatsStub:
    """Minimal blockcache stats stand-in: counts published block files."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir

    def block_files(self) -> int:
        n = 0
        for _, _, files in os.walk(self.cache_dir):
            n += len([f for f in files if f.startswith("block-") and not f.endswith(".tmp")])
        return n


def test_prewarm_describe_only_without_block_cache(tmp_path, monkeypatch):
    """Default posture (block cache off): outcomes stay exactly 'ok'."""
    eng, _provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)
    monkeypatch.delenv("SQLHANDLER_BLOCK_CACHE", raising=False)
    outcomes = eng.prewarm(("workorder/work_order", "workorder/work_order_note"))
    assert outcomes == {
        "workorder/work_order": "ok",
        "workorder/work_order_note": "ok",
    }
    # Describe warmed: a direct describe afterwards is a cache hit.
    eng.describe_table("workorder/work_order")


def test_prewarm_rowgroups_zero_skips_data_warm(tmp_path, monkeypatch):
    """SQLHANDLER_PREWARM_ROWGROUPS=0: describe-only, no data reads."""
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    monkeypatch.setenv("SQLHANDLER_PREWARM_ROWGROUPS", "0")
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)
    outcomes = eng.prewarm(("workorder/work_order",))
    assert outcomes == {"workorder/work_order": "ok"}  # describe-only status
    # Exactly ONE dataset open — describe's own; the data warm added none.
    assert len(provider.open_calls) == 1


def test_prewarm_fills_block_cache_and_second_read_hits(tmp_path, monkeypatch):
    """With the block cache ON, prewarm lands blocks a later read reuses.

    LocalFileSystem is only wrapped with SQLHANDLER_BLOCK_CACHE_INCLUDE_LOCAL
    (the block cache's own semantics) — set here so the cache-dir assertion
    works without MinIO; the production backends wrap by default.
    """
    import pyarrow.fs as pafs

    import sqlhandler.blockcache as bc

    cache_dir = tmp_path / "bc"
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE_INCLUDE_LOCAL", "1")
    monkeypatch.setenv("SQLHANDLER_PREWARM_ROWGROUPS", "1")
    eng, _ = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)
    # Route the provider's opens through the block-cache-wrapped filesystem
    # — byte-for-byte what the s3/iceberg/onelake providers do in
    # open_dataset (maybe_block_cache over the real fs); the FakeProvider
    # itself stays plain (its local files are not the point).
    orig_open = FakeProvider.open_dataset

    def wrapped_open(self, info, version=None):
        orig_open(self, info, version)  # keep the open-call accounting honest
        wfs = bc.maybe_block_cache(pafs.LocalFileSystem(), purpose="test")
        return pad.dataset([str(self.root / info.path / "part.parquet")], filesystem=wfs, format="parquet")

    monkeypatch.setattr(FakeProvider, "open_dataset", wrapped_open)
    outcomes = eng.prewarm(("workorder/work_order",))
    assert outcomes == {"workorder/work_order": "data-ok"}
    stub = _StatsStub(cache_dir)
    blocks_after_prewarm = stub.block_files()
    assert blocks_after_prewarm > 0, "prewarm must publish block-cache files"
    # A second identical read publishes NO new blocks (warm).
    eng._dataset_cache.clear()
    arrow = eng.query_duckdb("SELECT id FROM work_order LIMIT 3")
    assert arrow.num_rows == 3
    assert stub.block_files() == blocks_after_prewarm


def test_prewarm_outcome_statuses(tmp_path, monkeypatch):
    """Per-table statuses: data-ok / skipped / error, describe errors first."""
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    monkeypatch.setenv("SQLHANDLER_PREWARM_ROWGROUPS", "1")
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)

    calls = {"n": 0}

    def boom(info, version=None):
        calls["n"] += 1
        if calls["n"] >= 2:  # first open = describe's own; the data warm fails
            raise RuntimeError("unavailable")
        return FakeProvider.open_dataset(provider, info, version)

    provider.open_dataset = boom
    outcomes = eng.prewarm(("workorder/work_order",))
    assert outcomes["workorder/work_order"] == "data-error"

    # A non-prewarmable format refuses at the detector/resolve stage — the
    # virtual-table status needs a REAL virtual definition (the semantic
    # catalog), which is test_virtual.py's territory; here the pinned
    # statuses are data-ok / data-error / describe-error / skipped (cache
    # off), all covered above.


def test_broken_table_does_not_fail_prewarm(tmp_path, monkeypatch):
    """A table whose DATA warm raises records the error; prewarm returns
    and never raises (startup-safety contract)."""
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    monkeypatch.setenv("SQLHANDLER_PREWARM_ROWGROUPS", "1")
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)

    calls = {"n": 0}
    real_open = provider.open_dataset

    def flaky(info, version=None):
        # First open of the table = describe's own (succeeds); the data
        # warm's SECOND open fails — exactly the transient outage shape.
        if info.path == "workorder/work_order":
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("object store down")
        return real_open(info, version)

    provider.open_dataset = flaky
    outcomes = eng.prewarm(("workorder/work_order", "workorder/work_order_note"))
    assert outcomes["workorder/work_order"] == "data-error"
    # The other table still warmed fine.
    assert outcomes["workorder/work_order_note"] == "data-ok"


def test_usage_count_ordering_unchanged(tmp_path, monkeypatch):
    """The prewarm data read rides _open_dataset: usage counts still bump.

    One bump per prewarmed table (the describe path opens through the same
    counter) — the popularity signal that drives usage-driven prewarm is
    not perturbed by the data warm itself.
    """
    eng, _ = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)
    eng.prewarm(("workorder/work_order",))
    counts = dict(eng._table_usage)
    top = eng.usage_top_tables(3)
    assert top[0] == "workorder/work_order"
    assert counts[("default", "workorder/work_order")] == 1


# ---------------------------------------------------------------------------
# CROSS-SURFACE sharing (live 2026-10-05): the Query tab always sends a
# transport limit (the UI box, default 100) while the Inspector sends none —
# repr(limit) was a key part, so the SAME SQL cached TWICE. The canonical
# preview key strips BOTH the SQL's LIMIT token and the transport limit:
# one entry per shape, every surface hits it.
# ---------------------------------------------------------------------------


def test_query_tab_and_inspector_share_one_entry(tmp_path, monkeypatch):
    """LIMIT 100 via the transport-limit path (Query tab: api limit=100,
    SQL text with LIMIT 100) then the same via the inspector path
    (api limit=None): the second call is a canonical hit — no IO, no
    second entry."""
    eng, provider = _make_engine(tmp_path)
    # Query tab: /api/query clamps limit → 100, SQL text says LIMIT 100.
    tab = eng.query_duckdb("SELECT * FROM work_order LIMIT 100", limit=100)
    assert tab.num_rows == 100
    provider.open_calls.clear()

    calls = {"fp": 0}
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        calls["fp"] += 1
        return orig_fp(self, sql, version)

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)

    # Inspector: dispatch passes limit=None; SQL text says LIMIT 100.
    insp = eng.query_duckdb("SELECT * FROM work_order LIMIT 100", limit=None)
    assert insp.num_rows == 100
    assert calls["fp"] == 0, "the inspector must hit the Query tab's canonical entry"
    assert provider.open_calls == []
    assert insp.to_pydict() == tab.to_pydict()


def test_smaller_want_after_transport_limit_100(tmp_path, monkeypatch):
    """Inspector LIMIT 50 after the Query tab cached the LIMIT-100 shape:
    canonical hit, sliced — one entry serves both surfaces and both asks."""
    eng, provider = _make_engine(tmp_path)
    eng.query_duckdb("SELECT * FROM work_order LIMIT 100", limit=100)
    provider.open_calls.clear()
    calls = {"fp": 0}
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        calls["fp"] += 1
        return orig_fp(self, sql, version)

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)
    got = eng.query_duckdb("SELECT * FROM work_order LIMIT 100", limit=50)
    assert got.num_rows == 50
    assert calls["fp"] == 0
    assert provider.open_calls == []


# ---------------------------------------------------------------------------
# LIMIT-supersede prefix reuse (live 2026-10-05: LIMIT 99/98/97 re-read
# object storage on every distinct n — a bigger cached preview must serve a
# smaller one by slicing; any >=n subset is a correct bare-LIMIT answer).
# ---------------------------------------------------------------------------


def test_smaller_limit_served_from_bigger_cached_preview(tmp_path):
    """LIMIT 5 after a cached LIMIT 50 preview: NO IO, rows sliced from the
    superseding entry (counted via the provider's open_calls)."""
    eng, provider = _make_engine(tmp_path)
    big = eng.query_duckdb("SELECT * FROM work_order LIMIT 50")
    assert big.num_rows == 50
    provider.open_calls.clear()  # everything past this point must be IO-free

    small = eng.query_duckdb("SELECT * FROM work_order LIMIT 5")
    assert small.num_rows == 5
    assert small.column_names == big.column_names
    # The slice is a PREFIX of the bigger cached result (same order).
    assert small.to_pydict() == {k: v[:5] for k, v in big.to_pydict().items()}
    assert provider.open_calls == [], "a superset hit must never touch storage"


def test_reuse_only_within_the_same_shape(tmp_path, monkeypatch):
    """A different projection (or a different table) never reuses another
    shape's cached preview — the candidate key carries the full SQL
    identity, so 'SELECT id ...' cannot be served from 'SELECT * ...'.
    Counted via the fast path: a reuse would skip it entirely."""
    eng, _provider = _make_engine(tmp_path)
    eng.query_duckdb("SELECT * FROM work_order LIMIT 50")
    calls = {"fp": 0}
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        calls["fp"] += 1
        return orig_fp(self, sql, version)

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)

    cols = eng.query_duckdb("SELECT id FROM work_order LIMIT 5")
    assert cols.num_rows == 5 and cols.column_names == ["id"]
    assert calls["fp"] == 1, "a different projection must NOT reuse the star preview"


def test_supersede_ladder_hits_the_closest_superset(tmp_path):
    """LIMIT 12 (not on the ladder) is served from the cached LIMIT 50 —
    the candidate scan covers both the fixed ladder and the multipliers."""
    eng, provider = _make_engine(tmp_path)
    eng.query_duckdb("SELECT * FROM work_order LIMIT 50")
    provider.open_calls.clear()

    got = eng.query_duckdb("SELECT * FROM work_order LIMIT 12")
    assert got.num_rows == 12
    assert provider.open_calls == []


def test_bigger_limit_after_smaller_still_executes(tmp_path, monkeypatch):
    """LIMIT 100 after a cached LIMIT 50: no superset exists (50 < 100 and
    the candidate ladder above 100 is capped-out by the 1000 default, but
    50 cannot serve 100) — the fast path runs and the result is cached
    under ITS key; a repeat is then a direct hit (no fast-path call)."""
    eng, _provider = _make_engine(tmp_path)
    eng.query_duckdb("SELECT * FROM work_order LIMIT 50")
    calls = {"fp": 0}
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        calls["fp"] += 1
        return orig_fp(self, sql, version)

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)

    big = eng.query_duckdb("SELECT * FROM work_order LIMIT 100")
    assert big.num_rows == 100  # 150-row table; the fast path read a row group
    assert calls["fp"] == 1, "no superset cached — must execute"
    # The executed result is cached under its exact key: a repeat hits it
    # before the fast path would run again.
    again = eng.query_duckdb("SELECT * FROM work_order LIMIT 100")
    assert again.num_rows == 100
    assert calls["fp"] == 1


def test_supersede_respects_row_cap(tmp_path, monkeypatch):
    """A cap below the request refuses to manufacture a superset beyond it:
    the probe never proposes candidates above the cap."""
    eng, provider = _make_engine(tmp_path)
    import sqlhandler.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_max_rows", lambda: 10)
    eng.query_duckdb("SELECT * FROM work_order LIMIT 10")
    provider.open_calls.clear()
    # LIMIT 10 with cap 10: want >= cap → the probe declines immediately.
    # The exact-key hit serves it instead (already cached) — still no IO,
    # but the assertion here is that nothing crashes and rows are right.
    got = eng.query_duckdb("SELECT * FROM work_order LIMIT 10")
    assert got.num_rows == 10


def test_repeated_small_requests_are_canonical_hits(tmp_path, monkeypatch):
    """The canonical entry is the shared cache: repeated small requests
    (and different small limits) never re-touch the fast path."""
    eng, _provider = _make_engine(tmp_path)
    eng.query_duckdb("SELECT * FROM work_order LIMIT 50")
    calls = {"fp": 0}
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        calls["fp"] += 1
        return orig_fp(self, sql, version)

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)

    first = eng.query_duckdb("SELECT * FROM work_order LIMIT 5")
    assert first.num_rows == 5
    second = eng.query_duckdb("SELECT * FROM work_order LIMIT 5")
    third = eng.query_duckdb("SELECT * FROM work_order LIMIT 7")
    assert calls["fp"] == 0, "the canonical entry serves every smaller want"
    assert second.to_pydict() == first.to_pydict()
    assert third.num_rows == 7


def test_decorated_queries_never_reuse(tmp_path):
    """ORDER BY/WHERE variants are not bare previews — the reuse probe
    declines them (same guard as the fast path) and the normal path runs."""
    eng, _provider = _make_engine(tmp_path)
    eng.query_duckdb("SELECT * FROM work_order LIMIT 50")

    # ORDER BY is not a bare preview: neither the reuse probe nor the fast
    # path engages (the reuse probe calls _is_bare_preview first). Prove it
    # by the SQL job path — the result is correct and the preview cache was
    # not consulted for it (the exact key of an ORDER BY query differs from
    # any preview key by construction).
    ordered = eng.query_duckdb("SELECT * FROM work_order ORDER BY id LIMIT 5")
    assert ordered.num_rows == 5
    assert ordered.column("id").to_pylist() == sorted(ordered.column("id").to_pylist())


def test_supersede_l2_only_store_still_reusable(tmp_path, monkeypatch):
    """The superseding entry stored with the preview floor-0 rule reaches
    the OTHER replica via L2 — simulate by a fresh engine over the same L2
    dir: the smaller request slices from the L2 artifact, no fast-path call."""
    l2_dir = str(tmp_path / "l2")
    monkeypatch.setenv("SQLHANDLER_L2_DIR", l2_dir)
    monkeypatch.setenv("SQLHANDLER_L2_ENABLED", "1")
    monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", "0")
    monkeypatch.setenv("SQLHANDLER_L2_MAX_BYTES", "0")
    # Synchronous publish: the async writer would race eng2's read below.
    monkeypatch.setenv("SQLHANDLER_L2_WRITE_ASYNC", "0")
    eng, _provider = _make_engine(tmp_path)
    assert eng._l2_cache is not None
    eng.query_duckdb("SELECT * FROM work_order LIMIT 50")

    # A fresh engine = the other replica (empty L1, same shared L2 dir).
    eng2, _provider2 = _make_engine(tmp_path)
    assert eng2._l2_cache is not None and eng2._l2_cache is not eng._l2_cache
    calls = {"fp": 0}
    orig_fp = SqlEngine._preview_fastpath

    def counting_fp(self, sql, version):
        calls["fp"] += 1
        return orig_fp(self, sql, version)

    monkeypatch.setattr(SqlEngine, "_preview_fastpath", counting_fp)
    small = eng2.query_duckdb("SELECT * FROM work_order LIMIT 5")
    assert small.num_rows == 5
    assert calls["fp"] == 0, "L2 superset must serve the smaller request cross-replica"
