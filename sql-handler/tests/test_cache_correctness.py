"""Cache-correctness regression tests (audit 2026-11-12, engine zone).

Pins the accounting/identity bugs that the green suite never exercised:
overwrite accounting, L2-warm eviction identity, decode budgets, block-cache
layout keying, L2 stats cost, orphan reclamation and directory privacy.
"""

import io
import os
import stat
import time

import pyarrow as pa
import pyarrow.dataset as pad

from sqlhandler.blockcache import _CachedStream
from sqlhandler.engine import SqlEngine
from sqlhandler.l2cache import L2ResultCache, _ensure_private_dir
from sqlhandler.provider import DataProvider, TableInfo


class FakeProvider(DataProvider):
    kind = "fake"

    def __init__(self, root):
        self.root = root

    def list_tables(self):
        return [TableInfo(name="work_order", schema="workorder", format="parquet")]

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        return pad.dataset(str(self.root / info.path), format="parquet")

    def check_version(self, info):
        return None


def _engine(tmp_path, **kw) -> SqlEngine:
    kw.setdefault("cache_ttl", 3600)
    kw.setdefault("dataset_cache_ttl", 0)
    return SqlEngine(FakeProvider(tmp_path), **kw)


# ------------------------------------------------- C (P1-10): decode budget


def test_l2_hit_over_the_decode_budget_is_a_miss(tmp_path):
    cache = L2ResultCache(str(tmp_path), ttl=3600.0)
    key = "a" * 64
    table = pa.table({"x": list(range(1000))})  # nbytes = 8000
    cache.store(key, table)
    assert cache.lookup(key, max_bytes=1024) is None, "an over-budget entry must be an ordinary miss"
    assert cache.lookup(key, max_bytes=table.nbytes) is not None
    assert cache.lookup(key) is not None  # no budget -> historical behavior


def test_engine_charges_the_l2_decode_to_the_gate_and_a_budget(tmp_path):
    seen = {}

    class _FakeL2:
        def lookup(self, key, max_bytes=None):
            seen["max_bytes"] = max_bytes
            # implicitly returns None (an ordinary miss)

    eng = _engine(tmp_path)
    eng._l2_cache = _FakeL2()
    assert eng._result_cache_lookup("b" * 64) is None
    assert seen["max_bytes"] is not None, "the engine must pass its decode budget to the L2"


# ------------------------------------------------- F2: overwrite accounting


def test_result_cache_bytes_do_not_inflate_on_overwrite(tmp_path):
    eng = _engine(tmp_path)
    small = pa.table({"a": [1, 2, 3]})
    big = pa.table({"a": list(range(1000))})
    eng._result_cache_store("k", small)
    eng._result_cache_store("k", big)
    assert len(eng._result_cache) == 1
    assert eng._result_cache_bytes == big.nbytes, "the overwritten entry's bytes must be given back"


# ------------------------------------------------- F3: L2 warm carries its SQL


def test_l2_hit_warms_l1_with_its_sql(tmp_path):
    eng = _engine(tmp_path)
    eng._l2_cache = L2ResultCache(str(tmp_path / "l2"), ttl=3600.0)
    sql = "SELECT id FROM work_order"
    key = "c" * 64
    eng._l2_cache.store(key, pa.table({"id": [1, 2]}))
    got = eng._result_cache_lookup(key, sql=sql)
    assert got is not None
    assert eng._result_cache_sql.get(key) == sql, "write-tier eviction cannot see an entry without its SQL"


# ------------------------------------------------- F4: block-cache layout


def _stream(tmp_path, block_size, data=b"0123456789"):
    cfg = {"dir": str(tmp_path), "block_size": block_size, "max_bytes": 0}
    return _CachedStream(lambda: io.BytesIO(data), "p", len(data), cfg, lambda n: None)


def test_block_cache_key_includes_the_block_size(tmp_path):
    a = _stream(tmp_path, 4)
    b = _stream(tmp_path, 5)
    assert a._dir != b._dir, "a block-size change must not reuse another layout's blocks"


def test_wrong_length_block_is_refetched_not_spliced(tmp_path):
    first = _stream(tmp_path, 4)
    assert first.read(10) == b"0123456789"
    (first._dir / "block-1").write_bytes(b"ZZZ")  # corrupt: block 1 is 4 bytes
    fresh = _stream(tmp_path, 4)
    assert fresh.read(10) == b"0123456789", "a wrong-length block must never be spliced into the read"


# ------------------------------------------------- F5: layout version in the key


def test_l2_artifacts_carry_a_layout_version(tmp_path, monkeypatch):
    """A rolling upgrade must not trust artifacts from another cache layout."""
    import json

    import pyarrow.parquet as pq

    from sqlhandler import l2cache

    cache = L2ResultCache(str(tmp_path), ttl=3600.0)
    key = "d" * 64
    cache.store(key, pa.table({"x": [1, 2, 3]}))
    meta = json.loads((tmp_path / f"{key[:16]}.json").read_text(encoding="utf-8"))
    assert meta["layout"] == l2cache.CACHE_LAYOUT_VERSION
    assert cache.lookup(key) is not None
    # A binary with a DIFFERENT layout treats the artifact as a miss and
    # removes it instead of serving data whose meaning it cannot assume.
    monkeypatch.setattr(l2cache, "CACHE_LAYOUT_VERSION", "999")
    assert cache.lookup(key) is None
    assert not (tmp_path / f"{key[:16]}.json").exists()

    # A LEGACY sidecar (pre-layout-field) is still served: the upgrade itself
    # does not cold-start the cache.
    legacy = "e" * 64
    pq.write_table(pa.table({"x": [7]}), str(tmp_path / f"{legacy[:16]}.parquet"))
    (tmp_path / f"{legacy[:16]}.json").write_text(
        json.dumps({"key": legacy, "created": time.time(), "rows": 1, "bytes": 8}),
        encoding="utf-8",
    )
    assert cache.lookup(legacy) is not None


# ------------------------------------------------- F14: stats is O(1)


def test_l2_stats_does_not_walk_the_directory(tmp_path, monkeypatch):
    cache = L2ResultCache(str(tmp_path), ttl=3600.0)
    calls = {"n": 0}
    real = cache._cached_bytes

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(cache, "_cached_bytes", counted)
    cache.stats()
    cache.stats()
    cache.stats()
    assert calls["n"] == 0, "stats() must not walk the shared directory"
    measured = cache.refresh_bytes()
    assert calls["n"] == 1
    assert cache.stats()["bytes"] == measured


# ------------------------------------------------- F15: orphans + dir privacy


def test_orphaned_artifacts_are_reclaimed(tmp_path):
    d = tmp_path / "l2"
    d.mkdir()
    old = time.time() - 10_000
    orphan = d / "deadbeefdeadbeef.parquet"
    orphan.write_bytes(b"x" * 1000)
    stale_tmp = d / "abc.parquet.tmp"
    stale_tmp.write_bytes(b"x" * 1000)
    for path in (orphan, stale_tmp):
        os.utime(path, (old, old))
    cache = L2ResultCache(str(d), ttl=3600.0, max_bytes=500)
    assert cache.reclaim_orphans() == 2
    assert cache.refresh_bytes() == 0
    assert cache.enforce_size_cap() == 0, "with the orphans gone the cap needs no eviction"


def test_fresh_artifacts_are_not_reclaimed(tmp_path):
    d = tmp_path / "l2"
    d.mkdir()
    fresh = d / "cafebabecafebabe.parquet"
    fresh.write_bytes(b"x")
    cache = L2ResultCache(str(d), ttl=3600.0)
    assert cache.reclaim_orphans() == 0, "a just-published artifact must never be reclaimed"


def test_private_dir_mode_is_enforced_on_an_existing_directory(tmp_path):
    d = tmp_path / "shared"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    assert _ensure_private_dir(d) is True
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
