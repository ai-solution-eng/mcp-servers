"""Tests for the read-only external-database attach support.

Two layers:

* Unit tests (fake DuckDB connection) pin the config parsing, ATTACH SQL
  generation, secret scrubbing, and alias detection — the pieces where a
  regression would be silent.
* Integration tests run against a REAL Postgres via the ``pgserver``
  package (embedded binaries; skipped when not installed). They pin the
  security-critical behaviors of the production sequence — ATTACH before
  the fs lockdown, SELECT working with LocalFileSystem disabled, writes
  rejected by the READ_ONLY attach, and the fs lockdown still holding.
"""

import json
import os
import tempfile
from pathlib import Path

import pyarrow as pa
import pytest

# pgserver resolves its runtime/lock dir via platformdirs at import time —
# point XDG_RUNTIME_DIR at a writable location BEFORE the (lazy) import.
os.environ.setdefault(
    "XDG_RUNTIME_DIR", os.path.join(tempfile.gettempdir(), "sqlhandler-pg-runtime")
)
os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)

from sqlhandler.engine import SqlEngine  # noqa: E402
from sqlhandler.external import (  # noqa: E402
    AttachSpec,
    ExternalAttachError,
    apply_external,
    build_attach_sql,
    parse_attach_config,
    scrub_secrets,
    sql_references_attach,
    validate_qualified_name,
)
from sqlhandler.provider import LakehouseError  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NoTables:
    """Minimal provider stub: no lake tables, everything else unused."""

    kind = "stub"

    def list_tables(self):
        return []

    def table_uri(self, info):
        return f"stub://{info.path}"

    def open_dataset(self, info, version=None):  # pragma: no cover
        raise AssertionError("open_dataset should not be called in these tests")


class _OneLakeTable(_NoTables):
    """Stub provider exposing one lake table for mixed lake+DB join tests."""

    kind = "stub"

    def list_tables(self):
        from sqlhandler.provider import TableInfo

        return [TableInfo(name="lake_tbl", schema="default", source="default")]

    def open_dataset(self, info, version=None):
        return pa.table({"id": [1, 2, 3], "amt": [10, 20, 30]})


class _FakeCon:
    """Records execute() calls; optionally raises on the Nth call."""

    def __init__(self, fail_on=None, error=Exception("boom")):
        self.calls: list[str] = []
        self._fail_on = fail_on
        self._error = error

    def execute(self, sql, *args):
        self.calls.append(sql)
        if self._fail_on is not None and len(self.calls) >= self._fail_on:
            raise self._error
        return self


def _attach_env(host: str, tmp_path: Path, password_env: str = "") -> dict:
    return {
        "SQLHANDLER_ATTACH": json.dumps(
            [
                {
                    "name": "ops",
                    "type": "postgres",
                    "host": host,
                    "database": "postgres",
                    "user": "postgres",
                    "password_env": password_env,
                }
            ]
        )
    }


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_parse_inline_config_happy_path(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps(
            [
                {
                    "name": "ops",
                    "type": "postgres",
                    "host": "pg.internal",
                    "database": "opsdb",
                    "user": "ro",
                    "password_env": "PW",
                }
            ]
        ),
    )
    monkeypatch.setenv("PW", "s3cret")
    specs = parse_attach_config()
    assert len(specs) == 1
    s = specs[0]
    assert (s.name, s.type, s.host, s.port, s.database) == ("ops", "postgres", "pg.internal", 5432, "opsdb")
    assert s.password() == "s3cret"
    assert s.read_only is True
    assert s.display_uri == "postgres://pg.internal:5432/opsdb"  # no secret


def test_parse_file_config_with_defaults(monkeypatch, tmp_path):
    cfg = tmp_path / "attach.json"
    cfg.write_text(
        json.dumps({"databases": [{"name": "mx", "type": "mysql", "host": "db", "database": "d", "password_env": ""}]})
    )
    monkeypatch.setenv("SQLHANDLER_ATTACH_FILE", str(cfg))
    specs = parse_attach_config()
    assert specs[0].port == 3306  # type default
    assert specs[0].password() is None  # trust auth


def test_rejects_literal_password_key(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "ops", "type": "postgres", "host": "h", "database": "d", "password": "oops"}]),
    )
    with pytest.raises(ValueError, match="password_env"):
        parse_attach_config()


def test_rejects_missing_password_env_var(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "ops", "type": "postgres", "host": "h", "database": "d", "password_env": "NOT_SET"}]),
    )
    monkeypatch.delenv("NOT_SET", raising=False)
    with pytest.raises(ValueError, match="NOT_SET"):
        parse_attach_config()


def test_rejects_bad_and_duplicate_aliases(monkeypatch):
    bad = json.dumps([{"name": "9ops", "type": "postgres", "host": "h", "database": "d", "password_env": ""}])
    monkeypatch.setenv("SQLHANDLER_ATTACH", bad)
    with pytest.raises(ValueError, match="alias"):
        parse_attach_config()
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "memory", "type": "postgres", "host": "h", "database": "d", "password_env": ""}]),
    )
    with pytest.raises(ValueError, match="reserved"):
        parse_attach_config()
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps(
            [
                {"name": "ops", "type": "postgres", "host": "h", "database": "d", "password_env": ""},
                {"name": "OPS", "type": "mysql", "host": "h", "database": "d", "password_env": ""},
            ]
        ),
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_attach_config()


def test_rejects_unknown_type(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "x", "type": "oracle", "host": "h", "database": "d", "password_env": ""}]),
    )
    with pytest.raises(ValueError, match="type"):
        parse_attach_config()


def test_empty_config_is_empty_list():
    assert parse_attach_config({}) == []


# ---------------------------------------------------------------------------
# SQL generation / detection / scrubbing
# ---------------------------------------------------------------------------


def test_build_attach_sql_escapes_and_enforces_read_only():
    spec = AttachSpec("ops", "postgres", "h", 5432, "db", "u", "PW")
    sql = build_attach_sql(spec, "p'w")
    assert "READ_ONLY" in sql
    assert "TYPE postgres" in sql
    # value-level quote doubling ('p''w'), then DSN-level escaping inside the
    # ATTACH literal ('' -> '''') — both layers must be present.
    assert "p''''w" in sql
    assert 'AS "ops"' in sql


def test_build_attach_sql_mysql_key_names():
    spec = AttachSpec("mx", "mysql", "h", 3306, "db", "u", "PW")
    sql = build_attach_sql(spec, "pw")
    assert "database=" in sql and "passwd=" in sql
    assert "dbname=" not in sql


def test_sql_references_attach_detection():
    spec = AttachSpec("ops", "postgres", "h", 5432, "db", "u", "")
    assert sql_references_attach("SELECT * FROM ops.public.t", [spec]) == [spec]
    assert sql_references_attach('SELECT * FROM "ops".public.t', [spec]) == [spec]
    assert sql_references_attach("SELECT * FROM OPS.PUBLIC.T", [spec]) == [spec]
    assert sql_references_attach("SELECT * FROM operations.t", [spec]) == []
    assert sql_references_attach("SELECT * FROM opslog", [spec]) == []
    assert sql_references_attach("SELECT 1", [spec]) == []
    assert sql_references_attach("SELECT * FROM ops.public.t", []) == []


def test_scrub_secrets_removes_passwords():
    spec = AttachSpec("ops", "postgres", "h", 5432, "db", "u", "PW")
    text = "attach failed for host=h password=supersecret1 port=5432"
    out = scrub_secrets(text, [spec], {"PW": "supersecret1"})
    assert "supersecret1" not in out and "***" in out


def test_apply_external_loads_before_attach_and_scrubs_errors():
    spec = AttachSpec("ops", "postgres", "h", 5432, "db", "u", "PW")
    con = _FakeCon()
    apply_external(con, [spec], {"PW": "pw1", "SQLHANDLER_DUCKDB_EXTENSION_DIR": "/ext"})
    kinds = [c.split()[0] for c in con.calls]
    assert kinds[0] == "SET"  # extension directory
    assert kinds[1] == "LOAD"
    assert kinds[2] == "ATTACH"
    assert "READ_ONLY" in con.calls[2] and "pw1" in con.calls[2]

    failing = _FakeCon(fail_on=2, error=Exception("conn failed host=h password=pw1 port=5432"))
    with pytest.raises(ExternalAttachError) as ei:
        apply_external(failing, [spec], {"PW": "pw1"})  # no extension dir -> LOAD is call 1, ATTACH call 2
    assert "pw1" not in str(ei.value) and "***" in str(ei.value)


def test_validate_qualified_name_rejects_injection():
    assert validate_qualified_name("ops", "ops.public.t") == "ops.public.t"
    for bad in ("ops.public.x; DROP TABLE y", "ops.'quoted'", "ops.a.b.c", "other.public.t", "ops"):
        with pytest.raises(LakehouseError):
            validate_qualified_name("ops", bad)


# ---------------------------------------------------------------------------
# Integration: real Postgres via pgserver (skipped when not installed)
# ---------------------------------------------------------------------------


def _pg_fixture_vars(tmp_factory):
    pgserver = pytest.importorskip("pgserver")
    pgdata = tmp_factory.mktemp("sqlhandler-pgdata")
    db = pgserver.get_server(pgdata)
    return db


@pytest.fixture(scope="module")
def pg_server(tmp_path_factory):
    """A real embedded Postgres for the whole module."""
    return _pg_fixture_vars(tmp_path_factory)


@pytest.fixture(scope="module")
def pg_attached(pg_server, tmp_path_factory):
    """Engine with a live Postgres attached + one lake table registered."""
    host = os.path.abspath(str(pg_server.pgdata))
    extdir = Path(__file__).resolve().parent.parent / "duckdb-ext"
    monkey = pytest.MonkeyPatch()
    env = _attach_env(host, None)
    env["SQLHANDLER_DUCKDB_EXTENSION_DIR"] = str(extdir)
    for k, v in env.items():
        monkey.setenv(k, v)
    engine = SqlEngine(_OneLakeTable())
    yield engine
    monkey.undo()


def test_engine_pure_lake_query_does_not_attach(pg_attached):
    engine = pg_attached
    table = engine.query_duckdb("SELECT count(*) AS n FROM lake_tbl")
    assert table.column("n").to_pylist() == [3]


def test_engine_reads_attached_pg_under_lockdown(pg_attached):
    engine = pg_attached
    table = engine.query_duckdb("SELECT count(*) AS n FROM ops.pg_catalog.pg_type")
    assert table.column("n").to_pylist()[0] > 0


def test_engine_mixed_lake_and_pg_join(pg_attached):
    engine = pg_attached
    table = engine.query_duckdb(
        "SELECT count(*) AS n FROM lake_tbl l JOIN ops.pg_catalog.pg_type p ON p.oid > 0"
    )
    assert table.column("n").to_pylist()[0] > 0


def test_engine_pg_writes_rejected(pg_attached):
    engine = pg_attached
    with pytest.raises(Exception, match="(?i)read.?only|CREATE"):
        engine.query_duckdb("CREATE TABLE ops.public.hacked (x int)")


def test_engine_fs_lockdown_still_holds_with_attach(pg_attached):
    engine = pg_attached
    with pytest.raises(Exception, match="(?i)permission|disabled"):
        engine.query_duckdb("SELECT * FROM read_parquet('/etc/passwd')")


def test_describe_external(pg_attached):
    engine = pg_attached
    info = engine.describe_table("ops.pg_catalog.pg_type")
    assert info["source"] == "external" and info["read_only"] is True
    names = [c["name"] for c in info["columns"]]
    assert "oid" in names and "typname" in names
    assert "password" not in info["uri"]


def test_describe_external_rejects_injection(pg_attached):
    engine = pg_attached
    with pytest.raises(LakehouseError):
        engine.describe_table("ops.public.x; DROP TABLE y")


def test_profile_external(pg_attached):
    engine = pg_attached
    p = engine.profile_table("ops.pg_catalog.pg_type", columns=["oid", "typname"])
    assert p["source"] == "external"
    assert {c["name"] for c in p["columns"]} == {"oid", "typname"}
    assert p["profiled_rows"] > 0
    oid = next(c for c in p["columns"] if c["name"] == "oid")
    assert oid["min"] is not None and oid["q50"] is not None


def test_attached_databases_lists_tables(pg_attached, pg_server):
    engine = pg_attached
    # Seed one user table through a SEPARATE, deliberately unlocked attach.
    import duckdb

    con = duckdb.connect()
    con.execute(f"SET extension_directory='{Path(__file__).resolve().parent.parent / 'duckdb-ext'}'")
    con.execute("LOAD postgres_scanner")
    host = engine.attaches[0].host
    con.execute(
        f"ATTACH 'host={host} dbname=postgres user=postgres' AS w (TYPE postgres)"
    )
    con.execute("CREATE OR REPLACE TABLE w.public.agent_test (k int, label text)")
    con.execute("INSERT INTO w.public.agent_test VALUES (1,'a'), (2,'b')")
    con.close()

    listing = engine.attached_databases(refresh=True)
    assert len(listing) == 1 and listing[0]["name"] == "ops"
    assert listing[0]["error"] is None
    qualified = [t["qualified"] for t in listing[0]["tables"]]
    assert "ops.public.agent_test" in qualified
    # and the engine can query it through the read-only attach
    rows = engine.query_duckdb("SELECT count(*) AS n FROM ops.public.agent_test")
    assert rows.column("n").to_pylist() == [2]
