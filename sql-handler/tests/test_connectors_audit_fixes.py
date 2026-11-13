"""Regression tests for the 2026-11-12 connectors-audit fixes (task-16).

One test cluster per fix: GCS process-global credential pinning (P2),
sqlserver ODBC brace quoting (P2), Iceberg unsupported-remote-scheme refusal
(P2), ADLS missing secret-env name (P2), loud ICEBERG_CATALOG_TYPE (P3),
raw negative cap + gzip decompressed cap (P3), mysql root default removal
(P3), dbt virtual statement-type gate (P3), check_connection scrubbing (P3),
bounded discovery listings (P3).
"""

import gzip
import json
import logging
import os

import pyarrow.fs as pafs
import pytest

from sqlhandler.config import (
    FileConfig,
    IcebergConfig,
    S3Config,
    listing_max_entries,
    load_adls_config,
    load_iceberg_config,
    raw_max_file_mb,
)
from sqlhandler.dbt_import import import_dbt_manifest
from sqlhandler.external import AttachSpec, build_attach_sql, parse_attach_config
from sqlhandler.file import FileProvider
from sqlhandler.iceberg import IcebergProvider
from sqlhandler.provider import LakehouseError
from sqlhandler.rawfiles import build_raw_dataset
from sqlhandler.s3 import S3Provider

# --- item 1: GCS process-global credentials pinning -------------------------


@pytest.fixture(autouse=True)
def _reset_gcs_pin(monkeypatch):
    """Isolate the process-global GCS pin AND the env var it writes.

    gcs_build() sets os.environ directly (that is the behavior under test), so
    the teardown must remove it explicitly: leaving it set made
    `pytest tests/test_connectors_audit_fixes.py tests/test_adls_gcs.py` fail
    in the second file on a /keys/a.json that does not exist
    (cross-validation 2026-11-12, P2 test hygiene).
    """
    monkeypatch.setattr("sqlhandler.gcs._PINNED_GCS_CREDENTIALS_FILE", None)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    yield
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


def test_gcs_second_different_key_file_warns_and_first_wins(monkeypatch, caplog):
    cfg_a = gcs_and_config("/keys/a.json")
    cfg_b = gcs_and_config("/keys/b.json")
    gcs_build(cfg_a)
    with caplog.at_level(logging.WARNING, logger="sqlhandler.gcs"):
        gcs_build(cfg_b)
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/keys/a.json"
    assert any("process-global" in r.message and "first source wins" in r.message for r in caplog.records)


def test_gcs_operator_env_untouched_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/operator/key.json")
    with caplog.at_level(logging.WARNING, logger="sqlhandler.gcs"):
        gcs_build(gcs_and_config("/keys/mine.json"))
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/operator/key.json"
    assert any("outside sqlhandler" in r.message for r in caplog.records)


def test_gcs_same_file_and_unset_cases_silent(monkeypatch, caplog):
    gcs_build(gcs_and_config("/keys/a.json"))
    with caplog.at_level(logging.WARNING, logger="sqlhandler.gcs"):
        gcs_build(gcs_and_config("/keys/a.json"))  # identical file: idempotent
    assert not caplog.records
    # Plain pop, NOT monkeypatch.delenv: monkeypatch would record the value
    # gcs_build just wrote and RESTORE it at teardown (its undo runs after
    # this module's fixture), leaking "/keys/a.json" into later test modules.
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    with caplog.at_level(logging.WARNING, logger="sqlhandler.gcs"):
        gcs_build(gcs_and_config(""))  # no credentialsFile: nothing pinned, no warning
    assert not caplog.records


def gcs_and_config(credentials_file):
    from sqlhandler.config import GcsConfig

    return GcsConfig(bucket="b", credentials_file=credentials_file, anonymous=True)


def gcs_build(cfg):
    from sqlhandler.gcs import build_gcs_fs

    return build_gcs_fs(cfg)


# --- item 2: sqlserver ODBC brace quoting -----------------------------------


def _parse_odbc(text):
    """Minimal brace-aware ODBC connstring parser (for the round-trip assert)."""
    out, i, n = {}, 0, len(text)
    while i < n:
        eq = text.index("=", i)
        key = text[i:eq].strip()
        i = eq + 1
        if i < n and text[i] == "{":
            i += 1
            buf = []
            while True:
                if text[i : i + 2] == "}}":
                    buf.append("}")
                    i += 2
                    continue
                if text[i] == "}":
                    i += 1
                    break
                buf.append(text[i])
                i += 1
            value = "".join(buf)
        else:
            j = text.find(";", i)
            if j == -1:
                j = n
            value = text[i:j]
            i = j
        out[key.lower()] = value
        if i < n and text[i] == ";":
            i += 1
    return out


def test_sqlserver_password_brace_quoted_round_trips():
    spec = AttachSpec(name="ops", type="sqlserver", host="h", port=1433, database="db", user="svc;u", password_env="")
    dsn = build_attach_sql(spec, "a;b}c").split("'", 1)[1].rsplit("'", 1)[0]
    assert "Pwd={a;b}}c}" in dsn
    parsed = _parse_odbc(dsn)
    assert parsed["pwd"] == "a;b}c"
    assert parsed["uid"] == "svc;u"
    assert parsed["encrypt"] == "yes"  # no injected keys
    assert set(parsed) == {"server", "database", "uid", "pwd", "encrypt", "trustservercertificate"}


# --- item 3: Iceberg unsupported remote schemes -----------------------------


def _iceberg_provider(warehouse=""):
    return IcebergProvider(
        IcebergConfig(
            catalog_type="sql",
            catalog_uri="sqlite:///tmp/irrelevant.db",
            warehouse=warehouse,
            storage=S3Config(bucket="b", access_key="k", secret_key="s"),
        )
    )


def test_iceberg_unsupported_remote_scheme_refused():
    p = _iceberg_provider()
    with pytest.raises(LakehouseError, match="unsupported scheme"):
        p._normalize_path("gs://bucket/t/1.parquet")
    with pytest.raises(LakehouseError, match="unsupported scheme"):
        p._normalize_path("abfs://c@acct.dfs.core.windows.net/wh/t/1.parquet")
    with pytest.raises(LakehouseError, match="unsupported scheme"):
        p._normalize_path("abfss://c@acct.dfs.core.windows.net/wh/t/1.parquet")
    assert p._normalize_path("s3://bucket/t/1.parquet") == "s3://bucket/t/1.parquet"


# --- item 4: ADLS missing secret-env NAME -----------------------------------


def test_adls_client_secret_without_secret_env_name_raises(monkeypatch):
    for var in ("ADLS_AUTH", "ADLS_CLIENT_SECRET_ENV", "ADLS_TENANT_ID", "ADLS_CLIENT_ID", "ADLS_ACCOUNT", "ADLS_CONTAINER"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(LakehouseError, match="ADLS_CLIENT_SECRET_ENV"):
        load_adls_config({"ADLS_AUTH": "client-secret", "ADLS_TENANT_ID": "t", "ADLS_CLIENT_ID": "c", "ADLS_ACCOUNT": "a", "ADLS_CONTAINER": "c"})
    # anon (default) stays unaffected
    cfg = load_adls_config({"ADLS_ACCOUNT": "a", "ADLS_CONTAINER": "c"})
    assert cfg.auth == "anon" and cfg.client_secret == ""


# --- item 5: loud ICEBERG_CATALOG_TYPE --------------------------------------


def test_iceberg_unknown_catalog_type_raises(monkeypatch):
    monkeypatch.delenv("ICEBERG_CATALOG_TYPE", raising=False)
    with pytest.raises(LakehouseError, match="ICEBERG_CATALOG_TYPE"):
        load_iceberg_config({"ICEBERG_CATALOG_TYPE": "gluu"})
    cfg = load_iceberg_config({"ICEBERG_CATALOG_TYPE": "glue"})
    assert cfg.catalog_type == "glue"
    assert load_iceberg_config({}).catalog_type == "rest"  # unset default unchanged
    assert load_iceberg_config({"ICEBERG_CATALOG_TYPE": "HIVE"}).catalog_type == "hive"  # case-insensitive


# --- item 6: raw cap negatives + gzip decompressed cap ----------------------


def test_raw_negative_cap_falls_back_to_default_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "-1")
    with caplog.at_level(logging.WARNING, logger="sqlhandler.config"):
        assert raw_max_file_mb() == 64
    assert any("negative" in r.message for r in caplog.records)
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "0")
    assert raw_max_file_mb() == 0  # explicit unlimited still works
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "8")
    assert raw_max_file_mb() == 8


def test_gz_decompressed_size_capped_at_open(tmp_path, monkeypatch):
    blob = gzip.compress(b"a,b\n" * 400000)  # ~2.4MB decompressed, tiny compressed
    gz_path = tmp_path / "t.csv.gz"
    gz_path.write_bytes(blob)
    fs = pafs.LocalFileSystem()
    build_raw_dataset("t", "csv.gz", [str(gz_path)], fs)  # default 64MB cap: passes
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "1")
    with pytest.raises(LakehouseError, match="decompresses past"):
        build_raw_dataset("t", "csv.gz", [str(gz_path)], fs)
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "0")
    build_raw_dataset("t", "csv.gz", [str(gz_path)], fs)  # unlimited: no gate
    # plain (uncompressed) formats are not gated
    csv_path = tmp_path / "u.csv"
    csv_path.write_bytes(b"a,b\n" * 400000)
    monkeypatch.setenv("SQLHANDLER_RAW_MAX_FILE_MB", "1")
    build_raw_dataset("u", "csv", [str(csv_path)], fs)


# --- item 7: mysql/mariadb no root default ----------------------------------


def _attach_env(entries):
    return {"SQLHANDLER_ATTACH": json.dumps(entries), "SQLHANDLER_DB_PW_M": "pw"}


def test_mysql_without_user_refuses_root_default():
    with pytest.raises(ValueError, match="requires an explicit 'user'"):
        parse_attach_config(
            _attach_env([{"name": "m", "type": "mysql", "host": "h", "database": "db", "password_env": "SQLHANDLER_DB_PW_M"}])
        )


def test_mysql_with_explicit_user_still_works():
    specs = parse_attach_config(
        _attach_env([{"name": "m", "type": "mysql", "host": "h", "database": "db", "user": "svc", "password_env": "SQLHANDLER_DB_PW_M"}])
    )
    assert specs[0].user == "svc"


# --- item 8: dbt virtual statement-type gate --------------------------------


def _virtual_manifest(compiled_sql):
    return {
        "nodes": {
            "model.j.v": {
                "resource_type": "model",
                "name": "v",
                "schema": "analytics",
                "config": {},
                "description": "documented node so the skip ledger carries the refusal reason",
                "meta": {"sqlhandler": {"virtual": True}},
                "compiled_sql": compiled_sql,
                "columns": {},
            }
        }
    }


def test_dbt_virtual_gate_refuses_non_select_statements():
    # NOTE: SHOW is absent here ON PURPOSE — DuckDB's parser desugars
    # "SHOW TABLES" to a SELECT (StatementType.SELECT), so a parser-type gate
    # accepts it exactly like the engine's own definition gate does; the
    # registered-view load path remains the semantic backstop.
    bad = [
        "SET enable_object_cache=true",
        "CREATE TABLE t (id int)",
        "INSERT INTO t VALUES (1)",
        "ATTACH 'x.db' AS y (TYPE sqlite)",
        "SELECT 1; DROP TABLE t",
        "INSTALL httpfs; SELECT 1",
        "not even sql",
    ]
    for sql in bad:
        result = import_dbt_manifest(_virtual_manifest(sql), allow_virtual=True)
        assert result["virtuals"] == 0, sql
        assert any("never becomes a virtual table" in w for w in result["warnings"]), sql


def test_dbt_virtual_gate_accepts_select_with_explain():
    ok = import_dbt_manifest(_virtual_manifest("SELECT id FROM orders WHERE amount > 100"), allow_virtual=True)
    assert ok["virtuals"] == 1
    cte = import_dbt_manifest(_virtual_manifest("WITH big AS (SELECT 1 AS id) SELECT id FROM big"), allow_virtual=True)
    assert cte["virtuals"] == 1
    # EXPLAIN is REFUSED (cross-validation 2026-11-12): a definition must
    # produce rows, and `EXPLAIN ANALYZE <DML>` executes what it explains —
    # inert only because the engine gate rejects every EXPLAIN, which is a
    # backstop, not a reason to admit it.
    ex = import_dbt_manifest(_virtual_manifest("EXPLAIN SELECT 1"), allow_virtual=True)
    assert ex["virtuals"] == 0
    assert any("never becomes a virtual table" in w for w in ex["warnings"])
    # parser truth: SHOW is a SELECT to DuckDB — accepted here, backstopped by
    # the engine's view-registration gate
    show = import_dbt_manifest(_virtual_manifest("SHOW TABLES"), allow_virtual=True)
    assert show["virtuals"] == 1


# --- item 9: check_connection secret scrubbing ------------------------------


def test_s3_check_connection_scrubs_secrets(monkeypatch):
    cfg = S3Config(bucket="b", access_key="AKIA-SECRET", secret_key="SK-SECRET", endpoint_url="http://minio:9000")
    prov = S3Provider(cfg)

    class Boom:
        def get_file_info(self, selector):
            raise RuntimeError("auth failed for AKIA-SECRET with SK-SECRET at http://minio:9000")

    monkeypatch.setattr(prov, "_s3fs", lambda: Boom())
    out = prov.check_connection()
    assert "AKIA-SECRET" not in out and "SK-SECRET" not in out
    assert "***" in out


def test_iceberg_check_connection_scrubs_token(monkeypatch):
    cfg = IcebergConfig(
        catalog_type="rest",
        catalog_uri="https://rest:8080",
        catalog_token="TOKEN-VALUE",
        storage=S3Config(bucket="b", access_key="k", secret_key="s"),
    )
    prov = IcebergProvider(cfg)

    def boom():
        raise RuntimeError("401 from https://rest:8080 with TOKEN-VALUE")

    monkeypatch.setattr(prov, "_namespaces", boom)
    out = prov.check_connection()
    assert "TOKEN-VALUE" not in out
    assert "***" in out


# --- item 10: bounded discovery listings -------------------------------------


class _FakeInfo:
    def __init__(self, path):
        self.type = pafs.FileType.File
        self.path = path
        self.size = 1


class _FakeS3Fs:
    def __init__(self, count):
        self._count = count

    def get_file_info(self, selector):
        return [_FakeInfo(f"b/f{i}.parquet") for i in range(self._count)]


def test_listing_knob_defaults_and_validation(monkeypatch, caplog):
    monkeypatch.delenv("SQLHANDLER_LISTING_MAX_ENTRIES", raising=False)
    assert listing_max_entries() == 250_000
    monkeypatch.setenv("SQLHANDLER_LISTING_MAX_ENTRIES", "10")
    assert listing_max_entries() == 10
    monkeypatch.setenv("SQLHANDLER_LISTING_MAX_ENTRIES", "0")
    assert listing_max_entries() == 0  # explicit unlimited
    monkeypatch.setenv("SQLHANDLER_LISTING_MAX_ENTRIES", "-5")
    with caplog.at_level(logging.WARNING, logger="sqlhandler.config"):
        assert listing_max_entries() == 250_000
    assert any("negative" in r.message for r in caplog.records)


def test_s3_list_tables_refuses_past_bound(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_LISTING_MAX_ENTRIES", "3")
    prov = S3Provider(S3Config(bucket="b", access_key="k", secret_key="s"))
    monkeypatch.setattr(prov, "_s3fs", lambda: _FakeS3Fs(5))
    with pytest.raises(LakehouseError, match="SQLHANDLER_LISTING_MAX_ENTRIES"):
        prov.list_tables()
    monkeypatch.setenv("SQLHANDLER_LISTING_MAX_ENTRIES", "0")
    infos = prov.list_tables()  # unlimited: all 5 listed
    assert len(infos) == 5


def test_file_list_tables_refuses_past_bound(tmp_path, monkeypatch):
    root = tmp_path / "lz"
    (root / "t").mkdir(parents=True)
    for i in range(5):
        (root / "t" / f"p{i}.parquet").write_bytes(b"x")
    monkeypatch.setenv("SQLHANDLER_LISTING_MAX_ENTRIES", "3")
    prov = FileProvider(FileConfig(root_dir=str(root)))
    with pytest.raises(LakehouseError, match="SQLHANDLER_LISTING_MAX_ENTRIES"):
        prov.list_tables()


def test_iceberg_list_tables_refuses_past_bound(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_LISTING_MAX_ENTRIES", "3")

    class _Cat:
        def list_namespaces(self):
            return [("db",)]

        def list_tables(self, ns):
            return [("db", f"t{i}") for i in range(5)]

    prov = _iceberg_provider()
    prov._cat = _Cat()
    with pytest.raises(LakehouseError, match="SQLHANDLER_LISTING_MAX_ENTRIES"):
        prov.list_tables()