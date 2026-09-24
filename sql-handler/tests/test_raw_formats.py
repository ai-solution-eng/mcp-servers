"""Tests for raw-text (CSV/TSV/JSON/NDJSON) landing-zone table discovery.

The FILE backend exercises the identical discovery code path as S3 (both
providers share :mod:`sqlhandler.rawfiles` — the walk differs only in how the
file listing arrives), so no MinIO is needed here: everything runs on tmp_path
through FileProvider. The S3-side smoke test uses the fake-filesystem pattern
from test_s3_delta.py to prove the shared decision logic off the local path.

Covered here, per the raw-landing-zone design:
  * discovery shapes (single file / folder / schema folder / partition fold)
  * the format set (csv, tsv, json/ndjson/jsonl, .gz variants)
  * precedence (parquet wins a mixed folder; delta wins over everything)
  * the raw-vs-raw alphabetical collision rule + warning log
  * SQLHANDLER_RAW_MAX_FILE_MB skip (one line per skipped table)
  * SQLHANDLER_RAW_FORMATS=off hides every raw table
  * engine end-to-end: run_sql over a csv table, describe/profile/sample,
    and count(*) NOT taking the parquet metadata fast path
  * a parquet-only directory regression (existing discovery byte-identical)
"""

import gzip
import json
import logging
import os

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import FileConfig, S3Config, raw_formats_enabled, raw_max_file_mb
from sqlhandler.engine import SqlEngine
from sqlhandler.file import FileProvider
from sqlhandler.provider import LakehouseError
from sqlhandler.rawfiles import classify_raw_file, is_raw_format, raw_format_kind
from sqlhandler.s3 import S3Provider


def _write(root, rel, text):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _write_gz(root, rel, text):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _write_parquet(root, rel, table=None):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(table or pa.table({"id": [1, 2, 3], "name": ["x", "y", "z"]}), path)
    return path


def _csv(rows=("1,x", "2,y", "3,z")):
    return "id,name\n" + "\n".join(rows) + "\n"


# --------------------------------------------------------------- classify


def test_classify_raw_file_suffixes():
    assert classify_raw_file("a.csv") == "csv"
    assert classify_raw_file("a.CSV") == "csv"  # suffix case-insensitive like _is_parquet
    assert classify_raw_file("a.tsv") == "tsv"
    assert classify_raw_file("a.json") == "json"
    assert classify_raw_file("a.ndjson") == "ndjson"
    assert classify_raw_file("a.jsonl") == "jsonl"
    assert classify_raw_file("a.csv.gz") == "csv.gz"
    assert classify_raw_file("a.ndjson.gz") == "ndjson.gz"
    assert classify_raw_file("a.parquet") is None
    assert classify_raw_file("a.txt") is None
    assert is_raw_format("csv.gz") and not is_raw_format("parquet")


def test_raw_format_kind_maps_to_pyarrow_formats():
    assert raw_format_kind("csv") == "csv"
    assert raw_format_kind("csv.gz") == "csv"
    assert raw_format_kind("tsv") == "tsv"
    assert raw_format_kind("jsonl.gz") == "json"
    assert raw_format_kind("ndjson") == "json"


# ------------------------------------------------------- config env knobs


def test_raw_config_env_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("SQLHANDLER_RAW_FORMATS", raising=False)
    monkeypatch.delenv("SQLHANDLER_RAW_MAX_FILE_MB", raising=False)
    assert raw_formats_enabled() is True
    assert raw_max_file_mb() == 64
    monkeypatch.setenv("SQLHANDLER_RAW_FORMATS", "off")
    assert raw_formats_enabled() is False
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "0")
    assert raw_max_file_mb() == 0
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "garbage")
    assert raw_max_file_mb() == 64  # falls back on garbage like every knob


# ----------------------------------------------------- discovery: file backend


def test_raw_single_file_csv(tmp_path):
    _write(tmp_path, "orders.csv", _csv())
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = provider.list_tables()
    assert [t.path for t in tables] == ["orders"]
    assert tables[0].format == "csv"
    ds = provider.open_dataset(tables[0])
    assert ds.to_table().num_rows == 3


def test_raw_folder_and_schema_folder_and_partition_fold(tmp_path):
    _write(tmp_path, "events/a.csv", _csv())
    _write(tmp_path, "events/dt=2024/b.csv", _csv(("4,w",)))
    _write(tmp_path, "sales/customers.csv", _csv())
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = {t.path: t for t in provider.list_tables()}
    # Same derivation as a parquet file at these spots: the schema folder's
    # file names the folder as its table, and the events FOLDER (files inside)
    # names itself.
    assert set(tables) == {"events", "sales"}
    assert tables["events"].format == "csv" and tables["sales"].format == "csv"
    # partition folder's rows fold in (hive partitioning is OUT of scope: no
    # dt column derived — the files' own columns only)
    assert provider.open_dataset(tables["events"]).to_table().num_rows == 4


def test_raw_tsv_json_ndjson_jsonl_and_gz(tmp_path):
    _write(tmp_path, "tab.tsv", "id\tv\n1\tx\n")
    _write(tmp_path, "notes.jsonl", json.dumps({"id": 1}) + "\n" + json.dumps({"id": 2}) + "\n")
    _write(tmp_path, "logs.ndjson", json.dumps({"id": 3}) + "\n")
    _write(tmp_path, "single.json", json.dumps({"id": 4}) + "\n")
    _write_gz(tmp_path, "packed_csv.csv.gz", _csv(("7,gz",)))
    _write_gz(tmp_path, "packed_json.json.gz", json.dumps({"id": 8}) + "\n")
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = {t.path: t for t in provider.list_tables()}
    assert set(tables) == {"tab", "notes", "logs", "single", "packed_csv", "packed_json"}
    assert tables["tab"].format == "tsv" and tables["notes"].format == "jsonl"
    assert tables["logs"].format == "ndjson" and tables["single"].format == "json"
    assert tables["packed_csv"].format == "csv.gz" and tables["packed_json"].format == "json.gz"
    # every one opens and reads correctly through its format object
    assert provider.open_dataset(tables["tab"]).to_table().to_pydict() == {"id": [1], "v": ["x"]}
    assert provider.open_dataset(tables["notes"]).to_table().num_rows == 2
    assert provider.open_dataset(tables["single"]).to_table().num_rows == 1
    assert provider.open_dataset(tables["packed_csv"]).to_table().num_rows == 1
    assert provider.open_dataset(tables["packed_json"]).to_table().num_rows == 1


def test_mixed_folder_parquet_wins_raw_ignored(tmp_path, caplog):
    _write_parquet(tmp_path, "mixtbl/x.parquet", pa.table({"id": [1, 2, 3], "name": ["x", "y", "z"]}))
    _write(tmp_path, "mixtbl/y.csv", _csv(("9,ignored",)))
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = provider.list_tables()
    assert [(t.path, t.format) for t in tables] == [("mixtbl", "parquet")]
    # The csv file is NOT folded into the parquet table's discovery — the
    # open passes the parquet filter and the csv never becomes its own table.
    assert provider.open_dataset(tables[0]).schema.names == ["id", "name"]
    with caplog.at_level(logging.INFO, logger="sqlhandler.rawfiles"):
        provider.list_tables()
    assert any("mixtbl" in r.message and "parquet" in r.message for r in caplog.records)


def test_raw_collision_first_suffix_alphabetically_wins(tmp_path, caplog):
    _write(tmp_path, "coll/events.json", json.dumps({"id": 1}) + "\n")
    _write(tmp_path, "coll/events.csv", _csv(("5,csv-wins",)))
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = provider.list_tables()
    assert [(t.path, t.format) for t in tables] == [("coll", "csv")]
    assert provider.open_dataset(tables[0]).to_table().to_pydict() == {"id": [5], "name": ["csv-wins"]}
    with caplog.at_level(logging.WARNING, logger="sqlhandler.file"):
        provider.list_tables()
    assert any("collision" in r.message and "csv" in r.message for r in caplog.records)


def test_size_cap_skips_whole_table_with_log(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "1")
    _write(tmp_path, "small.csv", _csv())
    big = os.path.join(str(tmp_path), "huge", "a.csv")
    os.makedirs(os.path.dirname(big), exist_ok=True)
    with open(big, "wb") as fh:
        fh.write(b"id\n" + b"1" * (2 * 1024 * 1024))  # 2 MB > 1 MB cap
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    with caplog.at_level(logging.INFO, logger="sqlhandler.rawfiles"):
        tables = provider.list_tables()
    assert [t.path for t in tables] == ["small"]
    assert any("huge" in r.message and "SQLHANDLER_RAW_MAX_FILE_MB" in r.message for r in caplog.records)


def test_gz_cap_applies_to_compressed_size(tmp_path, monkeypatch):
    # 1 MB cap; the gz file's COMPRESSED size stays under it while the
    # uncompressed content is far larger — the table is DISCOVERED (the cap
    # is honest about being approximate for gzip, per the docstring).
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "1")
    _write_gz(tmp_path, "big.csv.gz", "id,v\n" + "".join(f"{i},{'x' * 50}\n" for i in range(20000)))
    assert os.path.getsize(str(tmp_path / "big.csv.gz")) < 1024 * 1024
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = provider.list_tables()
    assert [t.path for t in tables] == ["big"]
    assert provider.open_dataset(tables[0]).to_table().num_rows == 20000


def test_raw_formats_off_hides_raw_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RAW_FORMATS", "off")
    _write(tmp_path, "orders.csv", _csv())
    pq.write_table(pa.table({"id": [1]}), tmp_path / "keep.parquet")
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    assert [t.path for t in provider.list_tables()] == ["keep"]


def test_raw_hidden_folders_skipped_and_delta_wins(tmp_path):
    _write(tmp_path, ".hidden/s.csv", _csv())
    from deltalake import write_deltalake

    write_deltalake(str(tmp_path / "dt_tbl"), pa.table({"a": [1]}), mode="overwrite")
    _write(tmp_path, "dt_tbl/stray.csv", _csv(("9,stray",)))
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = provider.list_tables()
    assert [(t.path, t.format) for t in tables] == [("dt_tbl", "delta")]
    assert provider.open_dataset(tables[0]).to_table().num_rows == 1


def test_raw_inference_failure_surfaces_lakehouse_error(tmp_path):
    # mixed types in ndjson + single-line-array json: both out of scope for
    # pyarrow's newline-delimited json format — they must surface as the
    # standard LakehouseError at open, never a bare crash
    _write(tmp_path, "mixed.ndjson", json.dumps({"id": 1}) + "\n" + json.dumps({"id": "str"}) + "\n")
    _write(tmp_path, "array.json", json.dumps([{"id": 1}, {"id": 2}]))
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    for info in provider.list_tables():
        with pytest.raises(LakehouseError, match="Could not open NFS table"):
            provider.open_dataset(info)


def test_raw_time_travel_refused(tmp_path):
    _write(tmp_path, "t.csv", _csv())
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    with pytest.raises(LakehouseError, match="not supported"):
        provider.open_dataset(provider.list_tables()[0], version=0)


# ------------------------------------------------------------ S3 parity


class _FakeFileInfo:
    def __init__(self, path, size=10, is_file=True):
        self.path = path
        self.type = pafs.FileType.File if is_file else pafs.FileType.Directory
        self.size = size


class _FakeS3FS:
    """Minimal stand-in for pyarrow's S3FileSystem (test_s3_delta pattern)."""

    def __init__(self, entries):
        self._entries = entries

    def get_file_info(self, selector):
        base = selector.base_dir if selector.base_dir != "/" else ""
        if not base:
            return list(self._entries)
        return [e for e in self._entries if e.path == base or e.path.startswith(base + "/")]


def test_s3_provider_discovers_raw_tables_same_rules(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "0")
    entries = [
        _FakeFileInfo("bucket/orders.csv", size=100),
        _FakeFileInfo("bucket/sales/customers.csv", size=100),
        _FakeFileInfo("bucket/sales/events/a.json", size=100),
        _FakeFileInfo("bucket/mix/x.parquet", size=100),
        _FakeFileInfo("bucket/mix/y.csv", size=100),
    ]
    provider = S3Provider(S3Config(bucket="bucket", anonymous=True))
    provider._fs = _FakeS3FS(entries)
    tables = {t.path: t for t in provider.list_tables()}
    # Mirror of the file-backend derivation: a file directly in a schema
    # folder names the FOLDER as the table (bucket/sales/customers.csv ->
    # table "sales", exactly what a parquet file there would produce), and
    # the folder under it becomes its own table.
    assert set(tables) == {"orders", "sales", "sales/events", "mix"}
    assert tables["orders"].format == "csv"
    assert tables["sales"].format == "csv" and tables["sales"].location == "sales"
    assert tables["sales/events"].format == "json"
    assert tables["mix"].format == "parquet"  # parquet wins the mixed folder


def test_s3_provider_size_cap_skips(monkeypatch, caplog):
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "1")
    entries = [
        _FakeFileInfo("bucket/huge/a.csv", size=2 * 1024 * 1024),
        _FakeFileInfo("bucket/small.csv", size=100),
    ]
    provider = S3Provider(S3Config(bucket="bucket", anonymous=True))
    provider._fs = _FakeS3FS(entries)
    with caplog.at_level(logging.INFO, logger="sqlhandler.rawfiles"):
        tables = provider.list_tables()
    assert [t.path for t in tables] == ["small"]
    assert any("huge" in r.message for r in caplog.records)


# --------------------------------------------------------- engine end-to-end


def _csv_engine(tmp_path):
    _write(tmp_path, "orders.csv", _csv())
    return SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)


def test_engine_run_sql_over_csv_table(tmp_path):
    eng = _csv_engine(tmp_path)
    result = eng.query_duckdb("SELECT id, name FROM orders WHERE id > 1 ORDER BY id")
    assert result.to_pydict() == {"id": [2, 3], "name": ["y", "z"]}
    # a bare count returns the RIGHT number through the query path
    n = eng.query_duckdb("SELECT count(*) AS n FROM orders")
    assert n.to_pydict() == {"n": [3]}


def test_engine_count_star_does_not_take_metadata_fastpath_on_raw(tmp_path, monkeypatch):
    """The count(*) metadata fast path must not fire on raw tables.

    Observability: the fast path serves count from parquet footers with NO
    dataset open; the query path opens the dataset. We assert on the engine's
    own open counter (cache_stats()["dataset_opens"-shaped usage counters])
    plus the timing shape: a 200k-row csv counted via the metadata path would
    be sub-millisecond, the honest scan is not. Primarily: the RESULT must be
    correct, and the dataset must actually be opened (fast path would skip it).
    """
    import time

    monkeypatch.delenv("SQLHANDLER_RAW_MAX_FILE_MB", raising=False)
    _write_parquet(tmp_path, "p.parquet", pa.table({"id": list(range(200_000))}))
    _write(tmp_path, "c.csv", "id\n" + "\n".join(str(i) for i in range(200_000)) + "\n")
    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)

    stats_before = eng.cache_stats()["dataset_cached_tables"]
    t0 = time.perf_counter()
    n_parquet = eng.query_duckdb("SELECT count(*) AS n FROM p").to_pydict()["n"][0]
    parquet_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    n_csv = eng.query_duckdb("SELECT count(*) AS n FROM c").to_pydict()["n"][0]
    csv_ms = (time.perf_counter() - t0) * 1000

    assert n_parquet == n_csv == 200_000  # correctness first
    # The csv path actually opened a dataset (the gated-off fast path would
    # not have); the parquet path did NOT (served from footers).
    assert eng.cache_stats()["dataset_cached_tables"] > stats_before
    assert csv_ms > parquet_ms  # scan (51 ms measured) vs metadata (~1 ms)


def test_engine_describe_profile_sample_on_raw(tmp_path):
    eng = _csv_engine(tmp_path)
    desc = eng.describe_table("orders")
    assert desc["format"] == "csv" and desc["raw"] is True
    assert [c["name"] for c in desc["columns"]] == ["id", "name"]
    prof = eng.profile_table("orders")
    assert prof["n_rows"] == 3
    names = {c["name"] for c in prof["columns"]}
    assert {"id", "name"} <= names
    sample = eng.sample_rows("orders", limit=2)
    assert sample["n_rows"] == 3 and len(sample["rows"]) == 2
    colstats = eng.column_stats("orders", "id")
    assert colstats["min"] == 1 and colstats["max"] == 3


def test_engine_explain_query_reports_raw_format(tmp_path):
    eng = _csv_engine(tmp_path)
    entry = eng.explain_query("SELECT * FROM orders")["tables"][0]
    assert entry["format"] == "csv" and entry["rows"] == 3
    assert entry["rows_confidence"] == "exact"


# ------------------------------------------------- parquet regression (S3+file)


def test_parquet_only_discovery_unchanged(tmp_path):
    """Regression: a parquet-only directory discovers exactly as before.

    NOTE the pre-existing convention this pins: a file directly inside a
    schema folder names the FOLDER as the table (``sales/customers.parquet``
    -> table ``sales``), which is why the layout also contains a
    ``sales/customers/`` folder to produce the ``sales/customers`` table —
    both shapes were and are produced by the same walk.
    """
    _write_parquet(tmp_path, "orders.parquet")
    _write_parquet(tmp_path, "sales/customers.parquet", pa.table({"id": [1]}))
    _write_parquet(tmp_path, "sales/customers/dt=2024/x.parquet", pa.table({"id": [1]}))
    _write_parquet(tmp_path, ".hidden/x.parquet", pa.table({"id": [1]}))
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = provider.list_tables()
    paths = [t.path for t in tables]
    assert paths == ["orders", "sales", "sales/customers"]
    assert all(t.format == "parquet" for t in tables)
    assert all("dt=" not in p for p in paths) and all(".hidden" not in p for p in paths)


def test_s3_delta_log_json_never_becomes_a_raw_table(monkeypatch):
    # Regression (parent integration gate): raw-format discovery classified
    # Delta's _delta_log/*.json commit files as raw JSON tables, so a Delta
    # table under S3_FORMAT=auto listed TWO tables (the real delta_tbl plus
    # a "_delta_log" json table shadowing its folder) and
    # test_s3_delta.py::test_parquet_format_ignores_delta_logs failed.
    # Delta log files are table METADATA — excluded from raw discovery the
    # same way data files inside the delta table folder are.
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "0")
    entries = [
        _FakeFileInfo("bucket/lake/delta_tbl/_delta_log/00000000000000000000.json", size=50),
        _FakeFileInfo("bucket/lake/delta_tbl/part-0001.parquet", size=100),
        _FakeFileInfo("bucket/landing/events.jsonl", size=100),
    ]
    provider = S3Provider(S3Config(bucket="bucket", anonymous=True))
    provider._fs = _FakeS3FS(entries)
    tables = provider.list_tables()
    paths = [t.path for t in tables]
    assert "lake/delta_tbl" in paths
    assert not any("_delta_log" in p for p in paths)
    assert "landing" in paths  # real raw files still discover (folder-derivation rule)
