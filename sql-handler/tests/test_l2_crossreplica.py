"""Cross-replica L2 result-cache tests (the shared-disk tier).

The memory L1 result cache is PER REPLICA: with N replicas behind one
Service a repeated query lands on a different pod ~(N-1)/N of the time and
pays the cold path again. sqlhandler/l2cache.py publishes each result to a
directory every replica can read, which is the fix -- but only if three
things hold, and this file pins all three:

1. A second replica really is served from the shared dir -- proven with a
   provider OPEN COUNTER that must stay flat: engine B never opens the
   dataset, so it did not recompute.
2. A hit does not re-publish what it just read. The original engine code
   called _result_cache_store on an L2 hit, which ran the FULL store --
   including the L2 write. Measured on a 2 MB result: the hit cost 9.5 ms of
   which ~7.3 ms was the duplicate zstd encode + PVC write, i.e. ~74% of the
   hit latency was spent rewriting the artifact it had just read. In async
   mode (the deployment configuration) every hit also consumed a
   write-queue slot. These tests assert the artifact mtime is UNCHANGED and
   the write counter does not move, so a future refactor cannot silently
   reintroduce it.
3. The version-check cost is visible. While
   SQLHANDLER_CACHE_KEY_VERSION_THROTTLE is off (the ratified default) a
   cache-key build pays one provider metadata round-trip per referenced
   table -- INCLUDING on cache hits. The default is deliberately NOT changed
   here (three ratified tests pin immediate invalidation); instead the cost is
   counted, and this file pins the counter.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq

from sqlhandler.engine import SqlEngine
from sqlhandler.provider import DataProvider, TableInfo

TABLES = [TableInfo(name="sales", schema="shop", format="parquet")]

QUERY = "SELECT id, amount FROM sales WHERE amount > 90 ORDER BY id"


class CountingProvider(DataProvider):
    """Local-parquet provider that COUNTS the work a cache hit must avoid.

    opens is the decisive number: an L2 hit that recomputed would have to
    open the dataset. version_checks is the per-query metadata cost BUG B
    makes visible.
    """

    kind = "counting"

    def __init__(self, root: Path):
        self.root = root
        self.versions: dict[str, int] = {}
        self.opens = 0
        self.version_checks = 0

    def list_tables(self):
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        self.opens += 1
        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        self.version_checks += 1
        return self.versions.get(info.path)


def _seed(root: Path, rows: int = 40000) -> None:
    d = root / "shop" / "sales"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": pa.array(range(rows), type=pa.int64()),
                "amount": pa.array([float(i % 97) for i in range(rows)]),
            }
        ),
        d / "part.parquet",
    )


def _engine(l2_dir, root: Path, monkeypatch, env=None) -> SqlEngine:
    """One engine == one replica.

    SQLHANDLER_L2_MIN_BYTES=1 lowers the sharing floor: the default 256 KiB
    floor is an economic decision (a small result round-trips the volume
    slower than recomputing it), and these tests are about the SHARING
    MECHANISM, not the floor. The floor itself is pinned in test_l2cache.py.
    """
    monkeypatch.setenv("SQLHANDLER_L2_WRITE_ASYNC", "0")
    monkeypatch.setenv("SQLHANDLER_L2_MIN_BYTES", "1")
    if l2_dir is not None:
        monkeypatch.setenv("SQLHANDLER_L2_DIR", l2_dir)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return SqlEngine(CountingProvider(root), cache_ttl=0, cache_dir=None)


# ---------------------------------------------------------------------------
# 1. the cross-replica read (the whole point of the layer)
# ---------------------------------------------------------------------------


def test_second_replica_served_from_l2_without_opening_the_dataset(tmp_path, monkeypatch):
    """Replica A computes; replica B serves the SAME bytes and never opens.

    The open counter is the assertion that matters: identical rows alone
    would also be produced by recomputing. B opening the dataset ZERO times
    proves the result came off the shared directory.
    """
    root = tmp_path / "data"
    _seed(root)
    l2 = str(tmp_path / "l2")

    a = _engine(l2, root, monkeypatch)
    r1 = a.query_duckdb(QUERY)
    assert a.provider.opens == 1  # the cold path opened once
    assert a._l2_cache is not None
    assert a._l2_cache.stats()["writes"] == 1  # and published to the shared dir
    assert sorted(p.suffix for p in Path(l2).iterdir()) == [".json", ".parquet"]

    b = _engine(l2, root, monkeypatch)  # a DIFFERENT replica: its own L1
    r2 = b.query_duckdb(QUERY)
    assert r2.to_pylist() == r1.to_pylist()  # same answer...
    assert b.provider.opens == 0  # ...without opening the dataset: NO recompute
    assert b._l2_cache is not None
    assert b._l2_cache.stats()["hits"] == 1  # served from the shared dir
    assert b._result_cache_writes == 1  # and its L1 is warmed for next time

    # The warmed L1 now answers on this replica with no disk access at all.
    r3 = b.query_duckdb(QUERY)
    assert r3.to_pylist() == r1.to_pylist()
    assert b._l2_cache.stats()["hits"] == 1  # unchanged: L1 answered
    assert b._result_cache_hits == 1
    assert b.provider.opens == 0


def test_l2_hit_survives_a_third_replica(tmp_path, monkeypatch):
    """The shared entry serves EVERY later replica, not just the second."""
    root = tmp_path / "data"
    _seed(root)
    l2 = str(tmp_path / "l2")
    first = _engine(l2, root, monkeypatch)
    expected = first.query_duckdb(QUERY).to_pylist()
    for _ in range(3):
        replica = _engine(l2, root, monkeypatch)
        assert replica.query_duckdb(QUERY).to_pylist() == expected
        assert replica.provider.opens == 0
        assert replica._l2_cache is not None and replica._l2_cache.stats()["hits"] == 1


# ---------------------------------------------------------------------------
# 2. a hit must NOT re-publish the artifact it just read
# ---------------------------------------------------------------------------


def test_l2_hit_does_not_rewrite_the_shared_artifact(tmp_path, monkeypatch):
    """A pure cross-replica hit leaves the shared files byte-for-byte alone.

    The original code republished on every hit (full zstd encode + PVC write
    of data already on the PVC). mtime + the write counter both pin the fix.
    """
    root = tmp_path / "data"
    _seed(root)
    l2 = str(tmp_path / "l2")

    a = _engine(l2, root, monkeypatch)
    a.query_duckdb(QUERY)
    artifact = next(Path(l2).glob("*.parquet"))
    sidecar = next(Path(l2).glob("*.json"))
    art_before = (artifact.stat().st_mtime_ns, artifact.stat().st_size)
    side_before = (sidecar.stat().st_mtime_ns, sidecar.read_bytes())

    b = _engine(l2, root, monkeypatch)
    b.query_duckdb(QUERY)

    assert b._l2_cache is not None
    stats = b._l2_cache.stats()
    assert stats["hits"] == 1  # it WAS a hit...
    assert stats["writes"] == 0  # ...and it wrote NOTHING back
    assert stats["republishes"] == 0
    # mtime is the sharp assertion: a republish rewrites via os.replace, which
    # always produces a new inode with a new mtime.
    assert (artifact.stat().st_mtime_ns, artifact.stat().st_size) == art_before
    assert (sidecar.stat().st_mtime_ns, sidecar.read_bytes()) == side_before


def test_repeated_hits_never_grow_the_shared_write_count(tmp_path, monkeypatch):
    """Ten cross-replica hits: ten hits, zero writes, one artifact."""
    root = tmp_path / "data"
    _seed(root)
    l2 = str(tmp_path / "l2")
    a = _engine(l2, root, monkeypatch)
    a.query_duckdb(QUERY)
    assert a._l2_cache is not None and a._l2_cache.stats()["writes"] == 1
    files_before = sorted(p.name for p in Path(l2).iterdir())

    last = None
    for _ in range(10):
        last = _engine(l2, root, monkeypatch)
        last.query_duckdb(QUERY)
    assert last is not None and last._l2_cache is not None
    assert last._l2_cache.stats()["hits"] == 1
    assert last._l2_cache.stats()["writes"] == 0
    assert sorted(p.name for p in Path(l2).iterdir()) == files_before


def test_republishes_counter_is_a_real_signal(tmp_path, monkeypatch):
    """The republishes counter fires on a genuine overwrite, not just never.

    A counter that can only ever read 0 proves nothing. This drives the ONE
    case that legitimately republishes -- a recompute after a MISS whose
    result lands on a still-fresh entry for the same key -- and asserts the
    counter moves. That is what makes the pure-hit assertion above
    meaningful.
    """
    from sqlhandler.l2cache import L2ResultCache

    l2 = tmp_path / "l2"
    l2.mkdir()
    cache = L2ResultCache(str(l2), ttl=3600)
    key = "c" * 64
    table = pa.table({"a": pa.array([1, 2, 3], type=pa.int64())})

    cache.store(key, table)  # first publish: nothing was there
    assert cache.stats()["republishes"] == 0
    assert cache.stats()["writes"] == 1

    cache.store(key, table)  # same key, fresh entry already present
    stats = cache.stats()
    assert stats["writes"] == 2
    assert stats["republishes"] == 1  # the overwrite IS counted


def test_async_hit_enqueues_nothing(tmp_path, monkeypatch):
    """Async mode (the deployment config): a hit must not occupy the queue.

    With writeAsync on, a republish per hit would push an entry through the
    bounded write queue for data already published -- wasted PVC IO, and a
    slot that a genuine store may need (a full queue DROPS stores).
    """
    root = tmp_path / "data"
    _seed(root)
    l2 = str(tmp_path / "l2")
    a = _engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_WRITE_ASYNC": "1"})
    a.query_duckdb(QUERY)
    assert a._l2_cache is not None
    assert a._l2_cache.flush_async_stores() is True
    assert a._l2_cache.stats()["writes"] == 1

    b = _engine(l2, root, monkeypatch, env={"SQLHANDLER_L2_WRITE_ASYNC": "1"})
    b.query_duckdb(QUERY)
    assert b._l2_cache is not None
    stats = b._l2_cache.stats()
    assert stats["hits"] == 1
    assert stats["write_mode"] == "async"
    assert stats["write_queue_depth"] == 0  # nothing enqueued by the hit
    assert b._l2_cache.flush_async_stores() is True
    assert b._l2_cache.stats()["writes"] == 0  # still nothing published


# ---------------------------------------------------------------------------
# 3. BUG B visibility: the per-hit metadata cost is counted, not hidden
# ---------------------------------------------------------------------------


def test_cache_hit_still_pays_a_version_check_and_it_is_counted(tmp_path, monkeypatch):
    """A cache HIT costs one provider version check; cache_stats shows it.

    This is the RATIFIED behavior (SQLHANDLER_CACHE_KEY_VERSION_THROTTLE is
    off by default so an ETL commit invalidates instantly -- three tests pin
    it). This test does not change it: it makes the cost visible, which is
    what an operator needs to decide whether to opt into the throttle.
    """
    root = tmp_path / "data"
    _seed(root)
    a = _engine(str(tmp_path / "l2"), root, monkeypatch)
    assert a.cache_stats()["version_checks"] == {
        "provider_calls": 0,
        "throttle_reuses": 0,
        "throttled": False,
        "interval_s": a.version_check_interval,
    }

    a.query_duckdb(QUERY)  # cold
    cold_calls = a.cache_stats()["version_checks"]["provider_calls"]
    assert cold_calls >= 1

    before = a.cache_stats()["version_checks"]["provider_calls"]
    a.query_duckdb(QUERY)  # L1 HIT
    after = a.cache_stats()["version_checks"]["provider_calls"]
    assert after == before + 1, "a cache hit pays exactly one metadata round-trip"
    # The engine counter is the CACHE-KEY slice only: the provider also sees
    # _open_dataset's dataset-handle checks, which are a separate concern
    # (their own SQLHANDLER_VERSION_CHECK_INTERVAL), so the engine counter can
    # never exceed what the provider actually served.
    assert after <= a.provider.version_checks


def test_throttle_opt_in_is_visible_as_reuses(tmp_path, monkeypatch):
    """With the opt-in throttle ON, repeated hits show reuses instead of calls.

    Only VERSIONED tables can be throttled: the cache holds the token the
    provider reported, and a None token means "this source has no cheap
    change signal" (plain parquet), so the engine re-asks rather than
    pretending to know. The provider is therefore given a real version here
    -- which is also the only case where the throttle buys anything, since a
    None token is the cheap path by definition.
    """
    root = tmp_path / "data"
    _seed(root)
    a = _engine(
        str(tmp_path / "l2"),
        root,
        monkeypatch,
        env={"SQLHANDLER_CACHE_KEY_VERSION_THROTTLE": "1", "SQLHANDLER_VERSION_CHECK_INTERVAL": "60"},
    )
    a.provider.versions["shop/sales"] = 7  # a versioned (Delta-like) source
    a.query_duckdb(QUERY)
    calls_after_cold = a.cache_stats()["version_checks"]["provider_calls"]
    assert calls_after_cold >= 1
    a.query_duckdb(QUERY)
    a.query_duckdb(QUERY)
    stats = a.cache_stats()["version_checks"]
    assert stats["throttled"] is True
    assert stats["provider_calls"] == calls_after_cold, "throttled: no new provider calls"
    assert stats["throttle_reuses"] >= 1


def test_unversioned_table_cannot_be_throttled(tmp_path, monkeypatch):
    """A None token is never cached as if it were a version.

    Plain parquet reports no version, so the throttle has nothing to serve
    and the engine keeps asking -- correct (it never claims freshness it
    cannot observe) and free (the provider returns None without any IO).
    """
    root = tmp_path / "data"
    _seed(root)
    a = _engine(
        str(tmp_path / "l2"),
        root,
        monkeypatch,
        env={"SQLHANDLER_CACHE_KEY_VERSION_THROTTLE": "1", "SQLHANDLER_VERSION_CHECK_INTERVAL": "60"},
    )
    a.query_duckdb(QUERY)
    before = a.cache_stats()["version_checks"]["provider_calls"]
    a.query_duckdb(QUERY)
    stats = a.cache_stats()["version_checks"]
    assert stats["throttle_reuses"] == 0
    assert stats["provider_calls"] == before + 1


def test_version_check_counters_exist_even_with_l2_off(tmp_path, monkeypatch):
    """No L2 dir: the counters still exist (they are key-cost, not L2-scoped)."""
    monkeypatch.delenv("SQLHANDLER_L2_DIR", raising=False)
    root = tmp_path / "data"
    _seed(root)
    monkeypatch.setenv("SQLHANDLER_L2_WRITE_ASYNC", "0")
    a = SqlEngine(CountingProvider(root), cache_ttl=0, cache_dir=None)
    assert a._l2_cache is None
    a.query_duckdb(QUERY)
    stats = a.cache_stats()
    assert stats["l2"] is None
    assert stats["version_checks"]["provider_calls"] >= 1
    assert stats["version_checks"]["throttled"] is False
