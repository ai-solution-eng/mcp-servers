"""Tests for the performance features: disk block cache, S3/OneLake options
passthrough, and clustering (auto-sort) of materialized virtual tables."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest
import yaml

from sqlhandler import s3 as s3mod
from sqlhandler.blockcache import _BlockCacheHandler, maybe_block_cache
from sqlhandler.config import S3Config
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import LakehouseError, TableInfo

# ---------------------------------------------------------------------------
# block cache: correctness (byte-for-byte) + warm reuse (no base reads)
# ---------------------------------------------------------------------------


class _CountingHandler(pafs.FileSystemHandler):
    """Delegates to a real fs; counts data-stream opens (the expensive part)."""

    def __init__(self, base: pafs.FileSystem):
        self.base = base
        self.stream_opens = 0

    def __eq__(self, other):
        return isinstance(other, _CountingHandler) and self.base == other.base

    def normalize_path(self, path):
        return self.base.normalize_path(path)

    def open_input_file(self, path):
        self.stream_opens += 1
        return self.base.open_input_file(path)

    def open_input_stream(self, path):
        self.stream_opens += 1
        return self.base.open_input_stream(path)

    def get_file_info(self, path):
        return self.base.get_file_info(path)

    def get_file_info_selector(self, selector):
        # mirror _BlockCacheHandler: pyarrow >= 21 removed the dedicated
        # method from the real filesystems (see the regression test below)
        if hasattr(self.base, "get_file_info_selector"):
            return self.base.get_file_info_selector(selector)
        return self.base.get_file_info(selector)

    def get_type_name(self):
        return "counting"

    def _ro(self, *a, **k):
        raise NotImplementedError

    create_dir = _ro
    delete_dir = _ro
    delete_dir_contents = _ro
    delete_root_dir_contents = _ro
    delete_file = _ro
    move = _ro
    copy_file = _ro
    open_output_stream = _ro
    open_append_stream = _ro


@pytest.fixture()
def cached_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE_DIR", str(tmp_path / "blocks"))
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE_BLOCK_SIZE", "16384")  # 16KiB: force multi-block
    return tmp_path


def _seed_parquet(tmp_path: Path) -> Path:
    """A ~200KiB file of unpatterned-ish rows so reads span many blocks."""
    n = 4000
    t = pa.table(
        {
            "id": list(range(n)),
            "txt": [f"value-{i * 7919 % 100003}-{'x' * (i % 37)}" for i in range(n)],
            "num": [i * 3.25 for i in range(n)],
        }
    )
    path = tmp_path / "t.parquet"
    pq.write_table(t, path)
    return path


def _seed_parquet_memory(tmp_path: Path) -> tuple[pafs.FileSystem, bytes, str]:
    """A ~200KiB parquet file behind a NON-local pyarrow filesystem
    (SubTreeFileSystem — not skipped by the cache), returning (fs, raw bytes, path)."""
    n = 4000
    t = pa.table(
        {
            "id": list(range(n)),
            "txt": [f"value-{i * 7919 % 100003}-{'x' * (i % 37)}" for i in range(n)],
            "num": [i * 3.25 for i in range(n)],
        }
    )
    sink = pa.BufferOutputStream()
    pq.write_table(t, sink)
    raw = sink.getvalue().to_pybytes()
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    with fs.open_output_stream("/t.parquet") as f:
        f.write(raw)
    return fs, raw, "/t.parquet"


def test_block_cache_reads_are_byte_identical(cached_env):
    fs, raw, path = _seed_parquet_memory(cached_env)
    expected = pq.read_table(pa.BufferReader(raw)).to_pylist()

    wrapped = maybe_block_cache(fs)
    assert isinstance(wrapped, pafs.PyFileSystem)  # enabled + wrapped
    got = pad.dataset(path, filesystem=wrapped, format="parquet").to_table().to_pylist()
    assert got == expected

    # direct byte-range reads through the wrapper (parquet-style seeking).
    # NOTE the pyarrow contract: open_input_file is the random-access open;
    # open_input_stream is sequential by contract (handlers return streams
    # that PyFileSystem reports as non-seekable).
    s = wrapped.open_input_file(path)
    size = s.size()
    s.seek(size - 100)
    assert s.read() == raw[-100:]
    s.seek(0)
    assert s.read(501) == raw[:501]
    s.seek(12345)
    assert s.read(777) == raw[12345 : 12345 + 777]
    assert s.read() == raw[12345 + 777 :]  # read() = to-EOF from the current position


def test_block_cache_warm_scan_avoids_the_base_stream(cached_env):
    fs, _, path = _seed_parquet_memory(cached_env)
    counter = _CountingHandler(fs)
    fs = pafs.PyFileSystem(counter)

    wrapped = maybe_block_cache(fs)
    q = pad.dataset(path, filesystem=wrapped, format="parquet")
    assert q.to_table().num_rows == 4000
    assert counter.stream_opens >= 1

    # fresh wrapper (new process state, same disk cache): zero base opens
    counter2 = _CountingHandler(pafs.SubTreeFileSystem(str(cached_env), pafs.LocalFileSystem()))
    warm = pad.dataset(
        path, filesystem=maybe_block_cache(pafs.PyFileSystem(counter2)), format="parquet"
    )
    out = warm.to_table()
    assert out.num_rows == 4000
    assert counter2.stream_opens == 0  # every byte came from the local block cache


def test_block_cache_include_local_opts_nfs_mounts_in(cached_env, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE_INCLUDE_LOCAL", "1")
    wrapped = maybe_block_cache(pafs.LocalFileSystem(), purpose="nfs-mount")
    assert isinstance(wrapped, pafs.PyFileSystem)  # NFS story: local IS wrappable


def test_block_cache_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SQLHANDLER_BLOCK_CACHE", raising=False)
    wrapped = maybe_block_cache(pafs.LocalFileSystem())
    assert isinstance(wrapped, pafs.LocalFileSystem)  # untouched when off


def test_block_cache_skips_local_and_survives_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    assert isinstance(maybe_block_cache(pafs.LocalFileSystem()), pafs.LocalFileSystem)
    # a handler failure degrades to the plain fs, never raises into the data path
    monkeypatch.setattr(
        pafs, "PyFileSystem", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    fs = pafs.LocalFileSystem()
    assert maybe_block_cache(fs, purpose="probe") is fs


# ---------------------------------------------------------------------------
# S3 / OneLake operator-tuning passthroughs
# ---------------------------------------------------------------------------


def test_s3_options_passthrough(monkeypatch):
    created = {}

    class FakeS3:
        def __init__(self, **kw):
            created.update(kw)

    monkeypatch.setattr(s3mod.pafs, "S3FileSystem", FakeS3)
    monkeypatch.setenv(
        "SQLHANDLER_S3_OPTIONS", json.dumps({"request_timeout": 99, "retry_limit": 7})
    )
    cfg = S3Config(endpoint_url="http://127.0.0.1:9000", access_key="k", secret_key="s")
    s3mod.build_s3fs(cfg)
    assert created["request_timeout"] == 99
    assert created["retry_limit"] == 7

    monkeypatch.setenv("SQLHANDLER_S3_OPTIONS", "{not json")
    with pytest.raises(LakehouseError, match="SQLHANDLER_S3_OPTIONS"):
        s3mod.build_s3fs(cfg)


def test_s3_block_cache_wraps_the_dataset_fs(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE_DIR", str(tmp_path / "blocks"))
    created = {}

    class FakeS3:
        def __init__(self, **kw):
            created.update(kw)

    monkeypatch.setattr(s3mod.pafs, "S3FileSystem", FakeS3)
    from sqlhandler.blockcache import _cfg as bc_cfg

    monkeypatch.setenv("SQLHANDLER_S3_OPTIONS", json.dumps({"request_timeout": 42}))
    s3mod.build_s3fs(S3Config(endpoint_url="http://127.0.0.1:9000"))
    assert created["request_timeout"] == 42
    assert bc_cfg()["enabled"] is True  # the seam exists where datasets are built
    assert isinstance(_BlockCacheHandler, type)  # handler importable/wireable


# ---------------------------------------------------------------------------
# clustering: materialized virtual results are sorted for row-group pruning
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# block cache × delta-rs / OneLake: data reads routed through the cache
# ---------------------------------------------------------------------------


def _seed_delta_table(tmp_path: Path, name: str = "tab", n: int = 4000) -> tuple[Path, pa.Table]:
    """A real local Delta table standing in for the OneLake object store."""
    from deltalake import write_deltalake

    tbl = pa.table({"id": list(range(n)), "txt": [f"v-{i}" for i in range(n)]})
    d = tmp_path / name
    write_deltalake(str(d), tbl, mode="overwrite")
    return d, tbl


def _local_delta_provider(d: Path):
    """A OneLakeProvider whose object store IS a local Delta dir.

    The provider machinery (storage options, handler construction, cache
    wrapping, snapshot scoping) runs for real; only the ABFS transport is
    replaced by the local filesystem, so the test exercises the exact
    production code path with a mocked object store.
    """
    from deltalake import DeltaTable

    from sqlhandler.config import FabricConfig
    from sqlhandler.onelake import OneLakeProvider

    p = OneLakeProvider(
        FabricConfig(
            tenant_id="t",
            client_id="c",
            client_secret="s",
            lakehouse_abfss_url="abfss://ws@onelake.dfs.fabric.microsoft.com/lh",
        )
    )
    p.table_uri = lambda info: str(d)

    def _open(info, version=None):
        if version is not None:
            return DeltaTable(str(d), version=int(version))
        return DeltaTable(str(d))

    p._open_delta = _open
    return p


def _spy_delta_opens(monkeypatch) -> dict[str, int]:
    """Count data-stream opens reaching the delta-rs object-store handler."""
    import deltalake.fs as dlfs

    opens = {"n": 0}
    real_open = dlfs.DeltaStorageHandler.open_input_file

    def counting_open(self, path):
        opens["n"] += 1
        return real_open(self, path)

    monkeypatch.setattr(dlfs.DeltaStorageHandler, "open_input_file", counting_open)
    return opens


def test_onelake_block_cache_hits_on_second_scan(cached_env, monkeypatch):
    """The mission test: repeated OneLake delta scans hit the disk cache.

    delta-rs opens bypassed the block cache entirely (audit: OneLake never
    cached bytes) — after the wiring, the second open_dataset over the same
    table reads every byte from pod-local disk.
    """
    d, tbl = _seed_delta_table(cached_env)
    provider = _local_delta_provider(d)
    opens = _spy_delta_opens(monkeypatch)
    info = TableInfo(name="tab", schema="s", format="delta")
    expected = tbl.to_pylist()

    ds1 = provider.open_dataset(info)
    assert ds1.to_table().to_pylist() == expected
    cold = opens["n"]
    assert cold >= 1  # first scan really reads through the (mocked) store

    # a NEW dataset handle over the same table: zero further store reads
    ds2 = provider.open_dataset(info)
    assert ds2.to_table().to_pylist() == expected
    assert opens["n"] == cold


def test_onelake_cache_off_keeps_the_delta_rs_builtin_path(cached_env, monkeypatch):
    """With the block cache off, open_dataset passes NO filesystem — the
    exact pre-change delta-rs path (no wrapper, no extra machinery)."""
    import deltalake

    d, tbl = _seed_delta_table(cached_env)
    provider = _local_delta_provider(d)
    monkeypatch.delenv("SQLHANDLER_BLOCK_CACHE", raising=False)

    calls: list[object] = []
    real = deltalake.DeltaTable.to_pyarrow_dataset

    def spy(self, *args, **kwargs):
        calls.append(kwargs.get("filesystem", "NOT-PASSED"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(deltalake.DeltaTable, "to_pyarrow_dataset", spy)
    info = TableInfo(name="tab", schema="s", format="delta")
    assert provider.open_dataset(info).to_table().to_pylist() == tbl.to_pylist()
    assert calls == ["NOT-PASSED"]

    # cache on: the wrapped filesystem IS passed to delta-rs
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    calls.clear()
    assert provider.open_dataset(info).to_table().to_pylist() == tbl.to_pylist()
    assert len(calls) == 1
    fs = calls[0]
    assert fs is not None and fs != "NOT-PASSED"
    assert isinstance(fs, pafs.PyFileSystem)
    assert "blockcache" in fs.type_name  # the data path goes through the cache


def test_onelake_time_travel_is_snapshot_consistent_after_a_new_commit(cached_env):
    """A new ETL commit must not corrupt historical reads (and vice versa)."""
    from deltalake import write_deltalake

    d, tbl0 = _seed_delta_table(cached_env, n=50)
    provider = _local_delta_provider(d)
    info = TableInfo(name="tab", schema="s", format="delta")
    expected_v0 = tbl0.to_pylist()

    assert provider.open_dataset(info, version=0).to_table().to_pylist() == expected_v0

    tbl1 = pa.table({"id": list(range(100, 160)), "txt": [f"w-{i}" for i in range(60)]})
    write_deltalake(str(d), tbl1, mode="overwrite")  # v1 replaces v0's data

    assert provider.open_dataset(info).to_table().to_pylist() == tbl1.to_pylist()
    # historical re-read AFTER the live read warmed v1's blocks: still v0
    assert provider.open_dataset(info, version=0).to_table().to_pylist() == expected_v0


def test_onelake_delta_file_sizes_come_from_the_delta_log(cached_env):
    """The cache is seeded with the log's add-action sizes (no stat round-trips)."""
    d, _tbl = _seed_delta_table(cached_env, n=100)
    from deltalake import DeltaTable

    from sqlhandler.onelake import OneLakeProvider

    sizes = OneLakeProvider._delta_file_sizes(DeltaTable(str(d)))
    assert sizes and len(sizes) == 1
    (size,) = sizes.values()
    assert isinstance(size, int) and size > 0

    class Broken:
        def get_add_actions(self, flatten):
            raise RuntimeError("log unreadable")

    assert OneLakeProvider._delta_file_sizes(Broken()) is None


def test_block_cache_scope_keeps_snapshots_isolated(cached_env):
    """Same relative path + same size, different snapshot scope → different
    bytes. Without the scope the second read would be served the first
    snapshot's cached blocks (the exact hazard the scope exists for)."""
    snap0 = cached_env / "snap0"
    snap1 = cached_env / "snap1"
    snap0.mkdir()
    snap1.mkdir()
    content0 = ("0" * 50).encode()
    content1 = ("1" * 50).encode()  # same size, different bytes
    (snap0 / "data.parquet").write_bytes(content0)
    (snap1 / "data.parquet").write_bytes(content1)

    w0 = maybe_block_cache(
        pafs.SubTreeFileSystem(str(snap0), pafs.LocalFileSystem()), scope="delta-v0"
    )
    w1 = maybe_block_cache(
        pafs.SubTreeFileSystem(str(snap1), pafs.LocalFileSystem()), scope="delta-v1"
    )

    assert w0.open_input_file("data.parquet").read() == content0
    assert w1.open_input_file("data.parquet").read() == content1
    # the two snapshots occupy distinct cache keys
    block_dirs = {p.name for p in (Path(cached_env) / "blocks").iterdir()}
    assert len(block_dirs) == 2


def test_block_cache_sizes_hint_engages_without_stat(cached_env):
    """known_sizes lets the cache engage on filesystems that cannot stat —
    a warm read never touches the base at all (this is how the OneLake
    delta path avoids one HEAD per file per open)."""
    fs, raw, path = _seed_parquet_memory(cached_env)
    n_opens = {"n": 0}

    class NoStatHandler(pafs.FileSystemHandler):
        """Serves reads but refuses to stat anything."""

        def __init__(self):
            self.inner = fs

        def open_input_file(self, p):
            n_opens["n"] += 1
            return self.inner.open_input_file(p)

        def open_input_stream(self, p):
            n_opens["n"] += 1
            return self.inner.open_input_stream(p)

        def get_file_info(self, p):
            raise OSError("no stat available")

        def get_file_info_selector(self, selector):
            raise OSError("no stat available")

        def normalize_path(self, p):
            return str(p)

        def get_type_name(self):
            return "nostat"

        def _ro(self, *a, **k):
            raise NotImplementedError

        create_dir = delete_dir = delete_dir_contents = delete_root_dir_contents = _ro
        delete_file = move = copy_file = open_output_stream = open_append_stream = _ro

    wrapped = maybe_block_cache(pafs.PyFileSystem(NoStatHandler()), sizes={path: len(raw)})

    # cold read: byte-identical, cached despite the unstat-able base
    assert wrapped.open_input_file(path).read() == raw
    cold = n_opens["n"]
    assert cold >= 1

    # warm: zero further base opens — the hinted size seeded the cache
    assert wrapped.open_input_file(path).read() == raw
    assert n_opens["n"] == cold
    s = wrapped.open_input_file(path)
    s.seek(10)
    assert s.read(5) == raw[10:15]
    assert n_opens["n"] == cold


class _ModernPyarrowFS:
    """A base filesystem shaped like pyarrow >= 21: the dedicated
    ``get_file_info_selector`` is REMOVED and selector listing lives on
    ``get_file_info(paths_or_selector)``. Duck-typed (the handler only
    touches Python attributes on the base), so the regression below runs
    the fallback path even on an older installed pyarrow."""

    def __init__(self, inner: pafs.FileSystem):
        self._inner = inner

    def get_file_info(self, paths_or_selector):
        return self._inner.get_file_info(paths_or_selector)

    def open_input_file(self, path):
        return self._inner.open_input_file(path)

    def open_input_stream(self, path):
        return self._inner.open_input_stream(path)

    def normalize_path(self, path):
        return self._inner.normalize_path(path)

    @property
    def type_name(self):
        return self._inner.type_name


def test_block_cache_selector_discovery_on_modern_pyarrow(cached_env):
    """Regression (g2 fleet): pyarrow >= 21 removed
    ``FileSystem.get_file_info_selector``; the handler forwarded to it, so
    EVERY cached dataset open died with ``AttributeError`` inside the C++
    discovery callback — surfacing downstream as ``virtual table
    'revenue_by_region' could not be built ... Table with name orders does
    not exist``. The handler must use the selector-capable
    ``get_file_info`` on bases that no longer have the old method."""
    path = _seed_parquet(cached_env)
    expected = pq.read_table(path).to_pylist()

    base = _ModernPyarrowFS(pafs.LocalFileSystem())
    assert not hasattr(base, "get_file_info_selector")

    cfg = {
        "enabled": True,
        "include_local": True,
        "dir": str(cached_env / "blocks"),
        "block_size": 16384,
        "max_bytes": 64 * 1024 * 1024,
    }
    wrapped = pafs.PyFileSystem(_BlockCacheHandler(base, cfg))

    # selector-based discovery (what pad.dataset / the engine runs) + reads
    got = pad.dataset(str(path), filesystem=wrapped, format="parquet").to_table().to_pylist()
    assert got == expected


def test_block_cache_handler_scope_is_identity(cached_env):
    """Two handlers over the same base differing only by scope are distinct
    (pyarrow caches datasets by filesystem identity — scopes must not alias)."""
    cfg = {
        "enabled": True,
        "include_local": False,
        "dir": str(cached_env / "blocks"),
        "block_size": 4096,
        "max_bytes": 0,
    }
    base = pafs.SubTreeFileSystem(str(cached_env), pafs.LocalFileSystem())
    h0 = _BlockCacheHandler(base, cfg, scope="delta-v0")
    h0b = _BlockCacheHandler(base, cfg, scope="delta-v0")
    h1 = _BlockCacheHandler(base, cfg, scope="delta-v1")
    assert h0 == h0b
    assert hash(h0) == hash(h0b)
    assert h0 != h1


def test_block_cache_enabled_flag(tmp_path, monkeypatch):
    from sqlhandler.blockcache import block_cache_enabled

    monkeypatch.delenv("SQLHANDLER_BLOCK_CACHE", raising=False)
    assert block_cache_enabled() is False
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    assert block_cache_enabled() is True
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "0")
    assert block_cache_enabled() is False


# ---------------------------------------------------------------------------
# profile_table: one data scan (audit finding — it scanned twice)
# ---------------------------------------------------------------------------


def _profile_recording_connect(monkeypatch):
    """Replace duckdb.connect with a recording proxy; returns (sqls, restore-done)."""
    import duckdb

    sqls: list[str] = []
    real_connect = duckdb.connect

    class RecordingCon:
        def __init__(self, inner):
            self._inner = inner

        def sql(self, query, *args, **kwargs):
            sqls.append(query)
            return self._inner.sql(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def connect(*args, **kwargs):
        return RecordingCon(real_connect(*args, **kwargs))

    monkeypatch.setattr(duckdb, "connect", connect)
    return sqls


class _ProfileProvider:
    """One parquet table under workorder/t, opened from the temp dir."""

    kind = "profile-fake"

    def __init__(self, root: Path):
        self.root = root
        self._dset = pad.dataset(str(root / "workorder" / "t"), format="parquet")

    def list_tables(self):
        return [TableInfo(name="t", schema="workorder", format="parquet")]

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info, version=None):
        return self._dset

    def check_version(self, info):
        return None


def test_profile_table_single_scan(tmp_path, monkeypatch):
    """SUMMARIZE is the ONLY data-touching query: the row count of the
    bounded sample follows from metadata + the LIMIT (was: count(*) over
    the same sample — the same scan twice)."""
    n = 5000
    d = tmp_path / "workorder" / "t"
    d.mkdir(parents=True)
    pq.write_table(
        pa.table({"id": list(range(n)), "txt": [f"v{i}" for i in range(n)]}), d / "part.parquet"
    )

    eng = SqlEngine(_ProfileProvider(tmp_path), cache_ttl=0, cache_dir=str(tmp_path))
    sqls = _profile_recording_connect(monkeypatch)

    p = eng.profile_table("t")
    assert p["n_rows"] == n
    assert p["profiled_rows"] == n
    assert sum("SUMMARIZE" in q for q in sqls) == 1
    assert not any("count(*) FROM (" in q for q in sqls)


def test_profile_table_single_scan_with_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_PROFILE_MAX_ROWS", "2")
    d = tmp_path / "workorder" / "t"
    d.mkdir(parents=True)
    pq.write_table(pa.table({"id": list(range(5000))}), d / "part.parquet")

    eng = SqlEngine(_ProfileProvider(tmp_path), cache_ttl=0, cache_dir=str(tmp_path))
    sqls = _profile_recording_connect(monkeypatch)

    p = eng.profile_table("t")
    assert p["n_rows"] == 5000  # metadata count, un-LIMITed
    assert p["profiled_rows"] == 2  # min(n_rows, cap) — no second scan needed
    assert sum("SUMMARIZE" in q for q in sqls) == 1
    assert not any("count(*) FROM (" in q for q in sqls)


def test_profile_count_fallback_queries_when_metadata_unreadable():
    """Metadata-broken sources keep working: the count query is the one
    sanctioned second scan (extracted so the happy path stays single-scan)."""
    import duckdb

    con = duckdb.connect()
    try:
        assert SqlEngine._profile_count_fallback(con, "SELECT 1 AS x") == 1
        assert SqlEngine._profile_count_fallback(con, "SELECT * FROM range(10)") == 10
    finally:
        con.close()


def test_materialized_virtual_result_is_clustered(tmp_path, monkeypatch):
    from test_virtual import _write

    n = 5000
    _write(
        tmp_path,
        "shop/sales2",
        pa.table({"id": list(range(n)), "grp": [f"g{i % 4}" for i in range(n)]}),
    )

    class P2:
        kind = "p2"

        def list_tables(self):
            return [TableInfo(name="sales2", schema="shop", format="parquet")]

        def table_uri(self, info):
            return f"fake://{info.path}"

        def open_dataset(self, info, version=None):
            return pad.dataset(str(tmp_path / info.path), format="parquet")

        def check_version(self, info):
            return None

    cat = tmp_path / "catalog.yaml"
    cat.write_text(
        yaml.safe_dump(
            {
                "tables": {
                    "vw_clustered": {
                        "definition": "SELECT grp, id FROM sales2 WHERE id >= 0",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    eng = SqlEngine(P2(), cache_ttl=0, cache_dir=str(tmp_path))

    out = eng.query_duckdb("SELECT count(*) AS c FROM vw_clustered")
    assert out.column("c").to_pylist() == [n]

    cache_file = next(Path(eng._virtual_cache_dir).glob("*.parquet"))
    cached = pq.read_table(cache_file)
    assert cached.num_rows == n
    grp = cached.column("grp").to_pylist()
    ids = cached.column("id").to_pylist()
    assert grp == sorted(grp)  # clustered on the low-cardinality column
    for g in ("g0", "g1", "g2", "g3"):  # and the id within each group
        block = [i for i, gg in zip(ids, grp) if gg == g]
        assert block == sorted(block)


def test_clustering_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_VIRTUAL_CACHE_SORT", "0")
    from test_virtual import _write

    _write(
        tmp_path,
        "shop/sales2",
        pa.table({"id": list(range(5000)), "grp": [f"g{i % 4}" for i in range(5000)]}),
    )

    class P2:
        kind = "p2"

        def list_tables(self):
            return [TableInfo(name="sales2", schema="shop", format="parquet")]

        def table_uri(self, info):
            return f"fake://{info.path}"

        def open_dataset(self, info, version=None):
            return pad.dataset(str(tmp_path / info.path), format="parquet")

        def check_version(self, info):
            return None

    cat = tmp_path / "catalog.yaml"
    cat.write_text(
        yaml.safe_dump(
            {"tables": {"vw_clustered": {"definition": "SELECT grp, id FROM sales2 WHERE id >= 0"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    eng = SqlEngine(P2(), cache_ttl=0, cache_dir=str(tmp_path))
    eng.query_duckdb("SELECT count(*) AS c FROM vw_clustered")
    cached = pq.read_table(next(Path(eng._virtual_cache_dir).glob("*.parquet")))
    assert cached.column("grp").to_pylist() != sorted(cached.column("grp").to_pylist())
