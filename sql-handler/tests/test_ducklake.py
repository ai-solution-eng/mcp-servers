"""DuckLake attach type (read-only) — unit + real-extension integration tests.

DuckLake is DuckDB's lakehouse format: a SQL catalog database (sqlite/postgres/
motherduck/…) storing metadata (schemas/tables/snapshots/stats) that points at
Parquet data files on disk or object storage. SQLhandler attaches one as an
external catalog via the SAME SQLHANDLER_ATTACH machinery as the other types —
the ducklake extension LOADs from the vendored dir, the catalog connect string
rides the ``ducklake:`` URL scheme (NO ``TYPE`` keyword — empirically that form
is the only one duckdb 1.5.5 accepts), and READ_ONLY + sqlguard enforce the
usual source-never-a-sink posture.

Layers (mirrors tests/test_external.py):

* Unit tests (fake DuckDB connection) pin config parsing, ATTACH SQL
  generation, display_uri scrubbing, and the field rules specific to ducklake.
* Integration tests run against a REAL ducklake catalog on a tmp sqlite file,
  created through a second, deliberately unlocked connection — skipped with an
  explicit reason when the vendored ducklake extension cannot LOAD (CI without
  the baked extension dir).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pyarrow as pa
import pytest

from sqlhandler.external import (
    ExternalAttachError,
    apply_external,
    build_attach_sql,
    parse_attach_config,
    scrub_secrets,
    sql_references_attach,
)
from sqlhandler.provider import LakehouseError

REPO = Path(__file__).resolve().parent.parent
EXTDIR = REPO / "duckdb-ext" / "v1.5.5" / "linux_amd64"


def _ducklake_loadable() -> tuple[bool, str]:
    """(ok, reason): does LOAD ducklake work from the vendored dir here?"""
    import duckdb

    probe = duckdb.connect()
    try:
        probe.execute(f"SET extension_directory='{EXTDIR}'")
        probe.execute("LOAD ducklake")
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        probe.close()


_DUCKLAKE_OK, _DUCKLAKE_REASON = _ducklake_loadable()

requires_ducklake = pytest.mark.skipif(
    not _DUCKLAKE_OK,
    reason=f"vendored ducklake extension cannot LOAD in this environment: {_DUCKLAKE_REASON}",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCon:
    """Records execute() calls; optionally raises on every call from N on."""

    def __init__(self, fail_from=None, error=Exception("boom")):
        self.calls: list[str] = []
        self._fail_from = fail_from
        self._error = error

    def execute(self, sql, *args):
        self.calls.append(sql)
        if self._fail_from is not None and len(self.calls) >= self._fail_from:
            raise self._error
        return self


class _OneLakeTable:
    """Stub provider exposing one lake table for mixed lake+ducklake joins."""

    kind = "stub"

    def __init__(self, tmp_path: Path):
        import pyarrow.parquet as ppq

        self._path = tmp_path / "lake"
        self._path.mkdir(parents=True, exist_ok=True)
        ppq.write_table(
            pa.table({"id": [1, 2, 3], "amt": [10, 20, 30]}), self._path / "data.parquet"
        )

    def list_tables(self):
        from sqlhandler.provider import TableInfo

        return [TableInfo(name="lake_tbl", schema="default", source="default")]

    def table_uri(self, info):
        return f"stub://{info.path}"

    def open_dataset(self, info, version=None):
        import pyarrow.dataset as pad

        return pad.dataset(str(self._path), format="parquet")


def _ducklake_spec_env(catalog: str, *, name: str = "dl", **extra) -> dict:
    entry = {"name": name, "type": "ducklake", "catalog": catalog, **extra}
    return {"SQLHANDLER_ATTACH": json.dumps([entry])}


# ---------------------------------------------------------------------------
# Config parsing (unit)
# ---------------------------------------------------------------------------


def test_parse_ducklake_happy_path():
    env = _ducklake_spec_env("sqlite:/data/ducklake/catalog.db", data_path="/data/ducklake/files")
    (spec,) = parse_attach_config(env)
    assert spec.type == "ducklake"
    assert spec.name == "dl"
    assert spec.catalog == "sqlite:/data/ducklake/catalog.db"
    assert spec.data_path == "/data/ducklake/files"
    assert spec.host == "" and spec.database == "" and spec.port == 0
    assert spec.user == "" and spec.password_env == ""
    assert spec.read_only is True


def test_parse_ducklake_postgres_catalog_form():
    env = _ducklake_spec_env("postgres:dbname=ducklake host=pg.internal user=ro")
    (spec,) = parse_attach_config(env)
    assert spec.catalog == "postgres:dbname=ducklake host=pg.internal user=ro"
    sql = build_attach_sql(spec, None)
    assert sql.startswith("ATTACH 'ducklake:postgres:dbname=ducklake host=pg.internal user=ro'")
    assert "READ_ONLY" in sql
    assert " TYPE " not in sql  # the ducklake: scheme selects the handler


def test_parse_ducklake_password_env_optional_but_respected():
    env = _ducklake_spec_env("md:mydb", password_env="MDL_TOKEN")
    env["MDL_TOKEN"] = "tok-abc123"
    (spec,) = parse_attach_config(env)
    assert spec.password_env == "MDL_TOKEN"
    sql = build_attach_sql(spec, spec.password(env))
    assert "motherduck_token=tok-abc123" in sql
    # and scrubbing removes it again
    assert "tok-abc123" not in scrub_secrets(sql, [spec], env)


def test_parse_ducklake_rejects_server_style_fields():
    base = "sqlite:/data/catalog.db"
    # host
    env = _ducklake_spec_env(base, host="h")
    with pytest.raises(ValueError, match="host.*not valid.*ducklake"):
        parse_attach_config(env)
    # database
    env = _ducklake_spec_env(base, database="x")
    with pytest.raises(ValueError, match="database.*not valid.*ducklake"):
        parse_attach_config(env)
    # port
    env = _ducklake_spec_env(base, port=5432)
    with pytest.raises(ValueError, match="port.*not valid.*ducklake"):
        parse_attach_config(env)
    # user
    env = _ducklake_spec_env(base, user="u")
    with pytest.raises(ValueError, match="user.*not valid.*ducklake"):
        parse_attach_config(env)
    # params
    env = _ducklake_spec_env(base, params={"sslmode": "require"})
    with pytest.raises(ValueError, match="params.*not valid.*ducklake"):
        parse_attach_config(env)


def test_parse_ducklake_rejects_missing_catalog_and_control_chars():
    env = _ducklake_spec_env("")
    with pytest.raises(ValueError, match="catalog is required"):
        parse_attach_config(env)
    env = _ducklake_spec_env("sqlite:/data/cat.db\nDROP TABLE x")
    with pytest.raises(ValueError, match="control characters"):
        parse_attach_config(env)
    env = _ducklake_spec_env("sqlite:/data/cat.db", data_path="/data/files\x0b")
    with pytest.raises(ValueError, match="control characters"):
        parse_attach_config(env)
    # a NUL in the catalog string
    env = _ducklake_spec_env("sqlite:/data/cat\x00.db")
    with pytest.raises(ValueError, match="control characters"):
        parse_attach_config(env)


def test_parse_ducklake_requires_password_env_when_present():
    env = _ducklake_spec_env("md:mydb", password_env="MISSING_TOKEN")
    with pytest.raises(ValueError, match="password_env.*MISSING_TOKEN"):
        parse_attach_config(env)


def test_parse_mixed_types_ducklake_and_postgres():
    env = {
        "SQLHANDLER_ATTACH": json.dumps(
            [
                {"name": "ops", "type": "postgres", "host": "pg.internal",
                 "database": "opsdb", "user": "ro", "password_env": "PW"},
                {"name": "dl", "type": "ducklake", "catalog": "sqlite:/data/cat.db"},
            ]
        ),
        "PW": "p",
    }
    specs = parse_attach_config(env)
    assert [s.type for s in specs] == ["postgres", "ducklake"]
    assert {s.name for s in specs} == {"ops", "dl"}


# ---------------------------------------------------------------------------
# ATTACH SQL generation (unit, fake con)
# ---------------------------------------------------------------------------


def test_build_attach_sql_sqlite_catalog_with_data_path():
    env = _ducklake_spec_env("sqlite:/data/ducklake/catalog.db", data_path="/data/ducklake/files")
    env["SQLHANDLER_DUCKDB_EXTENSION_DIR"] = "/data/ext"
    (spec,) = parse_attach_config(env)
    con = _FakeCon()
    apply_external(con, [spec], env)
    assert con.calls == [
        "SET extension_directory='/data/ext'",
        "LOAD ducklake",
        (
            "ATTACH 'ducklake:sqlite:/data/ducklake/catalog.db' AS \"dl\" "
            "(DATA_PATH '/data/ducklake/files', READ_ONLY)"
        ),
    ]


def test_build_attach_sql_no_data_path_no_options():
    env = _ducklake_spec_env("sqlite:/data/cat.db")
    (spec,) = parse_attach_config(env)
    con = _FakeCon()
    apply_external(con, [spec], env)
    assert con.calls[-1] == "ATTACH 'ducklake:sqlite:/data/cat.db' AS \"dl\" (READ_ONLY)"


def test_build_attach_sql_data_path_quotes_are_doubled():
    env = _ducklake_spec_env("sqlite:/data/cat.db", data_path="/data/o'brien")
    (spec,) = parse_attach_config(env)
    sql = build_attach_sql(spec, None)
    assert "DATA_PATH '/data/o''brien'" in sql


def test_apply_external_scrubs_catalog_secret_from_errors():
    env = _ducklake_spec_env("md:mydb?motherduck_token=tok-abc123", password_env="MDL_TOKEN")
    env["MDL_TOKEN"] = "tok-abc123"
    (spec,) = parse_attach_config(env)
    boom = _FakeCon(fail_from=1, error=Exception("attach failed: token tok-abc123 rejected"))
    with pytest.raises(ExternalAttachError) as excinfo:
        apply_external(boom, [spec], env)
    assert "tok-abc123" not in str(excinfo.value)
    assert "tok-abc123" not in repr(excinfo.value)


def test_display_uri_is_credential_free():
    env = _ducklake_spec_env(
        "postgres:dbname=ducklake host=pg.internal password=tok-abc123",
        password_env="MDL_TOKEN",
    )
    env["MDL_TOKEN"] = "tok-abc123"
    (spec,) = parse_attach_config(env)
    uri = spec.display_uri
    assert uri.startswith("ducklake:postgres:")
    assert "tok-abc123" not in uri
    assert "password=***" in uri


def test_sql_references_attach_matches_ducklake_alias():
    env = _ducklake_spec_env("sqlite:/data/cat.db", name="lakehouse")
    (spec,) = parse_attach_config(env)
    assert sql_references_attach("SELECT * FROM lakehouse.main.t", [spec]) == [spec]
    assert sql_references_attach("SELECT * FROM other.main.t", [spec]) == []
    assert sql_references_attach("SELECT * FROM lakehouses.main.t", [spec]) == []


# ---------------------------------------------------------------------------
# Integration: a REAL ducklake on a tmp sqlite catalog through the vendored
# extension. Skipped (clean) when LOAD ducklake does not work here.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ducklake_attached(tmp_path_factory):
    """A real ducklake catalog + an engine-side read-only attach of it."""
    import duckdb

    from sqlhandler.engine import SqlEngine

    work = tmp_path_factory.mktemp("ducklake")
    cat = work / "catalog.db"
    data = work / "files"

    # CREATE side: a second connection WITHOUT read-only (the writer role an
    # ETL job would have). Small tables live INLINE in the catalog db; the
    # big table below forces real parquet files under data_path.
    writer = duckdb.connect()
    try:
        writer.execute(f"SET extension_directory='{EXTDIR}'")
        writer.execute("LOAD ducklake")
        writer.execute(f"ATTACH 'ducklake:sqlite:{cat}' AS w (DATA_PATH '{data}')")
        writer.execute("CREATE SCHEMA w.ops")
        writer.execute("CREATE TABLE w.ops.agent_test AS SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS t(k, label)")
        writer.execute("CREATE TABLE w.ops.big AS SELECT range AS k FROM range(200000)")
        writer.execute("CHECKPOINT w")
    finally:
        writer.close()

    ok, reason = _ducklake_loadable()
    if not ok:
        pytest.skip(f"ducklake extension cannot LOAD (parallel bake step): {reason}")

    monkey = pytest.MonkeyPatch()
    monkey.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps(
            [
                {"name": "dl", "type": "ducklake", "catalog": f"sqlite:{cat}"},
            ]
        ),
    )
    monkey.setenv("SQLHANDLER_DUCKDB_EXTENSION_DIR", str(EXTDIR))
    engine = SqlEngine(_OneLakeTable(work))
    yield {"engine": engine, "cat": cat, "data": data, "work": work}
    monkey.undo()


@requires_ducklake
def test_engine_reads_attached_ducklake_metadata_under_lockdown(ducklake_attached):
    """Under the default fs lockdown ducklake serves METADATA answers from the
    catalog/stats (count(*), min/max) — verified empirically on duckdb 1.5.5.
    Row reads are pinned separately (see test_engine_reads_rows_with_file_access)."""
    engine = ducklake_attached["engine"]
    table = engine.query_duckdb("SELECT count(*) AS n FROM dl.ops.agent_test")
    assert table.column("n").to_pylist() == [2]
    table = engine.query_duckdb("SELECT min(k) AS lo FROM dl.ops.agent_test")
    assert table.column("lo").to_pylist() == [1]


@requires_ducklake
def test_engine_reads_rows_with_file_access(ducklake_attached):
    """ROW reads through a ducklake catalog go through DuckDB's LocalFileSystem
    (both the inlined-catalog rows and the parquet data files under DATA_PATH) —
    so with the fs lockdown ON they fail closed, and with the engine's existing
    documented opt-out (SQLHANDLER_DUCKDB_FILE_ACCESS=1) they work. This is the
    ducklake 1.5.5 behavior this feature inherits; row reads REQUIRE the opt-out,
    which is operator-set, and reads stay read-only regardless (READ_ONLY attach
    + sqlguard do not depend on the flag)."""
    import duckdb

    from sqlhandler.external import apply_external, parse_attach_config

    cat = ducklake_attached["cat"]
    env = _ducklake_spec_env(f"sqlite:{cat}")
    env["SQLHANDLER_DUCKDB_EXTENSION_DIR"] = str(EXTDIR)
    specs = parse_attach_config(env)
    con = duckdb.connect()
    try:
        apply_external(con, specs, env)
        # the lockdown FIRST: row reads must fail CLOSED
        con.execute("SET disabled_filesystems='LocalFileSystem'")
        with pytest.raises(Exception, match="(?i)permission|disabled"):
            con.execute("SELECT * FROM dl.ops.agent_test").fetchall()
        # then the operator opt-out restores row reads (fresh connection)
    finally:
        con.close()
    con = duckdb.connect()
    try:
        apply_external(con, specs, env)
        # SQLHANDLER_DUCKDB_FILE_ACCESS=1 → _duckdb_fs_lockdown returns early;
        # emulate exactly that posture here:
        rows = con.execute("SELECT * FROM dl.ops.agent_test ORDER BY k").fetchall()
        assert rows == [(1, "a"), (2, "b")]
        big = con.execute("SELECT count(*) AS n FROM dl.ops.big").fetchall()
        assert big == [(200000,)]
    finally:
        con.close()


@requires_ducklake
def test_engine_reads_parquet_backed_ducklake_table_under_lockdown(ducklake_attached):
    """The 200k-row table lives as REAL parquet files under DATA_PATH — its
    metadata answers (count/min/max) are served from ducklake stats under the
    default fs lockdown."""
    engine = ducklake_attached["engine"]
    table = engine.query_duckdb("SELECT count(*) AS n, min(k) AS lo, max(k) AS hi FROM dl.ops.big")
    assert table.column("n").to_pylist() == [200000]
    assert table.column("lo").to_pylist() == [0]
    assert table.column("hi").to_pylist() == [199999]


@requires_ducklake
def test_engine_mixed_lake_and_ducklake_join(ducklake_attached):
    """The e2e JOIN in ONE query — the ducklake catalog and the lake table.
    Joined with SQLHANDLER_DUCKDB_FILE_ACCESS=1 (the documented opt-out row
    reads need on local ducklake data; see test_engine_reads_rows_with_file_access)."""
    engine = ducklake_attached["engine"]
    os.environ["SQLHANDLER_DUCKDB_FILE_ACCESS"] = "1"
    try:
        table = engine.query_duckdb(
            "SELECT count(*) AS n FROM dl.ops.agent_test a JOIN lake_tbl l ON l.id = a.k"
        )
    finally:
        os.environ.pop("SQLHANDLER_DUCKDB_FILE_ACCESS", None)
    assert table.column("n").to_pylist() == [2]


@requires_ducklake
def test_engine_ducklake_writes_refused_by_readonly_guard(ducklake_attached):
    """The user-visible read-only contract: writes through run_sql/query_duckdb
    are refused by the UNCONDITIONAL sqlguard layer (assert_attached_readonly)
    BEFORE any extension is loaded — INSERT/UPDATE/DELETE/CREATE all fail."""
    engine = ducklake_attached["engine"]
    for stmt, pattern in (
        ("INSERT INTO dl.ops.agent_test VALUES (3, 'nope')", r"INSERT statements are not allowed"),
        ("UPDATE dl.ops.agent_test SET k = 99", r"UPDATE statements are not allowed"),
        ("DELETE FROM dl.ops.agent_test", r"DELETE statements are not allowed"),
        ("CREATE TABLE dl.ops.hacked (x int)", r"CREATE statements are not allowed"),
    ):
        with pytest.raises(LakehouseError, match=pattern):
            engine.query_duckdb(stmt)


@requires_ducklake
def test_engine_ducklake_attach_readonly_enforced_at_engine_level(ducklake_attached):
    """Belt and braces behind the guard: on a connection that DID attach
    (what describe/profile use), DuckDB's READ_ONLY attach itself refuses
    catalog writes."""
    import duckdb

    from sqlhandler.external import parse_attach_config

    cat = ducklake_attached["cat"]
    env = _ducklake_spec_env(f"sqlite:{cat}")
    env["SQLHANDLER_DUCKDB_EXTENSION_DIR"] = str(EXTDIR)
    specs = parse_attach_config(env)
    con = duckdb.connect()
    try:
        apply_external(con, specs, env)
        for stmt in (
            "CREATE TABLE dl.ops.hacked (x int)",
            "CREATE SCHEMA dl.hacked",
            "ALTER TABLE dl.ops.agent_test RENAME TO nope",
        ):
            with pytest.raises(Exception, match="(?i)read.only"):
                con.execute(stmt)
    finally:
        con.close()


@requires_ducklake
def test_engine_fs_lockdown_still_holds_with_ducklake_attached(ducklake_attached):
    engine = ducklake_attached["engine"]
    with pytest.raises(Exception, match="(?i)permission|disabled"):
        engine.query_duckdb("SELECT * FROM read_parquet('/etc/passwd')")


@requires_ducklake
def test_attached_ducklake_lists_tables(ducklake_attached):
    engine = ducklake_attached["engine"]
    listing = engine.attached_databases(refresh=True)
    assert len(listing) == 1
    entry = listing[0]
    assert entry["name"] == "dl" and entry["type"] == "ducklake"
    assert entry["error"] is None
    assert entry["uri"].startswith("ducklake:sqlite:")
    qualified = [t["qualified"] for t in entry["tables"]]
    assert "dl.ops.agent_test" in qualified
    assert "dl.ops.big" in qualified


@requires_ducklake
def test_describe_external_ducklake(ducklake_attached):
    engine = ducklake_attached["engine"]
    info = engine.describe_table("dl.ops.agent_test")
    assert info["source"] == "external" and info["read_only"] is True
    assert info["uri"].startswith("ducklake:sqlite:")
    names = [c["name"] for c in info["columns"]]
    assert names == ["k", "label"]


@requires_ducklake
def test_describe_external_rejects_injection(ducklake_attached):
    engine = ducklake_attached["engine"]
    with pytest.raises(LakehouseError):
        engine.describe_table("dl.ops.x; DROP TABLE y")


@requires_ducklake
def test_ducklake_attach_of_missing_catalog_fails_clean(ducklake_attached):
    """A wrong catalog path fails loudly at attach time, inside the scrubbed
    ExternalAttachError — no half-attached state, no secret echo (there is
    none for a sqlite catalog)."""
    import duckdb

    from sqlhandler.external import parse_attach_config

    env = _ducklake_spec_env(f"sqlite:{ducklake_attached['work']}/nope.db")
    env["SQLHANDLER_DUCKDB_EXTENSION_DIR"] = str(EXTDIR)
    (spec,) = parse_attach_config(env)
    con = duckdb.connect()
    try:
        with pytest.raises(ExternalAttachError, match="(?i)ducklake|probe"):
            apply_external(con, [spec], env)
    finally:
        con.close()


@requires_ducklake
def test_ducklake_data_files_not_readable_through_duckdb_fs(ducklake_attached):
    """The data parquet files under DATA_PATH are reachable ONLY through the
    ducklake catalog: a direct read_parquet of one of them still fails under
    the fs lockdown (the lockdown whitelist lives inside the extension, it
    does not re-open the fs to callers)."""
    import duckdb

    files = [os.path.join(dp, f) for dp, _, fn in os.walk(ducklake_attached["data"]) for f in fn if f.endswith(".parquet")]
    assert files, "fixture did not materialize parquet data files"
    env = _ducklake_spec_env(f"sqlite:{ducklake_attached['cat']}")
    env["SQLHANDLER_DUCKDB_EXTENSION_DIR"] = str(EXTDIR)
    specs = parse_attach_config(env)
    con = duckdb.connect()
    try:
        apply_external(con, specs, env)
        con.execute("SET disabled_filesystems='LocalFileSystem'")
        with pytest.raises(Exception, match="(?i)permission|disabled"):
            con.execute(f"SELECT count(*) FROM read_parquet('{files[0]}')").fetchall()
    finally:
        con.close()


def test_ducklake_load_probe_skip_reason_shape():
    """The skipif gate evaluates cleanly and carries a usable reason string."""
    ok, reason = _ducklake_loadable()
    assert isinstance(ok, bool) and isinstance(reason, str)
    if ok:
        assert reason == ""
