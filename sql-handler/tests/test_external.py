"""Tests for the read-only external-database attach support.

Three layers:

* Unit tests (fake DuckDB connection) pin the config parsing, ATTACH SQL
  generation (all five types, plus the ``params`` passthrough), secret
  scrubbing, and alias detection — the pieces where a regression would be
  silent.
* Integration tests run against a REAL Postgres via the ``pgserver``
  package (embedded binaries; skipped when not installed) and against a
  real sqlite file through the baked ``sqlite_scanner`` extension (skipped
  with an explicit reason when the extension is not baked into
  ``duckdb-ext/`` yet — a separate build step owns that directory). They
  pin the security-critical behaviors of the production sequence — ATTACH
  before the fs lockdown, SELECT working with LocalFileSystem disabled,
  writes rejected by the READ_ONLY attach, and the fs lockdown still
  holding.
"""

import json
import os
import tempfile
from pathlib import Path

import pyarrow as pa
import pytest


# pgserver resolves its runtime/lock dir via platformdirs at import time —
# point XDG_RUNTIME_DIR at a writable location BEFORE the (lazy) import.
def _pg_runtime_dir() -> str:
    """A creatable XDG_RUNTIME_DIR (sandboxed CIs may pre-set an unwritable one)."""
    candidate = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(tempfile.gettempdir(), "sqlhandler-pg-runtime")
    try:
        os.makedirs(candidate, exist_ok=True)
        return candidate
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), "sqlhandler-pg-runtime")
        os.makedirs(fallback, exist_ok=True)
        return fallback


os.environ["XDG_RUNTIME_DIR"] = _pg_runtime_dir()

from sqlhandler.engine import SqlEngine  # noqa: E402
from sqlhandler.external import (  # noqa: E402
    AttachSpec,
    ExternalAttachError,
    apply_external,
    build_attach_sql,
    ensure_extensions,
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
# New attach types: mariadb alias, sqlite, sqlserver, params passthrough
# ---------------------------------------------------------------------------


def test_postgresql_spelling_not_accepted(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "x", "type": "postgresql", "host": "h", "database": "d", "password_env": ""}]),
    )
    with pytest.raises(ValueError, match="type.*not supported"):
        parse_attach_config()


def test_parse_mariadb_alias_keeps_type_and_defaults_to_root(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "mr", "type": "mariadb", "host": "db", "database": "d", "password_env": "PW"}]),
    )
    monkeypatch.setenv("PW", "pw")
    spec = parse_attach_config()[0]
    assert spec.type == "mariadb"  # the operator-facing spelling survives
    assert (spec.port, spec.user) == (3306, "root")  # mariadb shares mysql's defaults
    assert spec.display_uri == "mariadb://db:3306/d"


def test_parse_sqlserver_requires_explicit_user(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "ms", "type": "sqlserver", "host": "h", "database": "d", "password_env": ""}]),
    )
    with pytest.raises(ValueError, match="sqlserver.*'user'"):
        parse_attach_config()  # no default user — never fall back to 'sa'
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps(
            [{"name": "ms", "type": "sqlserver", "host": "h", "database": "d", "user": "ro", "password_env": "PW"}]
        ),
    )
    monkeypatch.setenv("PW", "pw")
    spec = parse_attach_config()[0]
    assert (spec.type, spec.port, spec.user) == ("sqlserver", 1433, "ro")  # 1433 default port


def test_parse_sqlite_happy_path(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "sdb", "type": "sqlite", "database": "/data/ops.db"}]),  # password_env omitted
    )
    spec = parse_attach_config()[0]
    assert (spec.host, spec.port, spec.user, spec.password_env) == ("", 0, "", "")
    assert spec.database == "/data/ops.db"
    assert spec.display_uri == "sqlite:///data/ops.db"  # a file path — no port, no secret
    assert spec.password() is None


def test_sqlite_rejects_server_fields(monkeypatch):
    base = {"name": "sdb", "type": "sqlite", "database": "/data/ops.db"}
    for extra, needle in (
        ({"host": "srv"}, "host"),
        ({"port": 1234}, "port"),
        ({"user": "u"}, "user"),
        ({"user_env": "U"}, "user"),
        ({"params": {"sslmode": "require"}}, "params"),
    ):
        monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([{**base, **extra}]))
        with pytest.raises(ValueError, match=needle):
            parse_attach_config()


def test_sqlite_database_is_minimally_validated_file_path(monkeypatch):
    base = {"name": "sdb", "type": "sqlite"}
    monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([base]))
    with pytest.raises(ValueError, match="database is required"):
        parse_attach_config()
    monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([{**base, "database": "/data/op\x00s.db"}]))
    with pytest.raises(ValueError, match="NUL/control"):
        parse_attach_config()
    monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([{**base, "database": "/data/ops db (v2).db"}]))
    assert parse_attach_config()[0].database == "/data/ops db (v2).db"  # spaces/parens are fine


def test_sqlite_password_env_omitted_ok_but_validated_when_given(monkeypatch):
    base = {"name": "sdb", "type": "sqlite", "database": "/data/ops.db"}
    monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([base]))
    assert parse_attach_config()[0].password() is None
    monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([{**base, "password_env": "MISSING_PW"}]))
    monkeypatch.delenv("MISSING_PW", raising=False)
    with pytest.raises(ValueError, match="MISSING_PW"):
        parse_attach_config()  # when given, the env var must still exist


def _params_entry(params: object) -> dict:
    return {
        "name": "ops",
        "type": "postgres",
        "host": "h",
        "database": "d",
        "password_env": "",
        "params": params,
    }


def test_params_parse_and_coerce_scalars(monkeypatch):
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([_params_entry({"sslmode": "require", "keepalives": 1, "verbose": True})]),
    )
    spec = parse_attach_config()[0]
    # non-string JSON scalars are str()-coerced; insertion order preserved
    assert spec.params == (("sslmode", "require"), ("keepalives", "1"), ("verbose", "True"))


def test_params_rejects_nested_values_with_typeerror(monkeypatch):
    for params in ({"sslmode": {"deeper": 1}}, {"sslmode": ["require"]}, "not-an-object"):
        monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([_params_entry(params)]))
        with pytest.raises(TypeError, match="scalar|JSON object"):
            parse_attach_config()  # wrong JSON *type* -> TypeError (entry-level convention)


def test_params_rejects_unsafe_values(monkeypatch):
    cases = [
        ({"sslmode": "x'y"}, "allowed set"),  # single quote
        ({"sslmode": "a\\b"}, "allowed set"),  # backslash
        ({"sslmode": "a;b"}, "allowed set"),  # semicolon (ODBC key separator)
        ({"sslmode": "{a}"}, "allowed set"),  # braces (ODBC quoting)
        ({"sslmode": "a\tb"}, "allowed set"),  # control character
        ({"sslmode": "x" * 257}, "256"),  # too long
    ]
    for params, needle in cases:
        monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([_params_entry(params)]))
        with pytest.raises(ValueError, match=needle):
            parse_attach_config()


def test_params_rejects_invalid_and_reserved_keys(monkeypatch):
    cases = [
        ("1sslmode", "not a valid connection parameter"),  # must start with a letter
        ("ssl-mode", "not a valid connection parameter"),  # dash not allowed
        ("PASSword", "not allowed"),  # secrets, caught case-insensitively
        ("passwd", "not allowed"),
        ("secret", "not allowed"),
        ("sslpassword", "not allowed"),
        ("Host", "not allowed"),  # duplicates of the entry's own fields
        ("PORT", "not allowed"),
        ("dbname", "not allowed"),
        ("trusted_connection", "not allowed"),
    ]
    for key, needle in cases:
        monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([_params_entry({key: "x"})]))
        with pytest.raises(ValueError, match=needle):
            parse_attach_config()


def test_mysql_mariadb_rejects_whitespace_values(monkeypatch):
    # mysql_scanner serializes DSN values BARE (its parser splits on
    # whitespace and does not strip quotes) — whitespace would corrupt the
    # DSN parse, so it is rejected at startup with the field named.
    base = {"name": "mx", "type": "mysql", "host": "h", "database": "db", "password_env": ""}
    cases = [
        ({**base, "host": "my host"}, "field/value host"),
        ({**base, "database": "two words"}, "field/value database"),
        ({**base, "params": {"ssl_mode": "verify identity"}}, "field/value params.ssl_mode"),
        ({**base, "user_env": "MX_USER"}, "field/value user"),  # user_env-resolved value
        ({**base, "type": "mariadb", "user": "svc user"}, "field/value user"),
    ]
    for entry, needle in cases:
        monkeypatch.setenv("SQLHANDLER_ATTACH", json.dumps([entry]))
        monkeypatch.setenv("MX_USER", "svc user")
        with pytest.raises(ValueError, match=needle):
            parse_attach_config()


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


def test_build_attach_sql_postgres_params_merge_without_duplicates():
    spec = AttachSpec(
        "ops",
        "postgres",
        "h",
        5432,
        "db",
        "u",
        "PW",
        True,
        (("sslmode", "require"), ("application_name", "sqlhandler")),
    )
    sql = build_attach_sql(spec, "pw")
    # values are _sql_quote'd ('require') and then doubled once more at the
    # ATTACH-literal level (''require'') — see the escape test above
    assert "sslmode=''require''" in sql and "application_name=''sqlhandler''" in sql
    assert "connect_timeout=10" in sql  # default kept alongside the added params
    assert sql.count("host=") == 1 and sql.count("sslmode=") == 1  # no duplicate keys
    assert "TYPE postgres" in sql and "READ_ONLY" in sql


def test_build_attach_sql_params_override_defaults():
    spec = AttachSpec("ops", "postgres", "h", 5432, "db", "u", "PW", True, (("connect_timeout", "30"),))
    sql = build_attach_sql(spec, None)
    assert "connect_timeout=''30''" in sql and sql.count("connect_timeout") == 1
    assert "connect_timeout=10" not in sql  # the default was replaced, not duplicated


def test_build_attach_sql_mysql_tls_param_forwarded_verbatim():
    spec = AttachSpec("mx", "mysql", "h", 3306, "db", "u", "PW", True, (("ssl_mode", "verify_identity"),))
    sql = build_attach_sql(spec, "pw")
    assert "ssl_mode=verify_identity" in sql  # libmariadb TLS key — NOT postgres' sslmode
    assert "passwd=pw" in sql and "database=db" in sql and "dbname=" not in sql
    assert sql.count("ssl_mode=") == 1 and sql.count("passwd=") == 1


def test_build_attach_sql_mariadb_attaches_as_mysql():
    spec = AttachSpec("mr", "mariadb", "h", 3306, "db", "root", "PW")
    sql = build_attach_sql(spec, "pw")
    assert "TYPE mysql" in sql  # mariadb rides the mysql attach type...
    assert "passwd=pw" in sql and "READ_ONLY" in sql
    assert "TYPE mariadb" not in sql


def test_build_attach_sql_mysql_mariadb_values_are_bare():
    # mysql_scanner's DSN parser splits on whitespace and does NOT strip
    # quotes: a quoted value would carry literal quote characters into the
    # connection (empirically host='127.0.0.1' fails with "Unknown server
    # host ''127.0.0.1''"). Values must therefore be emitted bare.
    for db_type in ("mysql", "mariadb"):
        spec = AttachSpec("mx", db_type, "db.internal", 3307, "opsdb", "svc", "PW")
        sql = build_attach_sql(spec, "pw1")
        assert "host=db.internal" in sql
        assert "port=3307" in sql
        assert "database=opsdb" in sql
        assert "user=svc" in sql
        assert "passwd=pw1" in sql
        assert "host='" not in sql  # no quoted DSN values anywhere
        assert "''" not in sql  # nothing to double-escape for quote-free values
        assert "TYPE mysql" in sql and "READ_ONLY" in sql


def test_build_attach_sql_mysql_rejects_whitespace_password_without_echo():
    # A whitespace-bearing password cannot ride the bare DSN: fail loudly
    # BEFORE embedding it, and never echo the secret in the message.
    spec = AttachSpec("mx", "mysql", "h", 3306, "db", "u", "PW")
    with pytest.raises(ValueError, match="resolved password contains whitespace") as ei:
        build_attach_sql(spec, "secret with spaces")
    assert "secret with spaces" not in str(ei.value)
    # mariadb rides the same guard
    with pytest.raises(ValueError, match="resolved password contains whitespace"):
        build_attach_sql(AttachSpec("mr", "mariadb", "h", 3306, "db", "root", "PW"), "pw\ttab")


def test_apply_external_surfaces_mysql_password_error_scrubbed():
    # The attach-time guard must surface as ExternalAttachError through
    # apply_external's scrub path — with the secret still absent.
    spec = AttachSpec("mx", "mysql", "h", 3306, "db", "u", "PW")
    with pytest.raises(ExternalAttachError, match="resolved password contains whitespace") as ei:
        apply_external(_FakeCon(), [spec], {"PW": "secret with spaces"})
    assert "secret with spaces" not in str(ei.value)


def test_postgres_params_value_with_space_still_quoted(monkeypatch):
    # libpq's conninfo parser strips quotes, so postgres keeps _sql_quote —
    # values with spaces ride fine there (unlike mysql/mariadb, where any
    # whitespace is rejected at parse time).
    monkeypatch.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps(
            [
                {
                    "name": "ops",
                    "type": "postgres",
                    "host": "h",
                    "database": "d",
                    "password_env": "",
                    "params": {"options": "-c statement_timeout=30000"},
                }
            ]
        ),
    )
    spec = parse_attach_config()[0]  # parses fine — the space is legal for libpq
    sql = build_attach_sql(spec, None)
    assert "options=''-c statement_timeout=30000''" in sql  # quoted + ATTACH-literal doubled


def test_build_attach_sql_sqlserver_conn_string_defaults():
    spec = AttachSpec("ms", "sqlserver", "sql.internal", 1433, "adw", "ro", "MS_PW")
    sql = build_attach_sql(spec, "pw1")
    assert 'AS "ms" (TYPE mssql, READ_ONLY)' in sql
    assert "Server=sql.internal,1433;Database=adw;Uid=ro;Pwd=pw1;Encrypt=yes;TrustServerCertificate=yes" in sql


def test_build_attach_sql_sqlserver_params_overlay_case_insensitive():
    spec = AttachSpec(
        "ms",
        "sqlserver",
        "h",
        1433,
        "d",
        "u",
        "PW",
        True,
        (("application_name", "sqlhandler"), ("encrypt", "yes")),  # new key + case-insensitive override
    )
    sql = build_attach_sql(spec, "pw")
    assert "application_name=sqlhandler" in sql
    assert sql.count("Encrypt=") + sql.count("encrypt=") == 1  # override replaced the default in place
    assert sql.count("Pwd=") == 1


def test_build_attach_sql_sqlite_is_just_the_file_path():
    spec = AttachSpec("sdb", "sqlite", "", 0, "/data/ops.db", "", "")
    sql = build_attach_sql(spec, None)
    assert sql == 'ATTACH \'/data/ops.db\' AS "sdb" (TYPE sqlite, READ_ONLY)'
    # a quote in the operator-authored path is doubled at the ATTACH-literal level
    quoted = build_attach_sql(AttachSpec("sdb", "sqlite", "", 0, "/data/a'b.db", "", ""), None)
    assert quoted == 'ATTACH \'/data/a\'\'b.db\' AS "sdb" (TYPE sqlite, READ_ONLY)'


def test_build_attach_sql_unknown_type_fails_loud():
    with pytest.raises(ValueError, match="unknown attach type"):
        build_attach_sql(AttachSpec("x", "mssql", "h", 1433, "d", "u", ""), None)


def test_ensure_extensions_maps_config_types_to_extensions():
    con = _FakeCon()
    ensure_extensions(con, {"postgres", "mysql", "mariadb", "sqlite", "sqlserver"})
    loads = [c for c in con.calls if c.startswith("LOAD")]
    # sorted() determinism on the config types, each resolved via _EXTENSIONS
    assert loads == [
        "LOAD mysql_scanner",  # mariadb (alias)
        "LOAD mysql_scanner",  # mysql
        "LOAD postgres_scanner",
        "LOAD sqlite_scanner",
        "LOAD mssql",  # sqlserver -> the community mssql extension
    ]


def test_ensure_extensions_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown attach type"):
        ensure_extensions(_FakeCon(), {"oracle"})


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
    table = engine.query_duckdb("SELECT count(*) AS n FROM lake_tbl l JOIN ops.pg_catalog.pg_type p ON p.oid > 0")
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
    con.execute(f"ATTACH 'host={host} dbname=postgres user=postgres' AS w (TYPE postgres)")
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


# ---------------------------------------------------------------------------
# Integration: a real sqlite file through the baked sqlite_scanner extension.
# The scanner .so is baked into <repo>/duckdb-ext/ by a separate build step
# that runs in parallel — when it has not landed yet these tests SKIP with an
# explicit reason instead of failing.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sqlite_attached(tmp_path_factory):
    """Engine with a small real sqlite file attached via sqlite_scanner."""
    db_path = tmp_path_factory.mktemp("sqlhandler-sqlite") / "ops.db"
    import sqlite3

    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE agent_test (k INTEGER, label TEXT)")
    con.executemany("INSERT INTO agent_test VALUES (?, ?)", [(1, "a"), (2, "b")])
    con.commit()
    con.close()

    extdir = Path(__file__).resolve().parent.parent / "duckdb-ext"
    # Probe the LOAD on a throwaway connection BEFORE building the engine: a
    # missing/mismatched sqlite_scanner bake must surface as a skip.
    import duckdb

    probe = duckdb.connect()
    try:
        probe.execute(f"SET extension_directory='{extdir}'")
        probe.execute("LOAD sqlite_scanner")
    except Exception as exc:
        pytest.skip(
            f"sqlite_scanner extension not baked in <repo>/duckdb-ext yet (parallel bake step): {exc}"
        )
    finally:
        probe.close()

    monkey = pytest.MonkeyPatch()
    monkey.setenv(
        "SQLHANDLER_ATTACH",
        json.dumps([{"name": "sdb", "type": "sqlite", "database": str(db_path)}]),
    )
    monkey.setenv("SQLHANDLER_DUCKDB_EXTENSION_DIR", str(extdir))
    engine = SqlEngine(_OneLakeTable())
    yield engine
    monkey.undo()


def test_engine_reads_attached_sqlite_under_lockdown(sqlite_attached):
    engine = sqlite_attached
    table = engine.query_duckdb("SELECT count(*) AS n FROM sdb.main.agent_test")
    assert table.column("n").to_pylist() == [2]


def test_engine_mixed_lake_and_sqlite_join(sqlite_attached):
    engine = sqlite_attached
    table = engine.query_duckdb("SELECT count(*) AS n FROM lake_tbl l JOIN sdb.main.agent_test a ON a.k > 0")
    assert table.column("n").to_pylist()[0] > 0


def test_engine_sqlite_writes_rejected(sqlite_attached):
    engine = sqlite_attached
    with pytest.raises(Exception, match="(?i)read.?only|CREATE"):
        engine.query_duckdb("CREATE TABLE sdb.main.hacked (x int)")


def test_engine_attached_sqlite_lists_tables(sqlite_attached):
    engine = sqlite_attached
    listing = engine.attached_databases(refresh=True)
    assert len(listing) == 1 and listing[0]["name"] == "sdb"
    assert listing[0]["error"] is None
    assert listing[0]["uri"].startswith("sqlite://")
    qualified = [t["qualified"] for t in listing[0]["tables"]]
    assert "sdb.main.agent_test" in qualified
    rows = engine.query_duckdb("SELECT count(*) AS n FROM sdb.main.agent_test")
    assert rows.column("n").to_pylist() == [2]
