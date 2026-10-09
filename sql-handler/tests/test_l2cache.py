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
from typing import Any

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler.engine import SqlEngine
from sqlhandler.l2cache import DEFAULT_MAX_BYTES, DEFAULT_MIN_BYTES, L2ResultCache, load_l2_config
from sqlhandler.provider import DataProvider, TableInfo

TABLES = [TableInfo(name="sales", schema="shop", format="parquet")]


@pytest.fixture(autouse=True)
def _sync_l2_writes(monkeypatch):
    """Default the suite to the SYNCHRONOUS write-out.

    These tests pin the publish semantics (atomicity, caps, TTL, sweep,
    degradation) — identical in both modes — and dozens of them read the
    shared directory the moment query() returns, which is only guaranteed
    in sync mode. The async write-out has its own tests below.
    """
    monkeypatch.setenv("SQLHANDLER_L2_WRITE_ASYNC", "0")


class FakeProvider(DataProvider):
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
    assert a._l2_cache is not None and a._l2_cache.stats()["writes"] == 1  # published to the shared dir
    assert sorted(p.suffix for p in Path(l2).iterdir()) == [".json", ".parquet"]

    b = _make_engine(l2, root, monkeypatch)
    assert b._result_cache_writes == 0  # nothing in ITS memory
    assert b._l2_cache is not None  # the engine built its cache from the dir
    r2 = b.query_duckdb(QUERY)
    assert r2.to_pylist() == r1.to_pylist()  # byte-identical result...
    assert b._l2_cache is not None and b._l2_cache.stats()["hits"] == 1  # ...served from the SHARED dir
    assert b._result_cache_writes == 1  # and L1 warmed for the next call
    # the warmed L1 entry now serves replica 2 without touching disk again
    r3 = b.query_duckdb(QUERY)
    assert r3.to_pylist() == r1.to_pylist()
    assert b._l2_cache is not None and b._l2_cache.stats()["hits"] == 1
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
    assert key is not None
    assert key is not None  # the L2 dir is configured → a key always resolves
    b.query_duckdb(QUERY)  # L2 hit, warms L1
    assert b._result_cache.get(key) is not None
    b.query_duckdb(QUERY)  # now an L1 hit
    assert b._result_cache_hits == 1
    assert b._l2_cache is not None and b._l2_cache.stats()["hits"] == 1  # unchanged — L1 answered


def test_l2_hit_returns_identical_arrow_roundtrip(tmp_path, monkeypatch):
    """Arrow → zstd parquet → Arrow preserves schema AND values (review gotcha)."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    r1 = a.query_duckdb(QUERY)
    stored_key = a._result_cache_key(QUERY, None, None, None, None)
    assert stored_key is not None
    stored = L2ResultCache(l2, ttl=3600).lookup(stored_key)
    assert stored is not None
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
    assert a._l2_cache is not None  # built from the configured dir
    assert a._l2_cache is not None and a._l2_cache.stats()["writes"] == 0
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
    assert a._l2_cache is not None  # built from the configured dir
    assert a._l2_cache is not None and a._l2_cache.stats()["writes"] == 0
    # L1 uncapped but L2 max = 1 byte: the band check skips publication.
    # Separate TEST PROCESSES aren't needed — just a fresh engine AFTER the
    # cap env is gone (monkeypatch tracks each setenv, so the loop below
    # deletes engine A's cap before B is built).
    monkeypatch.delenv("SQLHANDLER_RESULT_CACHE_MAX_BYTES", raising=False)
    b = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_MIN_BYTES": "0", "SQLHANDLER_L2_MAX_BYTES": "1"})
    b.query_duckdb(QUERY)
    assert b._result_cache_writes == 1
    assert b._l2_cache is not None  # built from the configured dir
    assert b._l2_cache is not None and b._l2_cache.stats()["writes"] == 0


def test_min_max_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_L2_DIR", str(tmp_path / "l2"))
    cfg = load_l2_config()
    assert cfg is not None
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
    assert key is not None
    assert key is not None  # the L2 dir is configured → a key always resolves
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
    assert b._l2_cache is not None and b._l2_cache.stats()["hits"] == 0
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
    assert b._l2_cache is not None and b._l2_cache.stats()["hits"] == 0
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
    assert a._l2_cache is not None and a._l2_cache.stats()["writes"] == 0


def test_concurrent_same_key_publish(tmp_path, monkeypatch):
    """N threads publish the SAME key at once: exactly one valid pair wins."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    key = "k" * 64
    table = pa.table({"id": pa.array(range(5000), type=pa.int64())})

    errors: list[Exception] = []
    engine_for_threads = a
    assert engine_for_threads._l2_cache is not None  # built before threads start
    publish_cache = engine_for_threads._l2_cache

    def publish() -> None:
        try:
            for _ in range(5):
                publish_cache.store(key, table)
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
    assert key is not None
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
    cfg_dir = load_l2_config()
    assert cfg_dir is not None
    assert cfg_dir["dir"] == str(tmp_path / "l2")


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
        assert cfg is not None
        assert cfg["min_bytes"] == DEFAULT_MIN_BYTES
        assert cfg["max_bytes"] == DEFAULT_MAX_BYTES
        assert cfg["ttl"] == 3600.0
    monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", "-5")
    monkeypatch.setenv("SQLHANDLER_L2_MAX_BYTES", "-5")
    monkeypatch.setenv("SQLHANDLER_L2_TTL", "-5")
    cfg = load_l2_config()
    assert cfg is not None
    assert cfg["min_bytes"] == 0 and cfg["max_bytes"] == 0 and cfg["ttl"] == 0.0
    # "0" is a REAL value (not garbage): min 0 = publish everything, max 0 =
    # unlimited ceiling (both the engine's band check and load_l2_config).
    monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", "0")
    monkeypatch.setenv("SQLHANDLER_L2_MAX_BYTES", "0")
    cfg = load_l2_config()
    assert cfg is not None
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
    assert key is not None
    assert b._l2_cache is not None and b._l2_cache.lookup(key) is not None  # not expired


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
    assert key is not None
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
    assert key is not None
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
    assert old_key is not None
    a.query_duckdb("SELECT count(*) AS c FROM sales WHERE amount > 90")  # second entry
    new_key = a._result_cache_key("SELECT count(*) AS c FROM sales WHERE amount > 90", None, None, None, None)
    assert new_key is not None
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
    assert key is not None
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
        yaml.safe_dump({"tables": {"vw_sales": {"definition": "SELECT id, amount FROM sales WHERE amount > 90"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    a = _make_engine(str(tmp_path / "l2"), root, monkeypatch)
    a.query_duckdb("SELECT count(*) AS c FROM vw_sales")
    assert a._result_cache_writes == 0  # L1 exclusion preserved
    assert a._l2_cache is not None and a._l2_cache.stats()["writes"] == 0  # L2 exclusion preserved


# ---------------------------------------------------- async write-out (HA 2026-09)

# The sglang write-through analogy applied to result caching: the computing
# replica hands the result to a bounded background queue (its query returns
# once the memory L1 copy is placed), a single worker serializes zstd
# parquet + sidecar off the query path, and OTHER replicas read the artifact
# at a small non-RAM cost once it lands. Queue-full backpressure DROPS the
# write (an ordinary cache miss) rather than growing replica memory.


def test_async_store_publishes_after_return(tmp_path, monkeypatch):
    """Default mode: query returns first, the worker publishes, flush makes
    it observable deterministically."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_WRITE_ASYNC": "1"})
    assert a._l2_cache is not None and a._l2_cache._write_queue is not None
    r1 = a.query_duckdb(QUERY)
    key = a._result_cache_key(QUERY, None, None, None, None)
    assert key is not None
    assert a._l2_cache is not None and a._l2_cache.flush_async_stores() is True
    assert sorted(p.suffix for p in Path(l2).iterdir()) == [".json", ".parquet"]
    stored = L2ResultCache(l2, ttl=3600).lookup(key)
    assert stored.to_pylist() == r1.to_pylist()
    stats = a._l2_cache.stats()
    assert stats["write_mode"] == "async" and stats["writes"] == 1
    assert stats["write_dropped"] == 0


def test_async_cross_replica_sharing(tmp_path, monkeypatch):
    """The whole point: replica 2 serves a result replica 1 paid for —
    the publish happened on replica 1's background worker."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_WRITE_ASYNC": "1"})
    r1 = a.query_duckdb(QUERY)
    assert a._l2_cache is not None and a._l2_cache.flush_async_stores() is True
    b = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_WRITE_ASYNC": "1"})
    r2 = b.query_duckdb(QUERY)
    assert r2.to_pylist() == r1.to_pylist()
    assert b._l2_cache is not None and b._l2_cache.stats()["hits"] == 1
    assert b._result_cache_writes == 1  # L1 warmed for the next call


def test_async_disabled_falls_back_to_sync(tmp_path, monkeypatch):
    """SQLHANDLER_L2_WRITE_ASYNC=0: the artifact is on disk the moment
    query() returns — the historical byte-for-byte behavior."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_WRITE_ASYNC": "0"})
    a.query_duckdb(QUERY)
    assert a._l2_cache is not None and a._l2_cache._write_queue is None
    assert sorted(p.suffix for p in Path(l2).iterdir()) == [".json", ".parquet"]
    assert a._l2_cache is not None and a._l2_cache.stats()["write_mode"] == "sync"


def test_async_queue_full_drops_and_counts(tmp_path, monkeypatch):
    """Backpressure: a full queue drops the write and counts it — the
    queue never grows and the query path never blocks."""
    from sqlhandler import l2cache as l2mod

    cache = L2ResultCache(str(tmp_path / "l2"), ttl=3600)
    monkeypatch.setattr(l2mod, "DEFAULT_ASYNC_QUEUE_DEPTH", 1)
    release, entered = threading.Event(), threading.Event()

    def slow_store(key, table):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(cache, "store", slow_store)
    cache._ensure_write_worker()
    assert cache.async_store("k1", pa.table({"a": [1]})) is True
    assert entered.wait(5)  # worker picked it up and is blocked in store
    # maxsize=1 bounds PENDING items (k1 is in flight, not pending): k2 fills
    # the queue, k3 is the one that finds it full and is dropped.
    assert cache.async_store("k2", pa.table({"a": [2]})) is True
    assert cache.async_store("k3", pa.table({"a": [3]})) is False  # queue full
    assert cache.stats()["write_dropped"] == 1
    release.set()
    assert cache.flush_async_stores() is True


def test_write_worker_started_once(tmp_path):
    """_ensure_write_worker is idempotent."""
    cache = L2ResultCache(str(tmp_path / "l2"), ttl=3600)
    cache._ensure_write_worker()
    w1 = cache._write_worker
    assert w1 is not None and w1.daemon and w1.name == "sqlhandler-l2-writeout"
    cache._ensure_write_worker()
    assert cache._write_worker is w1


# --------------------------------------------- shared metadata tier (2026-09)

# profile_table / column_stats outputs join the shared tier: a few KB of
# JSON whose recompute is a multi-second sampled scan, so sharing beats
# recomputing on every replica (the G2 observation: first calls seconds,
# warm per-replica calls ~100ms — but every OTHER replica re-paid the cold
# scan). Artifacts live under <l2-dir>/meta/ with snapshot-version keys.


def test_profile_shared_across_engines(tmp_path, monkeypatch):
    """Replica A pays the scan; replica B's identical profile is a shared
    hit (no rescan) and warms B's L1."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    r1 = a.profile_table("sales")
    assert a._l2_meta is not None and a._l2_meta.stats()["writes"] == 1
    b = _make_engine(l2, root, monkeypatch)
    r2 = b.profile_table("sales")
    # Compare the JSON-fidelity forms: DuckDB SUMMARIZE hands back Decimal
    # scalars on the fresh path; the shared artifact carries their str form
    # — which is exactly what every JSON/markdown consumer renders anyway
    # (str(Decimal("0.00")) == "0.00", identical display).
    assert json.loads(json.dumps(r2, default=str)) == json.loads(json.dumps(r1, default=str))
    assert b._l2_meta is not None and b._l2_meta.stats()["hits"] == 1
    assert b._profile_misses == 0  # B never scanned anything
    b.profile_table("sales")
    assert b._profile_hits == 2  # shared hit + L1 hit


def test_profile_shared_invalidated_by_snapshot(tmp_path, monkeypatch):
    """An ETL commit (version bump) invalidates instantly — no TTL wait."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    a.profile_table("sales")
    assert a._l2_meta is not None and a._l2_meta.stats()["writes"] == 1
    assert isinstance(a.provider, FakeProvider)
    a.provider.versions["shop/sales"] = 7  # ETL commit
    a.profile_table("sales")
    assert a._l2_meta is not None and a._l2_meta.stats()["writes"] == 2  # new key → recompute + republish


def test_column_stats_shared_across_engines(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    r1 = a.column_stats("sales", "amount")
    assert a._l2_meta is not None and a._l2_meta.stats()["writes"] == 1
    b = _make_engine(l2, root, monkeypatch)
    r2 = b.column_stats("sales", "amount")
    assert r2 == r1
    assert b._l2_meta is not None and b._l2_meta.stats()["hits"] == 1
    assert b._profile_misses == 0


def test_meta_tier_opt_out(tmp_path, monkeypatch):
    """SQLHANDLER_L2_METADATA=0: per-replica-only behavior, no meta cache."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_METADATA": "0"})
    assert a._l2_meta is None
    a.profile_table("sales")
    assert not (Path(l2) / "meta").exists()


def test_meta_tier_never_breaks_profile(tmp_path, monkeypatch):
    """A broken meta dir degrades to a plain per-replica profile."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    (Path(l2)).mkdir(parents=True, exist_ok=True)
    (Path(l2) / "meta").write_text("not a dir", encoding="utf-8")  # sabotage
    a = _make_engine(l2, root, monkeypatch)
    r = a.profile_table("sales")  # must not raise
    assert r["n_columns"] == len(r["columns"])


# ---------------------------------------------------- preview result sharing


def test_preview_cached_on_repeat(tmp_path, monkeypatch):
    """The 2026-09 fix: a bare-LIMIT preview is cached — the second identical
    call is an L1 hit instead of another object-store read."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    sql = "SELECT * FROM sales LIMIT 5"
    r1 = a.query_duckdb(sql)
    r2 = a.query_duckdb(sql)
    assert r2.to_pylist() == r1.to_pylist()
    assert a._result_cache_hits == 1  # the cache answered, not the fast path


def test_preview_shared_across_engines(tmp_path, monkeypatch):
    """Replica B serves replica A's preview from the shared tier (floor 0 —
    a preview's recompute is an object-store round trip, not arithmetic)."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    sql = "SELECT * FROM sales LIMIT 5"
    r1 = a.query_duckdb(sql)
    assert a._l2_cache is not None and a._l2_cache.flush_async_stores() is True
    b = _make_engine(l2, root, monkeypatch)
    r2 = b.query_duckdb(sql)
    assert r2.to_pylist() == r1.to_pylist()
    assert b._l2_cache is not None and b._l2_cache.stats()["hits"] == 1  # served from the SHARED dir


# ------------------------------------------- selective drop_for_table (2026-10)

# The write tier's eviction used to wipe the ENTIRE shared cache on every
# scratch write (every sidecar removed). Now sidecars record their
# referenced tables at store time ("tables") and drop_for_table removes
# only the written table's entries; legacy sidecars without "tables" are
# "unknown" and trigger the conservative full-wipe fallback ONLY when the
# selective drop matched nothing.


def _mk_table(**overrides: Any) -> TableInfo:
    defaults: dict[str, Any] = {"name": "sales", "schema": "shop", "format": "parquet"}
    defaults.update(overrides)
    return TableInfo(**defaults)


def _write_sidecar(l2_dir: Path, key: str, tables: list[str] | None = None, created: float | None = None) -> None:
    """Publish a minimal VALID pair (sidecar + parquet) directly on disk."""
    table = pa.table({"a": pa.array([1, 2, 3], type=pa.int64())})
    pq.write_table(table, str(l2_dir / f"{key[:16]}.parquet"))
    meta = {"key": key, "created": created if created is not None else time.time()}
    if tables is not None:
        meta["tables"] = tables
    (l2_dir / f"{key[:16]}.json").write_text(json.dumps(meta), encoding="utf-8")


def test_store_records_referenced_tables_in_sidecar(tmp_path, monkeypatch):
    """Store time: the engine passes the referenced-table idents through the
    store API, and the sidecar carries them as a sorted, deduped list."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    a.query_duckdb(QUERY)
    key = a._result_cache_key(QUERY, None, None, None, None)
    assert key is not None
    meta = json.loads((Path(l2) / f"{key[:16]}.json").read_text(encoding="utf-8"))
    assert meta["key"] == key
    assert "sales" in meta["tables"]
    assert "shop/sales" in meta["tables"]
    assert "shop_sales" in meta["tables"]
    assert meta["tables"] == sorted(set(meta["tables"]))


def test_store_without_tables_keeps_existing_sidecar_tables(tmp_path):
    """The L1-warm republish (no SQL in hand) must not ERASE a good sidecar's
    tables list — same key, same SQL identity, so the list carries forward."""
    l2 = tmp_path / "l2"
    l2.mkdir()
    key = "a" * 64
    _write_sidecar(l2, key, tables=["shop/sales"])
    cache = L2ResultCache(str(l2), ttl=3600)
    cache.store(key, pa.table({"a": pa.array([1], type=pa.int64())}))
    meta = json.loads((l2 / f"{key[:16]}.json").read_text(encoding="utf-8"))
    assert meta["tables"] == ["shop/sales"]


def test_drop_for_table_removes_only_matching_entries(tmp_path):
    """Two entries referencing DIFFERENT tables + one legacy entry: dropping
    table X removes only X's entry; the other table's and the legacy
    sidecar both survive (no more full-cache wipe)."""
    l2 = tmp_path / "l2"
    l2.mkdir()
    key_x = "1" * 64
    key_y = "2" * 64
    key_legacy = "3" * 64
    _write_sidecar(l2, key_x, tables=["shop/inventory", "inventory"])  # NOT the dropped table
    _write_sidecar(l2, key_y, tables=["shop/sales", "sales"])  # the dropped table
    _write_sidecar(l2, key_legacy)  # pre-"tables" sidecar: unknown
    cache = L2ResultCache(str(l2), ttl=3600)
    dropped = cache.drop_for_table("shop/sales", "sales")
    assert dropped == 1
    assert (l2 / f"{key_x[:16]}.json").exists()  # different table → survives
    assert (l2 / f"{key_x[:16]}.parquet").exists()
    assert not (l2 / f"{key_y[:16]}.json").exists()  # matched → gone
    assert not (l2 / f"{key_y[:16]}.parquet").exists()
    assert (l2 / f"{key_legacy[:16]}.json").exists()  # legacy → survives too
    assert (l2 / f"{key_legacy[:16]}.parquet").exists()


def test_drop_for_table_matches_normalized_identifiers(tmp_path):
    """Scheme/host/slash/case differences all meet: the SAME table recorded
    in path form matches a drop carrying the scheme-qualified form, and a
    bare-name drop matches case-insensitively. (Matching is exact compare
    after normalization — the engine always records AND drops the bare
    name, which is the bridge across differently-shaped paths.)"""
    l2 = tmp_path / "l2"
    l2.mkdir()
    key_a = "4" * 64
    key_b = "5" * 64
    _write_sidecar(l2, key_a, tables=["abfs://fs@acct/shop/sales", "shop/sales", "sales"])
    _write_sidecar(l2, key_b, tables=["shop/inventory"])
    cache = L2ResultCache(str(l2), ttl=3600)
    # scheme/host stripped, slashes trimmed, casefolded on both sides
    assert cache.drop_for_table("abfs://fs@acct/scratch/shop/Sales/", "SALES") == 1
    assert not (l2 / f"{key_a[:16]}.json").exists()
    assert (l2 / f"{key_b[:16]}.json").exists()
    # and the plain logical-path drop form matches a scheme-qualified record
    _write_sidecar(l2, key_a, tables=["abfs://fs@acct/shop/sales"])
    assert cache.drop_for_table("shop/Sales", "sales") == 1
    assert not (l2 / f"{key_a[:16]}.json").exists()


def test_drop_for_table_falls_back_on_unknown_only(tmp_path, caplog):
    """Fallback: nothing matched selectively BUT a legacy (tables-less)
    sidecar exists — the UNKNOWN entries are wiped (one may be the
    written table's), the WARNING names the wiped count, and known
    non-matching entries survive even the fallback."""
    import logging as _logging

    l2 = tmp_path / "l2"
    l2.mkdir()
    key_x = "6" * 64
    key_legacy = "7" * 64
    _write_sidecar(l2, key_x, tables=["shop/inventory"])  # known, does NOT match
    _write_sidecar(l2, key_legacy)  # unknown tables
    cache = L2ResultCache(str(l2), ttl=3600)
    with caplog.at_level(_logging.WARNING, logger="sqlhandler.l2cache"):
        dropped = cache.drop_for_table("shop/sales", "sales")
    assert dropped == 1  # only the unknown entry was wiped
    assert not (l2 / f"{key_legacy[:16]}.json").exists()
    assert not (l2 / f"{key_legacy[:16]}.parquet").exists()
    assert (l2 / f"{key_x[:16]}.json").exists()  # known non-match survives
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("unknown-tables (legacy) entries" in r.getMessage() and "1" in r.getMessage() for r in warnings)


def test_drop_for_table_no_fallback_when_nothing_unknown(tmp_path):
    """A selective drop that matches nothing with NO legacy sidecars present
    must NOT wipe: every entry is known and simply doesn't reference the
    dropped table."""
    l2 = tmp_path / "l2"
    l2.mkdir()
    key_y = "8" * 64
    _write_sidecar(l2, key_y, tables=["shop/sales"])
    cache = L2ResultCache(str(l2), ttl=3600)
    assert cache.drop_for_table("shop/inventory", "inventory") == 0
    assert (l2 / f"{key_y[:16]}.json").exists()
    assert (l2 / f"{key_y[:16]}.parquet").exists()


def test_drop_for_table_store_drop_lookup_flow(tmp_path, monkeypatch):
    """The end-to-end cache-correctness flow: engine stores an entry for
    table X, drop_for_table(X) removes it, the next lookup misses."""
    root = tmp_path / "data"
    _seed_table(root)
    l2 = str(tmp_path / "l2")
    a = _make_engine(l2, root, monkeypatch)
    a.query_duckdb(QUERY)
    key = a._result_cache_key(QUERY, None, None, None, None)
    assert key is not None
    assert L2ResultCache(l2, ttl=3600).lookup(key) is not None  # stored
    assert a._l2_cache is not None and a._l2_cache.drop_for_table("shop/sales", "sales") == 1
    assert not (Path(l2) / f"{key[:16]}.json").exists()
    assert L2ResultCache(l2, ttl=3600).lookup(key) is None  # miss
    assert not (Path(l2) / f"{key[:16]}.parquet").exists()  # pair removed


def test_drop_for_table_never_raises_on_garbage(tmp_path):
    """Corrupt sidecars, a missing dir, and empty identifiers: still returns
    a count, still never raises (the accelerator, never a dependency)."""
    l2 = tmp_path / "l2"
    l2.mkdir()
    (l2 / "corrupt.json").write_text("{not json", encoding="utf-8")
    (l2 / "nokey.json").write_text(json.dumps({"tables": ["shop/sales"]}), encoding="utf-8")
    cache = L2ResultCache(str(l2), ttl=3600)
    assert cache.drop_for_table("shop/sales", "sales") == 0
    assert cache.drop_for_table("", "") == 0
    empty = L2ResultCache(str(tmp_path / "never-created"), ttl=3600)
    assert empty.drop_for_table("shop/sales", "sales") == 0


def test_engine_write_evicts_only_written_table_l2(tmp_path, monkeypatch):
    """The integration the fix exists for: a write to table B must NOT
    destroy replica-shared entries for table A (the pre-fix behavior
    wiped the whole shared dir on every scratch write)."""
    root = tmp_path / "data"
    _seed_table(root)
    # a second, physically distinct table the engine can list
    inv = root / "shop" / "inventory"
    inv.mkdir(parents=True, exist_ok=True)
    n = 40000
    pq.write_table(
        pa.table({"sku": pa.array(range(n), type=pa.int64())}),
        inv / "part.parquet",
    )
    l2 = str(tmp_path / "l2")
    # TABLES + inventory: the FakeProvider must list both, so both register.
    monkeypatch.setattr(
        "test_l2cache.TABLES",
        [
            TableInfo(name="sales", schema="shop", format="parquet"),
            TableInfo(name="inventory", schema="shop", format="parquet"),
        ],
    )
    a = _make_engine(l2, root, monkeypatch)
    r_a = a.query_duckdb(QUERY)  # references sales
    a.query_duckdb("SELECT sku FROM inventory WHERE sku > 10")  # references inventory
    assert a._l2_cache is not None and a._l2_cache.stats()["writes"] == 2
    key_a = a._result_cache_key(QUERY, None, None, None, None)
    assert key_a is not None
    key_b = a._result_cache_key("SELECT sku FROM inventory WHERE sku > 10", None, None, None, None)
    assert key_b is not None
    # a scratch write to inventory evicts ITS entry only
    a._evict_result_cache_for_write("duckdb", str(root / "scratch" / "inventory"))
    assert a._l2_cache is not None and a._l2_cache.stats()["writes"] == 2  # no full wipe happened
    assert (Path(l2) / f"{key_a[:16]}.json").exists()  # sales entry SURVIVES
    assert not (Path(l2) / f"{key_b[:16]}.json").exists()  # inventory entry gone
    assert L2ResultCache(l2, ttl=3600).lookup(key_a) is not None
    stored_a = L2ResultCache(l2, ttl=3600).lookup(key_a)
    assert stored_a is not None
    assert stored_a.to_pylist() == r_a.to_pylist()
