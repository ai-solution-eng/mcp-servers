"""Iceberg manifest local-path containment (audit 2026-11-12 P1-12).

A catalog's manifest decides which data files feed a table — that is REMOTE
input. file://, absolute and schemeless paths address the LOCAL filesystem
(pyarrow datasets bypass the DuckDB fs lockdown), so they are refused unless
the operator opts in via SQLHANDLER_ICEBERG_ALLOW_LOCAL_FILES, and even then
only when realpath-contained inside ICEBERG_WAREHOUSE.
"""

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import IcebergConfig, S3Config
from sqlhandler.iceberg import LOCAL_FILES_ENV, IcebergProvider
from sqlhandler.provider import LakehouseError, TableInfo


def _provider(warehouse=""):
    cfg = IcebergConfig(
        catalog_type="sql",
        catalog_uri="sqlite:///tmp/irrelevant.db",
        warehouse=warehouse,
        storage=S3Config(bucket="b", access_key="k", secret_key="s"),
    )
    return IcebergProvider(cfg)


def _off(monkeypatch):
    monkeypatch.delenv(LOCAL_FILES_ENV, raising=False)


class _File:
    def __init__(self, file_path):
        self.file_path = file_path


class _Task:
    def __init__(self, path):
        self.file = _File(path)


class _Scan:
    def __init__(self, paths):
        self._paths = paths

    def plan_files(self):
        return [_Task(p) for p in self._paths]


class _FakeTable:
    def __init__(self, location="", paths=None):
        self._location = location
        self._paths = paths or []

    def location(self):
        return self._location

    def scan(self):
        return _Scan(self._paths)


class _FakeCatalog:
    def __init__(self, tables):
        self._tables = tables

    def load_table(self, ident):
        return self._tables[tuple(ident)]


def test_remote_schemes_pass_through_without_optin(monkeypatch):
    """s3:// passes through; gs:// and abfs(s):// are REFUSED with a clear
    message (audit 2026-11-12, P2: they were advertised but unreadable)."""
    _off(monkeypatch)
    p = _provider(warehouse="s3://wh")
    assert p._normalize_path("s3://wh/db/t/1.parquet") == "s3://wh/db/t/1.parquet"
    import pytest as _pytest

    with _pytest.raises(Exception) as exc:
        p._normalize_path("gs://b/t/1.parquet")
    assert "scheme" in str(exc.value).lower()
    with _pytest.raises(Exception):
        p._normalize_path("abfss://c@acct.dfs.core.windows.net/wh/t/1.parquet")


def test_relative_under_remote_warehouse_passes_through(monkeypatch):
    # the normal Iceberg layout: manifest paths relative to a REMOTE warehouse
    _off(monkeypatch)
    p = _provider(warehouse="s3://wh")
    assert p._normalize_path("db/t1/1.parquet") == "s3://wh/db/t1/1.parquet"


def test_file_scheme_refused_by_default(monkeypatch, tmp_path):
    _off(monkeypatch)
    p = _provider(warehouse=str(tmp_path))
    with pytest.raises(LakehouseError, match="local reads"):
        p._normalize_path("file:///etc/secret.parquet")


def test_absolute_path_refused_by_default(monkeypatch, tmp_path):
    _off(monkeypatch)
    p = _provider(warehouse=str(tmp_path))
    with pytest.raises(LakehouseError, match="local reads"):
        p._normalize_path("/etc/secret.parquet")


def test_schemeless_refused_by_default(monkeypatch, tmp_path):
    _off(monkeypatch)
    p = _provider(warehouse=str(tmp_path))
    with pytest.raises(LakehouseError, match="local reads"):
        p._normalize_path("t/part.parquet")


def test_optin_allows_contained_paths(monkeypatch, tmp_path):
    monkeypatch.setenv(LOCAL_FILES_ENV, "1")
    wh = tmp_path / "wh"
    wh.mkdir()
    p = _provider(warehouse=str(wh))
    inside = str(wh / "t" / "part.parquet")
    assert p._normalize_path(inside) == os.path.realpath(inside)
    # schemeless relative resolves against the warehouse and stays inside
    assert p._normalize_path("t/part.parquet") == os.path.realpath(str(wh / "t" / "part.parquet"))
    # the file:// form of the same contained path
    assert p._normalize_path("file://" + inside) == os.path.realpath(inside)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_optin_truthy_values(monkeypatch, tmp_path, value):
    monkeypatch.setenv(LOCAL_FILES_ENV, value)
    wh = tmp_path / "wh"
    wh.mkdir()
    p = _provider(warehouse=str(wh))
    assert p._normalize_path(str(wh / "a.parquet")) == str(wh / "a.parquet")


@pytest.mark.parametrize("value", ["0", "false", "", "off", "garbage"])
def test_optin_falsy_values_refuse(monkeypatch, tmp_path, value):
    monkeypatch.setenv(LOCAL_FILES_ENV, value)
    p = _provider(warehouse=str(tmp_path))
    with pytest.raises(LakehouseError):
        p._normalize_path(str(tmp_path / "a.parquet"))


def test_optin_still_rejects_warehouse_escape(monkeypatch, tmp_path):
    monkeypatch.setenv(LOCAL_FILES_ENV, "1")
    wh = tmp_path / "wh"
    wh.mkdir()
    p = _provider(warehouse=str(wh))
    with pytest.raises(LakehouseError, match="escapes the warehouse"):
        p._normalize_path("/etc/passwd")
    with pytest.raises(LakehouseError, match="escapes the warehouse"):
        p._normalize_path("file://" + str(tmp_path / "sibling.parquet"))  # not under wh
    with pytest.raises(LakehouseError, match="escapes the warehouse"):
        p._normalize_path(str(wh / ".." / "escape.parquet"))
    # a symlink planted inside the warehouse cannot lead out either
    outside = tmp_path / "outside"
    outside.mkdir()
    link = wh / "link.parquet"
    os.symlink(outside / "real.parquet", link)
    with pytest.raises(LakehouseError, match="escapes the warehouse"):
        p._normalize_path(str(link))


def test_optin_without_warehouse_requires_absolute(monkeypatch):
    monkeypatch.setenv(LOCAL_FILES_ENV, "1")
    p = _provider(warehouse="")
    assert p._normalize_path("/tmp/absolute.parquet") == "/tmp/absolute.parquet"
    with pytest.raises(LakehouseError, match="ICEBERG_WAREHOUSE"):
        p._normalize_path("relative.parquet")


def test_open_dataset_local_manifest_refused_by_default(monkeypatch, tmp_path):
    _off(monkeypatch)
    data_dir = tmp_path / "t"
    data_dir.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2]}), data_dir / "part.parquet")
    table = _FakeTable(paths=[str(data_dir / "part.parquet")])
    p = _provider(warehouse=str(tmp_path))
    p._cat = _FakeCatalog({("db", "t"): table})
    with pytest.raises(LakehouseError, match="local reads"):
        p.open_dataset(TableInfo(name="t", schema="db"))


def test_open_dataset_optin_reads_contained_warehouse(monkeypatch, tmp_path):
    monkeypatch.setenv(LOCAL_FILES_ENV, "1")
    wh = tmp_path / "wh"
    data_dir = wh / "t"
    data_dir.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2]}), data_dir / "part.parquet")
    table = _FakeTable(location=str(data_dir), paths=[str(data_dir / "part.parquet")])
    p = _provider(warehouse=str(wh))
    p._cat = _FakeCatalog({("db", "t"): table})
    ds = p.open_dataset(TableInfo(name="t", schema="db"))
    assert ds.to_table().num_rows == 2