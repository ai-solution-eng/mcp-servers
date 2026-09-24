"""Tests for the ADLS Gen2 and GCS backends.

No network, no live Azure/GCS: listing is driven by fake pyarrow filesystem
objects (the test_s3_delta.py / test_raw_formats.py pattern) injected onto
the providers' ``_fs`` handle, and the deltalake DeltaTable constructor is
monkeypatched to capture how each provider dials its object store.

Covered, mirroring the s3 backend's guarantees:
  * config parsing (env-only; missing → clear LakehouseError; the
    ADLS_CLIENT_SECRET_ENV indirection; ADLS_AUTH validation)
  * discovery parity with s3 (file→table, folder→table, schema folders,
    partition-fold, hidden-skip, Delta via _delta_log, raw landing zone)
  * make_provider dispatch (AdlsConfig/GcsConfig → the right provider)
  * open_dataset over a fake-fs parquet listing (dataset built on the fs)
  * credential handling: the secret resolves from the env-var NAME, the
    storage options carry it, and it is scrubbed from raised errors
  * the negative: ADLS_AUTH=client-secret with the named env var unset →
    a loud startup error naming the missing variable
"""

import logging
import os

import pyarrow.fs as pafs
import pytest

from sqlhandler.adls import AdlsProvider, build_adls_fs
from sqlhandler.config import AdlsConfig, GcsConfig, load_adls_config, load_backend_config, load_gcs_config
from sqlhandler.gcs import GcsProvider, build_gcs_fs, gcs_delta_storage_options
from sqlhandler.provider import LakehouseError, TableInfo, make_provider

# ------------------------------------------------------------- test doubles


class FakeFileInfo:
    def __init__(self, path, size=10, is_file=True):
        self.path = path
        self.type = pafs.FileType.File if is_file else pafs.FileType.Directory
        self.size = size
        self.size_bytes = size


class FakeObjectFS:
    """Minimal stand-in for pyarrow's AzureFileSystem/GcsFileSystem.

    Returns a canned listing filtered by selector base_dir — the same shape
    test_raw_formats.py's _FakeS3FS gives the s3 provider, so the ADLS/GCS
    walk is exercised against the identical fake-filesystem contract.
    Paths are ABSOLUTE object-store paths (container/bucket included),
    exactly what the real filesystems' get_file_info returns.
    """

    def __init__(self, entries):
        # entries: list[FakeFileInfo] (or list[str] -> files of size 10)
        self.entries = [FakeFileInfo(p) if isinstance(p, str) else p for p in entries]

    def get_file_info(self, selector):
        base = selector.base_dir if selector.base_dir != "/" else ""
        if not base:
            return list(self.entries)
        return [e for e in self.entries if e.path == base or e.path.startswith(base + "/")]


class _FakeDeltaTable:
    calls: list = []  # noqa: RUF012 - test double, list reset per test

    def __init__(self, uri, version=None, storage_options=None):
        _FakeDeltaTable.calls.append({"uri": uri, "version": version, "storage_options": storage_options})
        self._dataset = f"dataset-for-{uri}-v{version}"

    def to_pyarrow_dataset(self):
        return self._dataset


def _patch_delta(monkeypatch) -> None:
    """Point the deltalake import the providers make inside _open_delta at
    _FakeDeltaTable (the sys.modules pattern test_s3_delta.py uses)."""
    import sys
    import types

    fake = types.ModuleType("deltalake")
    fake.DeltaTable = _FakeDeltaTable
    monkeypatch.setitem(sys.modules, "deltalake", fake)


# ------------------------------------------------------------------- config


def test_load_adls_config_env_only(monkeypatch):
    for var in (
        "ADLS_ACCOUNT", "ADLS_CONTAINER", "ADLS_PREFIX", "ADLS_AUTH", "ADLS_TENANT_ID",
        "ADLS_CLIENT_ID", "ADLS_CLIENT_SECRET_ENV", "ADLS_ENDPOINT_SUFFIX",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_adls_config(
        {
            "ADLS_ACCOUNT": "acct",
            "ADLS_CONTAINER": "cont",
            "ADLS_PREFIX": "lake",
            "ADLS_AUTH": "client-secret",
            "ADLS_TENANT_ID": "t",
            "ADLS_CLIENT_ID": "c",
            "ADLS_CLIENT_SECRET_ENV": "ADLS_TEST_SECRET",
            "ADLS_TEST_SECRET": "s3cr3t",
        }
    )
    assert (cfg.account, cfg.container, cfg.prefix) == ("acct", "cont", "lake")
    assert cfg.auth == "client-secret"
    assert cfg.client_secret == "s3cr3t"
    assert cfg.client_secret_env == "ADLS_TEST_SECRET"
    assert cfg.is_configured
    assert cfg.dfs_authority == "acct.dfs.core.windows.net"


def test_load_adls_config_anon_defaults(monkeypatch):
    monkeypatch.delenv("ADLS_AUTH", raising=False)
    cfg = load_adls_config({"ADLS_ACCOUNT": "acct", "ADLS_CONTAINER": "cont"})
    assert cfg.auth == "anon" and cfg.client_secret == ""
    assert cfg.is_configured


def test_load_adls_config_filesystem_alias():
    cfg = load_adls_config({"ADLS_ACCOUNT": "a", "ADLS_FILESYSTEM": "fs1"})
    assert cfg.container == "fs1"


def test_load_adls_config_sovereign_suffix():
    cfg = load_adls_config(
        {"ADLS_ACCOUNT": "a", "ADLS_CONTAINER": "c", "ADLS_ENDPOINT_SUFFIX": "core.usgovcloudapi.net"}
    )
    assert cfg.dfs_authority == "a.dfs.core.usgovcloudapi.net"
    assert cfg.blob_authority == "a.blob.core.usgovcloudapi.net"


def test_load_adls_config_invalid_auth_is_loud():
    with pytest.raises(LakehouseError, match="Invalid ADLS_AUTH"):
        load_adls_config({"ADLS_ACCOUNT": "a", "ADLS_CONTAINER": "c", "ADLS_AUTH": "basic"})


def test_load_adls_config_missing_secret_env_is_loud():
    with pytest.raises(LakehouseError, match="MISSING_SECRET_VAR.*is not set"):
        load_adls_config(
            {
                "ADLS_ACCOUNT": "a",
                "ADLS_CONTAINER": "c",
                "ADLS_AUTH": "client-secret",
                "ADLS_CLIENT_SECRET_ENV": "MISSING_SECRET_VAR",
            }
        )


def test_load_adls_config_missing_required_is_unconfigured():
    cfg = load_adls_config({"ADLS_ACCOUNT": "a"})  # no container
    assert not cfg.is_configured


def test_load_adls_config_client_secret_without_trio_is_unconfigured():
    cfg = load_adls_config({"ADLS_ACCOUNT": "a", "ADLS_CONTAINER": "c", "ADLS_AUTH": "client-secret"})
    assert not cfg.is_configured  # no tenant/client/secret


def test_load_gcs_config_env_only():
    cfg = load_gcs_config(
        {"GCS_BUCKET": "bkt", "GCS_PREFIX": "lake", "GCS_CREDENTIALS_FILE": "/mnt/key.json"}
    )
    assert (cfg.bucket, cfg.prefix, cfg.credentials_file) == ("bkt", "lake", "/mnt/key.json")
    assert not cfg.anonymous and cfg.is_configured


def test_load_gcs_config_anonymous(monkeypatch):
    monkeypatch.delenv("GCS_ANONYMOUS", raising=False)
    cfg = load_gcs_config({"GCS_BUCKET": "pub", "GCS_ANONYMOUS": "true"})
    assert cfg.anonymous and cfg.is_configured


def test_load_gcs_config_missing_bucket_is_unconfigured():
    assert not load_gcs_config({"GCS_PREFIX": "x"}).is_configured


def test_backend_selection_adls_gcs():
    name, cfg = load_backend_config({"SQLHANDLER_BACKEND": "adls", "ADLS_ACCOUNT": "a", "ADLS_CONTAINER": "c"})
    assert name == "adls" and isinstance(cfg, AdlsConfig)
    name, cfg = load_backend_config({"SQLHANDLER_BACKEND": "gcs", "GCS_BUCKET": "b"})
    assert name == "gcs" and isinstance(cfg, GcsConfig)


# ----------------------------------------------------------------- dispatch


def test_make_provider_dispatch_adls_gcs():
    assert isinstance(make_provider(AdlsConfig(account="a", container="c")), AdlsProvider)
    assert isinstance(make_provider(GcsConfig(bucket="b")), GcsProvider)


def test_providers_reject_unconfigured():
    with pytest.raises(LakehouseError, match="ADLS_ACCOUNT"):
        AdlsProvider(AdlsConfig())
    with pytest.raises(LakehouseError, match="GCS_BUCKET"):
        GcsProvider(GcsConfig())
    # client-secret mode without the trio is unconfigured too
    with pytest.raises(LakehouseError, match="ADLS"):
        AdlsProvider(AdlsConfig(account="a", container="c", auth="client-secret", client_id="only"))


# ---------------------------------------------------------------- discovery

# Every fake path below is an ABSOLUTE object-store path: ADLS entries live
# under <container>/..., GCS entries under <bucket>/... — the shape the real
# filesystems hand back from get_file_info.

ADLS_PATHS = {
    "single": ["cont/lake/orders.parquet"],
    "folder": ["cont/lake/customers/part-0001.parquet"],
    "schema": ["cont/lake/sales/customers/part-0001.parquet"],
    "partitions": [
        "cont/lake/sales/customers/dt=2024/x.parquet",
        "cont/lake/sales/customers/dt=2025/y.parquet",
        "cont/lake/.hidden/z.parquet",
    ],
    "delta": [
        "cont/lake/dt_tbl/_delta_log/00000000000000000000.json",
        "cont/lake/dt_tbl/part-0001.parquet",
        "cont/lake/plain/part.parquet",
        "cont/lake/single.parquet",
    ],
    "raw": [
        "cont/lake/orders.csv",
        "cont/lake/sales/customers.csv",
        "cont/lake/sales/events/a.json",
        "cont/lake/mix/x.parquet",
        "cont/lake/mix/y.csv",
    ],
    "gz": ["cont/lake/packed.csv.gz"],
}
GCS_PATHS = {
    "single": ["bkt/lake/orders.parquet"],
    "folder": ["bkt/lake/customers/part-0001.parquet"],
    "schema": ["bkt/lake/sales/customers/part-0001.parquet"],
    "partitions": [
        "bkt/lake/sales/customers/dt=2024/x.parquet",
        "bkt/lake/sales/customers/dt=2025/y.parquet",
        "bkt/lake/.hidden/z.parquet",
    ],
    "delta": [
        "bkt/lake/dt_tbl/_delta_log/00000000000000000000.json",
        "bkt/lake/dt_tbl/part.parquet",
    ],
    "raw": [
        "bkt/lake/orders.csv",
        "bkt/lake/sales/customers.csv",
        "bkt/lake/sales/events/a.json",
        "bkt/lake/mix/x.parquet",
        "bkt/lake/mix/y.csv",
    ],
    "gz": ["bkt/lake/packed.csv.gz"],
}


def _adls_provider(paths, prefix="lake"):
    prov = AdlsProvider(AdlsConfig(account="acct", container="cont", prefix=prefix))
    prov._fs = FakeObjectFS(paths)
    return prov


def _gcs_provider(paths, prefix="lake"):
    prov = GcsProvider(GcsConfig(bucket="bkt", prefix=prefix))
    prov._fs = FakeObjectFS(paths)
    return prov


@pytest.mark.parametrize(
    "paths,expected",
    [
        # single file at prefix root -> table by stem, default schema
        ("single", {"orders": ("default", "orders", "parquet")}),
        # folder -> table named by the folder
        ("folder", {"customers": ("default", "customers", "parquet")}),
        # schema folder -> schema/name
        ("schema", {"sales/customers": ("sales", "customers", "parquet")}),
        # Hive partition dirs fold into the table; hidden dirs are skipped
        ("partitions", {"sales/customers": ("sales", "customers", "parquet")}),
    ],
)
@pytest.mark.parametrize("backend", ["adls", "gcs"])
def test_discovery_parity_with_s3(backend, paths, expected):
    src = ADLS_PATHS if backend == "adls" else GCS_PATHS
    prov = _adls_provider(src[paths]) if backend == "adls" else _gcs_provider(src[paths])
    tables = {t.path: t for t in prov.list_tables()}
    assert set(tables) == set(expected)
    for path, (schema, name, fmt) in expected.items():
        ti = tables[path]
        assert (ti.schema, ti.name, ti.format) == (schema, name, fmt)


def test_adls_detects_delta_and_hides_its_data_files():
    prov = _adls_provider(ADLS_PATHS["delta"])
    tables = {t.path: t for t in prov.list_tables()}
    assert tables["dt_tbl"].format == "delta"
    assert tables["plain"].format == "parquet"
    assert tables["single"].format == "parquet"
    assert "dt_tbl/_delta_log" not in tables


def test_gcs_detects_delta_and_hides_its_data_files():
    prov = _gcs_provider(GCS_PATHS["delta"])
    tables = {t.path: t for t in prov.list_tables()}
    assert tables["dt_tbl"].format == "delta"
    assert "dt_tbl/_delta_log" not in tables


def test_delta_log_json_never_becomes_a_raw_table(monkeypatch):
    # Same regression the s3 backend fixed: _delta_log commit JSONs are table
    # METADATA, not raw landing-zone files.
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "0")
    for make_prov, paths in (
        (_adls_provider, ADLS_PATHS["delta"]),
        (_gcs_provider, GCS_PATHS["delta"]),
    ):
        prov = make_prov(paths)
        tables = prov.list_tables()
        assert any(t.format == "delta" for t in tables)
        assert not any("_delta_log" in t.path for t in tables)


def test_raw_landing_zone_same_rules(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "0")
    for prov in (_adls_provider(ADLS_PATHS["raw"]), _gcs_provider(GCS_PATHS["raw"])):
        tables = {t.path: t for t in prov.list_tables()}
        assert set(tables) == {"orders", "sales", "sales/events", "mix"}
        assert tables["orders"].format == "csv"
        assert tables["sales/events"].format == "json"
        assert tables["mix"].format == "parquet"  # parquet wins the mixed folder


def test_raw_gz_stem_drops_full_suffix(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "0")
    for prov in (_adls_provider(ADLS_PATHS["gz"]), _gcs_provider(GCS_PATHS["gz"])):
        tables = prov.list_tables()
        assert tables[0].name == "packed" and tables[0].format == "csv.gz"


def test_raw_size_cap_skips_whole_table(monkeypatch, caplog):
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "1")
    for backend, root in (("adls", "cont"), ("gcs", "bkt")):
        caplog.clear()
        provider_cls = _adls_provider if backend == "adls" else _gcs_provider
        prov = provider_cls(
            [
                FakeFileInfo(f"{root}/lake/huge/a.csv", size=2 * 1024 * 1024),
                FakeFileInfo(f"{root}/lake/small.csv", size=100),
            ]
        )
        with caplog.at_level(logging.INFO, logger="sqlhandler.rawfiles"):
            tables = prov.list_tables()
        assert [t.path for t in tables] == ["small"]
        assert any("huge" in r.message for r in caplog.records)


def test_raw_formats_off_hides_raw_tables(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RAW_FORMATS", "off")
    for prov in (_adls_provider(ADLS_PATHS["raw"]), _gcs_provider(GCS_PATHS["raw"])):
        tables = {t.path: t for t in prov.list_tables()}
        assert set(tables) == {"mix"}


# -------------------------------------------------------------- open paths


def _local_provider(tmp_path, backend):
    """A provider whose fs slot holds a REAL pyarrow filesystem rooted at
    tmp_path via SubTreeFileSystem — the provider's path arithmetic
    (container/prefix/table) is what's under test; the subtree root makes
    the container-relative paths resolve under tmp_path exactly like the
    real object stores resolve them under the account root.
    """
    if backend == "adls":
        prov = AdlsProvider(AdlsConfig(account="acct", container="cont", prefix="lake"))
        root = tmp_path / "cont" / "lake"
    else:
        prov = GcsProvider(GcsConfig(bucket="bkt", prefix="lake"))
        root = tmp_path / "bkt" / "lake"
    root.mkdir(parents=True, exist_ok=True)
    prov._fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    return prov, root


def _rebase_location(prov, info):
    """Point a TableInfo at the tmp-relative prefix the LocalFileSystem sees.

    The provider builds '<container>/<prefix>/<location>' from the config;
    under LocalFileSystem the container segment must exist on disk, so the
    fake layout creates tmp_path/<container>/<prefix>/ and the location is
    used as-is (container = a plain directory here).
    """
    return info


def test_adls_open_parquet_over_local_fs(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    prov, root = _local_provider(tmp_path, "adls")
    pq.write_table(pa.table({"id": [1, 2, 3], "name": ["x", "y", "z"]}), root / "orders.parquet")
    info = TableInfo(name="orders", schema="default", format="parquet", location="orders.parquet")
    ds = prov.open_dataset(info)
    assert ds.to_table().to_pydict() == {"id": [1, 2, 3], "name": ["x", "y", "z"]}
    # traversal is refused before any IO
    with pytest.raises(LakehouseError, match="Invalid ADLS table location"):
        prov.open_dataset(TableInfo(name="evil", location="../secret.parquet"))


def test_gcs_open_parquet_over_local_fs(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    prov, root = _local_provider(tmp_path, "gcs")
    pq.write_table(pa.table({"id": [4, 5]}), root / "items.parquet")
    info = TableInfo(name="items", schema="default", format="parquet", location="items.parquet")
    assert prov.open_dataset(info).to_table().to_pydict() == {"id": [4, 5]}
    with pytest.raises(LakehouseError, match="Invalid GCS table location"):
        prov.open_dataset(TableInfo(name="evil", location="../secret.parquet"))


def test_adls_open_raw_over_local_fs(tmp_path):
    prov, root = _local_provider(tmp_path, "adls")
    (root / "events.jsonl").write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
    info = TableInfo(name="events", schema="default", format="jsonl", location="events.jsonl")
    object.__setattr__(info, "raw_files", ["cont/lake/events.jsonl"])
    assert prov.open_dataset(info).to_table().num_rows == 2
    with pytest.raises(LakehouseError, match="not supported"):
        prov.open_dataset(info, version=0)


def test_gcs_open_raw_over_local_fs(tmp_path):
    prov, root = _local_provider(tmp_path, "gcs")
    (root / "events.csv").write_text("id,v\n1,x\n", encoding="utf-8")
    info = TableInfo(name="events", schema="default", format="csv", location="events.csv")
    object.__setattr__(info, "raw_files", ["bkt/lake/events.csv"])
    assert prov.open_dataset(info).to_table().to_pydict() == {"id": [1], "v": ["x"]}


def test_parquet_time_travel_refused_on_both():
    for prov in (_adls_provider([]), _gcs_provider([])):
        with pytest.raises(LakehouseError, match="not supported"):
            prov.open_dataset(TableInfo(name="plain", format="parquet"), version=1)


def test_table_uri_shapes():
    adls = _adls_provider([])
    info = TableInfo(name="orders", schema="sales", format="parquet", location="sales/orders")
    assert adls.table_uri(info) == "abfs://cont@acct.dfs.core.windows.net/sales/orders"
    gcs = _gcs_provider([])
    assert gcs.table_uri(info) == "gs://bkt/lake/sales/orders"


# -------------------------------------------------------------------- delta


def test_adls_delta_routes_with_storage_options(monkeypatch):
    _patch_delta(monkeypatch)
    prov = AdlsProvider(
        AdlsConfig(
            account="acct",
            container="cont",
            auth="client-secret",
            tenant_id="t",
            client_id="c",
            client_secret="s3cr3t",
        )
    )
    prov._fs = FakeObjectFS([])
    info = TableInfo(name="orders", schema="sales", format="delta", location="sales/orders")
    ds = prov.open_dataset(info)
    call = _FakeDeltaTable.calls[-1]
    assert call["uri"] == "abfs://cont@acct.dfs.core.windows.net/sales/orders"
    opts = call["storage_options"]
    assert opts["account_name"] == "acct"
    assert opts["azure_tenant_id"] == "t"
    assert opts["azure_client_id"] == "c"
    assert opts["azure_client_secret"] == "s3cr3t"
    assert opts["dfs_endpoint"] == "acct.dfs.core.windows.net"
    assert ds == f"dataset-for-{call['uri']}-vNone"


def test_adls_delta_anon_omits_credentials(monkeypatch):
    _patch_delta(monkeypatch)
    prov = AdlsProvider(AdlsConfig(account="acct", container="cont"))
    prov._fs = FakeObjectFS([])
    prov.open_dataset(TableInfo(name="pub", format="delta"))
    opts = _FakeDeltaTable.calls[-1]["storage_options"]
    assert "azure_client_secret" not in opts and "azure_tenant_id" not in opts


def test_adls_delta_version_and_sovereign(monkeypatch):
    _patch_delta(monkeypatch)
    cfg = AdlsConfig(
        account="acct",
        container="cont",
        auth="client-secret",
        tenant_id="t",
        client_id="c",
        client_secret="s",
        endpoint_suffix="core.chinacloudapi.cn",
    )
    prov = AdlsProvider(cfg)
    prov._fs = FakeObjectFS([])
    info = TableInfo(name="orders", format="delta")
    ds = prov.open_dataset(info, version=3)
    call = _FakeDeltaTable.calls[-1]
    assert call["version"] == 3
    assert call["uri"].endswith("acct.dfs.core.chinacloudapi.cn/orders")
    assert ds == f"dataset-for-{call['uri']}-v3"


def test_gcs_delta_routes_with_storage_options(monkeypatch, tmp_path):
    _patch_delta(monkeypatch)
    key_file = tmp_path / "key.json"
    key_file.write_text("{}", encoding="utf-8")
    prov = GcsProvider(GcsConfig(bucket="bkt", credentials_file=str(key_file)))
    prov._fs = FakeObjectFS([])
    info = TableInfo(name="orders", schema="sales", format="delta", location="sales/orders")
    ds = prov.open_dataset(info)
    call = _FakeDeltaTable.calls[-1]
    assert call["uri"] == "gs://bkt/sales/orders"
    assert call["storage_options"]["google_service_account"] == str(key_file)
    assert ds == f"dataset-for-{call['uri']}-vNone"


def test_gcs_delta_anonymous_skip_signature(monkeypatch):
    _patch_delta(monkeypatch)
    prov = GcsProvider(GcsConfig(bucket="bkt", anonymous=True))
    prov._fs = FakeObjectFS([])
    prov.open_dataset(TableInfo(name="pub", format="delta"))
    assert _FakeDeltaTable.calls[-1]["storage_options"]["skip_signature"] == "true"


def test_delta_version_from_log_keys():
    for make_prov, paths in (
        (_adls_provider, ADLS_PATHS["delta"]),
        (_gcs_provider, GCS_PATHS["delta"]),
    ):
        prov = make_prov(paths)
        info = TableInfo(name="dt_tbl", format="delta", location="dt_tbl")
        assert prov.check_version(info) == 0
        assert prov.check_version(TableInfo(name="plain", format="parquet")) is None


def test_delta_version_counts_numeric_commits():
    for make_prov, root in ((_adls_provider, "cont"), (_gcs_provider, "bkt")):
        prov = make_prov([])
        prov._fs = FakeObjectFS(
            [
                f"{root}/lake/orders/_delta_log/00000000000000000000.json",
                f"{root}/lake/orders/_delta_log/00000000000000000004.json",
                f"{root}/lake/orders/_delta_log/00000000000000000004.checkpoint.parquet",
            ]
        )
        info = TableInfo(name="orders", format="delta", location="orders")
        assert prov.check_version(info) == 4


# ------------------------------------------------------- delta block cache


class _FakeDTForCache:
    """DeltaTable double with add-action stats for the cache-wrap tests."""

    def __init__(self, version=7):
        self._version = version

    def version(self):
        return self._version

    def get_add_actions(self, flatten=True):
        import pyarrow as pa

        return pa.table({"path": ["part-0.parquet"], "size_bytes": [1234]})


def test_adls_delta_block_cache_wrap(monkeypatch):
    # Cache ON: the DeltaStorageHandler rebuild routes data-file reads through
    # the cache (the onelake pattern) — asserted via the wrapped PyFileSystem
    # handle _delta_data_fs hands back; cache OFF returns None so delta-rs's
    # built-in path stays in place.
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE_DIR", "/tmp/sqlhandler-test-adls-bc")
    try:
        prov = AdlsProvider(
            AdlsConfig(
                account="acct",
                container="cont",
                auth="client-secret",
                tenant_id="t",
                client_id="c",
                client_secret="s",
            )
        )
        info = TableInfo(name="orders", format="delta", location="orders")
        dt = _FakeDTForCache(version=7)
        fs = prov._delta_data_fs(dt, info, None)
        assert fs is not None
        assert type(fs).__name__ == "PyFileSystem"
        monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "0")
        assert prov._delta_data_fs(dt, info, None) is None
    finally:
        monkeypatch.delenv("SQLHANDLER_BLOCK_CACHE", raising=False)


def test_gcs_delta_block_cache_wrap(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "1")
    monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE_DIR", "/tmp/sqlhandler-test-gcs-bc")
    try:
        prov = GcsProvider(GcsConfig(bucket="bkt"))
        info = TableInfo(name="orders", format="delta", location="orders")
        dt = _FakeDTForCache(version=3)
        fs = prov._delta_data_fs(dt, info, 3)
        assert fs is not None
        monkeypatch.setenv("SQLHANDLER_BLOCK_CACHE", "0")
        assert prov._delta_data_fs(dt, info, None) is None
    finally:
        monkeypatch.delenv("SQLHANDLER_BLOCK_CACHE", raising=False)


# ------------------------------------------------- filesystem constructors


def test_build_adls_fs_credential_shapes():
    anon = build_adls_fs(AdlsConfig(account="a", container="c"))
    assert isinstance(anon, pafs.AzureFileSystem)
    sec = build_adls_fs(
        AdlsConfig(account="a", container="c", auth="client-secret", tenant_id="t", client_id="c", client_secret="s")
    )
    assert isinstance(sec, pafs.AzureFileSystem)


def test_build_adls_fs_sovereign_authorities():
    fs = build_adls_fs(AdlsConfig(account="a", container="c", endpoint_suffix="core.usgovcloudapi.net"))
    assert isinstance(fs, pafs.AzureFileSystem)


def test_build_adls_fs_options_override_bad_json(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_ADLS_OPTIONS", "{not json")
    with pytest.raises(LakehouseError, match="SQLHANDLER_ADLS_OPTIONS"):
        build_adls_fs(AdlsConfig(account="a", container="c"))


def test_build_gcs_fs_shapes(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    anon = build_gcs_fs(GcsConfig(bucket="b", anonymous=True))
    assert isinstance(anon, pafs.GcsFileSystem)
    # construction is lazy in pyarrow 25: no credential file needed to build
    plain = build_gcs_fs(GcsConfig(bucket="b"))
    assert isinstance(plain, pafs.GcsFileSystem)


def test_build_gcs_fs_sets_credentials_env(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    build_gcs_fs(GcsConfig(bucket="b", credentials_file=str(key)))
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(key)


def test_build_gcs_fs_existing_credentials_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/ambient/path.json")
    build_gcs_fs(GcsConfig(bucket="b", credentials_file=str(tmp_path / "other.json")))
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/ambient/path.json"


def test_build_gcs_fs_bad_options_json(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_GCS_OPTIONS", "{bad")
    with pytest.raises(LakehouseError, match="SQLHANDLER_GCS_OPTIONS"):
        build_gcs_fs(GcsConfig(bucket="b"))


def test_gcs_delta_storage_options_shapes(tmp_path):
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    opts = gcs_delta_storage_options(GcsConfig(bucket="b", credentials_file=str(key)))
    assert opts == {"google_service_account": str(key)}
    assert gcs_delta_storage_options(GcsConfig(bucket="b", anonymous=True)) == {"skip_signature": "true"}


# ------------------------------------------------------ secret scrubbing


class _BoomFS:
    def __init__(self, message):
        self.message = message

    def get_file_info(self, selector):
        raise RuntimeError(self.message)


def _secret_provider():
    return AdlsProvider(
        AdlsConfig(
            account="a",
            container="c",
            auth="client-secret",
            tenant_id="t",
            client_id="c",
            client_secret="s3cr3t",
        )
    )


def test_error_scrubbing_on_list_failure():
    prov = _secret_provider()
    prov._fs = _BoomFS("request failed: Bearer s3cr3t rejected")
    with pytest.raises(LakehouseError) as excinfo:
        prov.list_tables()
    assert "s3cr3t" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_check_connection_reports_error_scrubbed():
    prov = _secret_provider()
    prov._fs = _BoomFS("connection refused (secret=s3cr3t)")
    err = prov.check_connection()
    assert err is not None and "s3cr3t" not in err


def test_open_dataset_error_scrubbed(tmp_path):

    prov, root = _local_provider(tmp_path, "adls")
    # a corrupt parquet makes pad.dataset raise with an opaque message
    (root / "broken.parquet").write_bytes(b"not parquet at all")
    prov.config = AdlsConfig(
        account="acct",
        container="cont",
        prefix="lake",
        auth="client-secret",
        tenant_id="t",
        client_id="c",
        client_secret="s3cr3t",
    )
    info = TableInfo(name="broken", schema="default", format="parquet", location="broken.parquet")
    with pytest.raises(LakehouseError) as excinfo:
        prov.open_dataset(info)
    assert "s3cr3t" not in str(excinfo.value)


# ------------------------------------------------------- engine end-to-end


def test_engine_run_sql_over_adls_gcs_tables(tmp_path):
    """E2E: SqlEngine over each provider — run_sql, describe, profile.

    The engine is provider-agnostic, so the ADLS/GCS providers must slot in
    with zero engine changes: registration (schema/name qualification),
    predicate pushdown through the pyarrow dataset, and the metadata surfaces
    all behave exactly like the s3/file backends.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    from sqlhandler.engine import SqlEngine

    for backend in ("adls", "gcs"):
        prov, root = _local_provider(tmp_path, backend)
        pq.write_table(
            pa.table({"id": [1, 2, 3, 4], "region": ["e", "e", "w", "w"]}),
            root / "orders.parquet",
        )
        eng = SqlEngine(prov, cache_ttl=0)
        result = eng.query_duckdb(
            "SELECT id FROM orders WHERE region = 'e' ORDER BY id"
        )
        assert result.to_pydict() == {"id": [1, 2]}, backend
        desc = eng.describe_table("orders")
        assert desc["uri"].endswith("orders.parquet") and desc["n_columns"] == 2, backend
        prof = eng.profile_table("orders")
        assert prof["n_rows"] == 4 and {"id", "region"} <= {c["name"] for c in prof["columns"]}
        # pushdown correctness across both providers' datasets
        n = eng.query_duckdb("SELECT count(*) AS n FROM orders").to_pydict()["n"][0]
        assert n == 4
