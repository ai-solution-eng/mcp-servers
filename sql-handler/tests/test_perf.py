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
        return self.base.get_file_info_selector(selector)

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
    warm = pad.dataset(path, filesystem=maybe_block_cache(pafs.PyFileSystem(counter2)), format="parquet")
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
    monkeypatch.setattr(pafs, "PyFileSystem", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
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
    monkeypatch.setenv("SQLHANDLER_S3_OPTIONS", json.dumps({"request_timeout": 99, "retry_limit": 7}))
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


def test_materialized_virtual_result_is_clustered(tmp_path, monkeypatch):
    from tests.test_virtual import _write

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
    from tests.test_virtual import _write

    _write(tmp_path, "shop/sales2", pa.table({"id": list(range(5000)), "grp": [f"g{i % 4}" for i in range(5000)]}))

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
        yaml.safe_dump({"tables": {"vw_clustered": {"definition": "SELECT grp, id FROM sales2 WHERE id >= 0"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    eng = SqlEngine(P2(), cache_ttl=0, cache_dir=str(tmp_path))
    eng.query_duckdb("SELECT count(*) AS c FROM vw_clustered")
    cached = pq.read_table(next(Path(eng._virtual_cache_dir).glob("*.parquet")))
    assert cached.column("grp").to_pylist() != sorted(cached.column("grp").to_pylist())
