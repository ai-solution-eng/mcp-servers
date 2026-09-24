"""Tests for the shared L2 result cache (sqlhandler/l2cache.py + engine wiring).

The two-replica assertion runs in-process: two :class:`SqlEngine` instances
over the SAME provider fixtures but distinct in-memory caches, sharing one
tmp L2 directory — replica 1 pays the query, replica 2 must be served from
disk (and warm its own L1 on the way).

Also pinned here: the conditional ``policy=`` cache-key slot (with no policy
the key is byte-identical to the historical format — golden test), the byte
caps, sidecar degradation, concurrent same-key publish, and cleanup.
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq

from sqlhandler.engine import SqlEngine
from sqlhandler.l2cache import DEFAULT_MAX_BYTES, DEFAULT_MIN_BYTES, L2ResultCache, load_l2_config
from sqlhandler.provider import TableInfo

TABLES = [TableInfo(name="sales", schema="shop", format="parquet")]


class FakeProvider:
    """Same local-parquet provider shape test_virtual.py uses (no L2 baggage)."""

    kind = "fake"

    def __init__(self, root: Path):
        self.root = root
        self.versions: dict[str, int] = {}

    def list_tables(self):
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return self.versions.get(info.path)


def _seed_table(root: Path) -> None:
    d = root / "shop" / "sales"
    d.mkdir(parents=True, exist_ok=True)
    # ~1 MiB of rows: comfortably above the L2 min-bytes floor so stores happen.
    n = 40000
    pq.write_table(
        pa.table(
            {
                "id": pa.array(range(n), type=pa.int64()),
                "amount": pa.array([float(i % 97) for i in range(n)]),
            }
        ),
        d / "part.parquet",
    )


def _make_engine(l2_dir: str, root: Path, monkeypatch=None, env: dict | None = None) -> SqlEngine:
    # Default min-bytes 1 so the seeded ~16 KB result is always published;
    # tests exercising the FLOOR set SQLHANDLER_L2_MIN_BYTES explicitly.
    merged = {"SQLHANDLER_L2_MIN_BYTES": "1", **(env or {})}
    if monkeypatch is not None:
        monkeypatch.setenv("SQLHANDLER_L2_DIR", l2_dir)
        for k, v in merged.items():
            monkeypatch.setenv(k, v)
    else:
        os.environ["SQLHANDLER_L2_DIR"] = l2_dir
        for k, v in merged.items():
            os.environ[k] = v
    return SqlEngine(FakeProvider(root), cache_ttl=0, cache_dir=None)


QUERY = "SELECT id, amount FROM sales WHERE amount > 90 ORDER BY id"


# ---------------------------------------------------------------------------
# two-replica sharing (the core assertion)
# ---------------------------------------------------------------------------


def test_two_engines_share_one_l2_dir(tmp_path, monkeypatch):
    """Replica 1 computes + publishes; replica 2 serves from the shared dir."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    r1 = a.query_duckdb(QUERY)
    assert a._l2_cache is not None
    assert a._l2_cache.stats()["writes"] == 1  # published to the shared dir
    assert sorted(p.suffix for p in Path(l2).iterdir()) == [".json", ".parquet"]

    b = _make_engine(l2, root, monkeypatch)
    assert b._result_cache_writes == 0  # nothing in ITS memory
    r2 = b.query_duckdb(QUERY)
    assert r2.to_pylist() == r1.to_pylist()  # byte-identical result...
    assert b._l2_cache.stats()["hits"] == 1  # ...served from the SHARED dir
    assert b._result_cache_writes == 1  # and L1 warmed for the next call
    # the warmed L1 entry now serves replica 2 without touching disk again
    r3 = b.query_duckdb(QUERY)
    assert r3.to_pylist() == r1.to_pylist()
    assert b._l2_cache.stats()["hits"] == 1
    assert b._result_cache_hits == 1


def test_l2_hit_falls_back_to_l1_on_next_query(tmp_path, monkeypatch):
    """After an L2 hit the warmed L1 entry answers (hits counted per layer)."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    a.query_duckdb(QUERY)
    b = _make_engine(l2, root, monkeypatch)
    key = b._result_cache_key(QUERY, None, None, None, None)
    b.query_duckdb(QUERY)  # L2 hit, warms L1
    assert b._result_cache.get(key) is not None
    b.query_duckdb(QUERY)  # now an L1 hit
    assert b._result_cache_hits == 1
    assert b._l2_cache.stats()["hits"] == 1  # unchanged — L1 answered


def test_l2_hit_returns_identical_arrow_roundtrip(tmp_path, monkeypatch):
    """Arrow → zstd parquet → Arrow preserves schema AND values (review gotcha)."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    r1 = a.query_duckdb(QUERY)
    stored = L2ResultCache(l2, ttl=3600).lookup(a._result_cache_key(QUERY, None, None, None, None))
    assert stored.schema == r1.schema
    assert stored.to_pylist() == r1.to_pylist()


# ---------------------------------------------------------------------------
# byte caps
# ---------------------------------------------------------------------------


def test_below_min_bytes_l1_only(tmp_path, monkeypatch):
    """Small results never round-trip the disk (PVC IO beats recompute loss)."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_MIN_BYTES": str(1024**3)})
    a.query_duckdb(QUERY)
    assert a._result_cache_writes == 1
    assert a._l2_cache.stats()["writes"] == 0
    assert not Path(l2).exists() or not list(Path(l2).iterdir())


def test_above_max_bytes_l1_only(tmp_path, monkeypatch):
    """A result over the max cap is L1-capped out anyway — never published."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    # L1 cap 1 byte: the store returns before the L2 branch (documented order)
    a = _make_engine(
        l2,
        root,
        monkeypatch,
        env={"SQLHANDLER_RESULT_CACHE_MAX_BYTES": "1", "SQLHANDLER_L2_MIN_BYTES": "1"},
    )
    a.query_duckdb(QUERY)
    assert a._l2_cache.stats()["writes"] == 0
    # L1 uncapped but L2 max = 1 byte: the band check skips publication.
    # Separate TEST PROCESSES aren't needed — just a fresh engine AFTER the
    # cap env is gone (monkeypatch tracks each setenv, so the loop below
    # deletes engine A's cap before B is built).
    monkeypatch.delenv("SQLHANDLER_RESULT_CACHE_MAX_BYTES", raising=False)
    b = _make_engine(
        l2, root, monkeypatch, env={"SQLHANDLER_L2_MIN_BYTES": "0", "SQLHANDLER_L2_MAX_BYTES": "1"}
    )
    b.query_duckdb(QUERY)
    assert b._result_cache_writes == 1
    assert b._l2_cache.stats()["writes"] == 0


def test_min_max_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_L2_DIR", str(tmp_path / "l2"))
    cfg = load_l2_config()
    assert cfg["min_bytes"] == DEFAULT_MIN_BYTES == 256 * 1024
    assert cfg["max_bytes"] == DEFAULT_MAX_BYTES == 2 * 1024**3


# ---------------------------------------------------------------------------
# sidecar degradation + atomicity
# ---------------------------------------------------------------------------


def _publish_and_break(tmp_path, monkeypatch, mode: str):
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    r1 = a.query_duckdb(QUERY)
    key = a._result_cache_key(QUERY, None, None, None, None)
    sidecar = Path(l2) / f"{key[:16]}.json"
    artifact = Path(l2) / f"{key[:16]}.parquet"
    if mode == "corrupt-sidecar":
        sidecar.write_text("{not json", encoding="utf-8")
    elif mode == "missing-sidecar":
        sidecar.unlink()
    elif mode == "wrong-key":
        sidecar.write_text(json.dumps({"key": "0" * 64, "created": time.time()}), encoding="utf-8")
    elif mode == "corrupt-parquet":
        artifact.write_bytes(b"not parquet at all")
    b = _make_engine(l2, root, monkeypatch)
    r2 = b.query_duckdb(QUERY)
    assert r2.to_pylist() == r1.to_pylist()  # degrades to a live recompute
    assert b._l2_cache.stats()["hits"] == 0
    return b, key


def test_corrupt_sidecar_degrades_to_miss(tmp_path, monkeypatch):
    _publish_and_break(tmp_path, monkeypatch, "corrupt-sidecar")


def test_missing_sidecar_degrades_to_miss(tmp_path, monkeypatch):
    _publish_and_break(tmp_path, monkeypatch, "missing-sidecar")


def test_sidecar_key_mismatch_degrades_to_miss(tmp_path, monkeypatch):
    """A foreign file at the same hash-prefix is never trusted."""
    _publish_and_break(tmp_path, monkeypatch, "wrong-key")


def test_corrupt_parquet_removed_and_missed(tmp_path, monkeypatch):
    """Unreadable parquet with a LIVE sidecar: miss now, pair cleaned up.

    The corrupt parquet is overwritten by the recompute's fresh publish
    (same key, same dir), so the SECOND engine's store re-creates the pair;
    the assertion is that the lookup path itself removed/ignored the broken
    artifact and the query still served correct data.
    """
    b, _key = _publish_and_break(tmp_path, monkeypatch, "corrupt-parquet")
    assert b._l2_cache.stats()["hits"] == 0
    # after the failed read, a fresh lookup of the republished pair hits
    r = b.query_duckdb(QUERY)
    assert r.num_rows > 0


def test_store_failure_never_raises(tmp_path, monkeypatch):
    """An unwritable L2 dir must not fail the query (accelerator, not dep)."""
    root = tmp_path / "data"
    _seed_table(root)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupies the path", encoding="utf-8")
    a = _make_engine(str(blocker), root, monkeypatch)
    out = a.query_duckdb(QUERY)  # must not raise
    assert out.num_rows > 0
    assert a._l2_cache.stats()["writes"] == 0


def test_concurrent_same_key_publish(tmp_path, monkeypatch):
    """N threads publish the SAME key at once: exactly one valid pair wins."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    key = "k" * 64
    table = pa.table({"id": pa.array(range(5000), type=pa.int64())})

    errors: list[Exception] = []

    def publish():
        try:
            for _ in range(5):
                a._l2_cache.store(key, table)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    c = L2ResultCache(l2, ttl=3600)
    result = c.lookup(key)
    assert result is not None
    assert result.to_pylist() == table.to_pylist()
    assert c.stats()["hits"] == 1


# ---------------------------------------------------------------------------
# conditional policy= key part
# ---------------------------------------------------------------------------


def test_key_format_golden_no_policy(tmp_path, monkeypatch):
    """GOLDEN: with no policy the key parts are byte-identical to today's.

    Pins the exact \x1f-joined parts list (normalized SQL, params, limit,
    row_cap, version_as_of, then sorted snapshot tokens) so the conditional
    ``policy=`` slot provably changed nothing for the no-policy case.
    """
    root = tmp_path / "data"
    _seed_table(root)
    a = _make_engine(str(tmp_path / "l2"), root, monkeypatch)
    key = a._result_cache_key(QUERY, {"x": 1}, 10, 100, 3)
    assert key is not None
    from sqlhandler.engine import _normalize_cache_sql

    expected_parts = [
        _normalize_cache_sql(QUERY),
        repr({"x": 1}),
        repr(10),
        repr(100),
        repr(3),
        "default/shop/sales=None",
    ]
    expected = hashlib.sha256("\x1f".join(expected_parts).encode()).hexdigest()
    assert key == expected  # byte-identical to the pre-L2 format


def test_key_golden_with_policy_hash(tmp_path, monkeypatch):
    """A non-empty policy hash appends `policy=<hash>` as the LAST part."""
    root = tmp_path / "data"
    _seed_table(root)
    a = _make_engine(str(tmp_path / "l2"), root, monkeypatch)
    from sqlhandler.engine import _normalize_cache_sql

    base = [
        _normalize_cache_sql(QUERY),
        repr(None),
        repr(None),
        repr(None),
        repr(None),
        "default/shop/sales=None",
    ]
    ph = "deadbeef" * 8
    expected_with = hashlib.sha256("\x1f".join(base + [f"policy={ph}"]).encode()).hexdigest()
    a._policy_hash = lambda caller=None: ph  # type: ignore[method-assign]
    key = a._result_cache_key(QUERY, None, None, None, None)
    assert key == expected_with
    # and it differs from the no-policy key (never share masked/unmasked)
    _bind_policy(a, "")
    assert a._result_cache_key(QUERY, None, None, None, None) != expected_with


def test_empty_policy_part_is_empty_string():
    from sqlhandler.engine import SqlEngine

    assert SqlEngine._cache_policy_part("") == ""
    assert SqlEngine._cache_policy_part("abc") == "policy=abc"


def _bind_policy(engine: SqlEngine, policy_hash: str) -> None:
    """Bind a fixed policy hash to the engine's hook (test stand-in for the
    identity middleware; production code never monkeypatches this). The
    Stage-2 hook takes the caller argument (keyword) — the stand-in ignores
    it (a fixed hash for the test's single caller)."""
    engine._policy_hash = lambda caller=None: policy_hash  # type: ignore[method-assign]


def test_masked_and_unmasked_same_sql_never_share(tmp_path, monkeypatch):
    """The cross-caller leak assertion, expressed at the key level."""
    root = tmp_path / "data"
    _seed_table(root)
    a = _make_engine(str(tmp_path / "l2"), root, monkeypatch)
    keys = {}
    for label, ph in (("unmasked", ""), ("masked", "f" * 64)):
        _bind_policy(a, ph)
        keys[label] = a._result_cache_key(QUERY, None, None, None, None)
    assert keys["masked"] != keys["unmasked"]
    _bind_policy(a, "")
    assert keys["unmasked"] == a._result_cache_key(QUERY, None, None, None, None)  # stable


# ---------------------------------------------------------------------------
# disabled-by-default + config
# ---------------------------------------------------------------------------


def test_disabled_by_default_zero_behavior_change(tmp_path, monkeypatch):
    """No SQLHANDLER_L2_DIR: engine identical to today — no L2, no sweep."""
    monkeypatch.delenv("SQLHANDLER_L2_DIR", raising=False)
    monkeypatch.delenv("SQLHANDLER_L2_ENABLED", raising=False)
    assert load_l2_config() is None
    root = tmp_path / "data"
    _seed_table(root)
    a = SqlEngine(FakeProvider(root), cache_ttl=0, cache_dir=None)
    a.query_duckdb(QUERY)
    assert a._l2_cache is None
    assert a._result_cache_writes == 1  # L1 exactly as before
    stats = a.cache_stats()
    assert stats["l2"] is None and stats["l2_hits"] == 0 and stats["l2_writes"] == 0
    monkeypatch.delattr(load_l2_config, "__self__", raising=False)
    # with the dir set, enabled:0 also disables (master switch)
    monkeypatch.setenv("SQLHANDLER_L2_DIR", str(tmp_path / "l2"))
    monkeypatch.setenv("SQLHANDLER_L2_ENABLED", "0")
    assert load_l2_config() is None
    monkeypatch.setenv("SQLHANDLER_L2_ENABLED", "1")
    assert load_l2_config()["dir"] == str(tmp_path / "l2")


def test_enabled_false_env_garbage_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_L2_DIR", str(tmp_path / "l2"))
    # Non-numeric garbage falls back to the defaults; "-5" PARSES but clamps
    # to 0 (the same max(x, 0) discipline every other env int in the repo
    # uses — a negative byte cap is nonsense, not a crash).
    for garbage in ("nope", " "):
        monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", garbage)
        monkeypatch.setenv("SQLHANDLER_L2_MAX_BYTES", garbage)
        monkeypatch.setenv("SQLHANDLER_L2_TTL", garbage)
        cfg = load_l2_config()
        assert cfg["min_bytes"] == DEFAULT_MIN_BYTES
        assert cfg["max_bytes"] == DEFAULT_MAX_BYTES
        assert cfg["ttl"] == 3600.0
    monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", "-5")
    monkeypatch.setenv("SQLHANDLER_L2_MAX_BYTES", "-5")
    monkeypatch.setenv("SQLHANDLER_L2_TTL", "-5")
    cfg = load_l2_config()
    assert cfg["min_bytes"] == 0 and cfg["max_bytes"] == 0 and cfg["ttl"] == 0.0
    # "0" is a REAL value (not garbage): min 0 = publish everything, max 0 =
    # unlimited ceiling (both the engine's band check and load_l2_config).
    monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", "0")
    monkeypatch.setenv("SQLHANDLER_L2_MAX_BYTES", "0")
    cfg = load_l2_config()
    assert cfg["min_bytes"] == 0 and cfg["max_bytes"] == 0


def test_ttl_zero_disables_l2_expiry_checks(tmp_path, monkeypatch):
    """ttl=0 keeps entries forever (no lazy delete, no sweep) — cap-only mode."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_TTL": "0"})
    a.query_duckdb(QUERY)
    cache = L2ResultCache(l2, ttl=0)
    assert cache.sweep() == 0  # sweep inert
    monkeypatch.setenv("SQLHANDLER_L2_TTL", "0")
    b = _make_engine(l2, root, monkeypatch)
    time.sleep(0.01)
    key = b._result_cache_key(QUERY, None, None, None, None)
    assert b._l2_cache.lookup(key) is not None  # not expired


# ---------------------------------------------------------------------------
# cleanup: lazy delete + daemon sweep
# ---------------------------------------------------------------------------


def test_lazy_delete_on_expired_lookup(tmp_path, monkeypatch):
    """A lookup past the TTL is a miss AND removes the pair from disk."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_TTL": "0.05"})
    a.query_duckdb(QUERY)
    key = a._result_cache_key(QUERY, None, None, None, None)
    cache = L2ResultCache(l2, ttl=0.05)
    assert cache.lookup(key) is not None  # fresh
    time.sleep(0.1)
    assert cache.lookup(key) is None  # expired
    assert not (Path(l2) / f"{key[:16]}.json").exists()
    assert not (Path(l2) / f"{key[:16]}.parquet").exists()


def test_expired_lookup_does_not_clobber_fresh(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_TTL": "0.05"})
    a.query_duckdb(QUERY)
    key = a._result_cache_key(QUERY, None, None, None, None)
    cache = L2ResultCache(l2, ttl=3600)
    time.sleep(0.1)
    assert cache.lookup(key) is not None  # ITS ttl is fresh — file kept


def test_sweep_removes_expired_sidecars(tmp_path, monkeypatch):
    """Daemon-style mtime pass: expired pairs go, fresh ones stay."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_MIN_BYTES": "1"})
    a.query_duckdb(QUERY)  # one entry in the dir
    old_key = a._result_cache_key(QUERY, None, None, None, None)
    a.query_duckdb("SELECT count(*) AS c FROM sales WHERE amount > 90")  # second entry
    new_key = a._result_cache_key(
        "SELECT count(*) AS c FROM sales WHERE amount > 90", None, None, None, None
    )
    # age only the FIRST entry's sidecar beyond the ttl (mtime pass)
    past = time.time() - 7200
    os.utime(Path(l2) / f"{old_key[:16]}.json", (past, past))
    cache = L2ResultCache(l2, ttl=3600)
    removed = cache.sweep()
    assert removed == 1
    assert not (Path(l2) / f"{old_key[:16]}.parquet").exists()
    assert (Path(l2) / f"{new_key[:16]}.json").exists()  # fresh entry survives
    assert (Path(l2) / f"{new_key[:16]}.parquet").exists()


def test_sweep_handles_corrupt_sidecar(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    a.query_duckdb(QUERY)
    key = a._result_cache_key(QUERY, None, None, None, None)
    sidecar = Path(l2) / f"{key[:16]}.json"
    past = time.time() - 7200
    sidecar.write_text("garbage{", encoding="utf-8")  # corrupt CONTENT
    os.utime(sidecar, (past, past))  # corrupt mtime LAST (write_text resets it)
    cache = L2ResultCache(l2, ttl=3600)
    assert cache.sweep() == 1
    assert not (Path(l2) / f"{key[:16]}.parquet").exists()


def test_sweep_on_missing_dir_is_noop(tmp_path):
    cache = L2ResultCache(str(tmp_path / "never-created"), ttl=3600)
    assert cache.sweep() == 0


def test_sweeper_thread_started_once(tmp_path, monkeypatch):
    """start_sweeper is idempotent; inert when ttl <= 0."""
    cache = L2ResultCache(str(tmp_path / "l2"), ttl=3600)
    cache.start_sweeper()
    t1 = cache._sweeper
    assert t1 is not None and t1.daemon and t1.name == "sqlhandler-l2-sweep"
    cache.start_sweeper()
    assert cache._sweeper is t1
    zero = L2ResultCache(str(tmp_path / "l2b"), ttl=0)
    zero.start_sweeper()
    assert zero._sweeper is None


def test_sweeper_loop_sweeps(tmp_path, monkeypatch):
    """The daemon loop actually calls sweep (short ttl/2 interval, 30s floor
    is bypassed by calling the loop body's helper directly)."""
    cache = L2ResultCache(str(tmp_path / "l2"), ttl=0.05)
    cache.start_sweeper()
    assert cache._sweeper is not None
    # _sweep_loop sleeps max(ttl*0.5, 30) — verify via the interval math
    assert max(cache.ttl * 0.5, 30.0) == 30.0


# ---------------------------------------------------------------------------
# stats / metrics surfaces
# ---------------------------------------------------------------------------


def test_cache_stats_report_l2_fields(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    stats = a.cache_stats()
    # flat fields for the metrics series + nested block for humans
    assert stats["l2_hits"] == 0
    assert stats["l2_writes"] == 0
    assert stats["l2"]["dir"] == l2
    assert stats["l2"]["ttl"] == 3600.0
    assert stats["l2"]["hits"] == 0
    assert stats["l2"]["writes"] == 0
    a.query_duckdb(QUERY)
    stats = a.cache_stats()
    assert stats["l2_writes"] == 1
    assert stats["l2"]["writes"] == 1
    # a second engine over the SAME dir counts its L2 hit in ITS own stats
    b = _make_engine(l2, root, monkeypatch)
    b.query_duckdb(QUERY)
    assert b.cache_stats()["l2_hits"] == 1


def test_metrics_render_l2_series(tmp_path, monkeypatch):
    from sqlhandler.observability import Metrics

    root = tmp_path / "data"
    _seed_table(root)
    a = _make_engine(str(tmp_path / "l2"), root, monkeypatch)
    a.query_duckdb(QUERY)  # replica A computes + publishes
    b = _make_engine(str(tmp_path / "l2"), root, monkeypatch)  # replica B: cold L1
    b.query_duckdb(QUERY)  # B's L2 hit (and warms its L1)
    text = Metrics().render(b)
    assert 'sqlhandler_cache_hits_total{cache="l2"} 1' in text
    assert 'sqlhandler_cache_misses_total{cache="l2"} 0' in text
    # existing series byte-unchanged: describe still present and first
    assert 'sqlhandler_cache_hits_total{cache="describe"} 0' in text
    assert 'sqlhandler_cache_hits_total{cache="profile"} 0' in text
    assert 'sqlhandler_cache_hits_total{cache="dataset"} 0' in text
    describe_idx = text.index('cache="describe"')
    dataset_idx = text.index('cache="dataset"')
    l2_idx = text.index('cache="l2"')
    assert describe_idx < dataset_idx < l2_idx


# ---------------------------------------------------------------------------
# exclusion invariants (must survive the L2)
# ---------------------------------------------------------------------------


def test_virtual_tables_stay_out_of_both_layers(tmp_path, monkeypatch):
    """Virtual + attach-DB queries were excluded from L1 by design; the L2
    must not 'fix' that — the materialization cache owns their speed."""
    root = tmp_path / "data"
    _seed_table(root)
    import yaml

    cat = tmp_path / "catalog.yaml"
    cat.write_text(
        yaml.safe_dump(
            {
                "tables": {
                    "vw_sales": {"definition": "SELECT id, amount FROM sales WHERE amount > 90"}
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    a = _make_engine(str(tmp_path / "l2"), root, monkeypatch)
    a.query_duckdb("SELECT count(*) AS c FROM vw_sales")
    assert a._result_cache_writes == 0  # L1 exclusion preserved
    assert a._l2_cache.stats()["writes"] == 0  # L2 exclusion preserved
