"""Tests for the 1.5.5-community attach types: mongodb and bigquery — and the
honest skips: clickhouse and motherduck.

No network, no real extensions: the community ``mongo`` / ``bigquery``
extensions are NOT in the repo's baked duckdb-ext/ tree (that directory is
owned by a separate build step), so every test here fakes the LOAD/ATTACH
boundary exactly like tests/test_external.py's _FakeCon — unit tests pin the
config parsing, the ATTACH SQL shapes, the secret scrubbing and the
not-baked error surfacing; the DuckDB-1.5.5 support facts they encode were
verified against duckdb.org's community-extension pages and the vendored
extension dir (see external.py's module docstring):

* ``mongodb`` -> community ``mongo`` extension, full ATTACH (TYPE mongo).
* ``bigquery`` -> community ``bigquery`` extension, full ATTACH
  (TYPE bigquery); auth is ADC or a project-scoped access-token DuckDB
  secret (CREATE OR REPLACE SECRET), never a DSN password.
* ``clickhouse`` -> NO 1.5.5 extension with ATTACH support (community repo
  for 1.5.5 carries chsql_native — scan functions only) -> refused loudly.
* ``motherduck`` -> bare ``md:`` ATTACH auto-installs a signed extension
  from MotherDuck's own repo (and can trigger an OAuth browser login) —
  outside the bake-and-LOAD trust boundary; MotherDuck rides the ducklake
  type instead (md: catalog + password_env, tested in test_ducklake.py).
"""

from __future__ import annotations

import json

import pytest

from sqlhandler.external import (
    _EXTENSIONS,
    ATTACH_TYPES,
    DEFAULT_PORTS,
    ExternalAttachError,
    apply_external,
    build_attach_sql,
    build_pre_attach_sql,
    ensure_extensions,
    parse_attach_config,
    scrub_secrets,
)
from sqlhandler.provider import LakehouseError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _env(entry: dict, **secrets: str) -> dict:
    env = {"SQLHANDLER_ATTACH": json.dumps([entry])}
    env.update(secrets)
    return env


# ---------------------------------------------------------------------------
# Type registry: the new types ride the existing structures
# ---------------------------------------------------------------------------


def test_new_types_are_registered():
    for t in ("mongodb", "bigquery"):
        assert t in ATTACH_TYPES
        assert t in _EXTENSIONS  # resolve to an extension to LOAD
        assert t in DEFAULT_PORTS or t in ("bigquery",)  # bigquery: no port
    assert DEFAULT_PORTS["mongodb"] == 27017
    assert _EXTENSIONS["mongodb"] == "mongo"  # the community extension's name
    assert _EXTENSIONS["bigquery"] == "bigquery"


def test_clickhouse_and_motherduck_are_not_types():
    # honest skips: a config entry must fail LOUDLY, not silently misbehave
    for t in ("clickhouse", "motherduck"):
        env = _env({"name": "x", "type": t, "host": "h", "database": "d", "password_env": ""})
        with pytest.raises(ValueError, match="type"):
            parse_attach_config(env)


# ---------------------------------------------------------------------------
# mongodb: config parsing
# ---------------------------------------------------------------------------


def test_parse_mongodb_happy_path():
    env = _env(
        {
            "name": "mdb",
            "type": "mongodb",
            "host": "mongo.internal",
            "database": "opsdb",
            "user": "ro",
            "password_env": "MPW",
        },
        MPW="s3cret",
    )
    (spec,) = parse_attach_config(env)
    assert (spec.name, spec.type, spec.host, spec.port, spec.database) == (
        "mdb",
        "mongodb",
        "mongo.internal",
        27017,  # the mongodb default port
        "opsdb",
    )
    assert spec.user == "ro"
    assert spec.password(env) == "s3cret"
    assert spec.display_uri == "mongodb://mongo.internal:27017/opsdb"  # no secret


def test_parse_mongodb_user_optional_no_default():
    # unlike postgres/mysql there is NO default user: an unauthenticated
    # (local/dev) attach is expressed by omitting user entirely
    env = _env({"name": "mdb", "type": "mongodb", "host": "h", "database": "d", "password_env": ""})
    (spec,) = parse_attach_config(env)
    assert spec.user == ""


def test_parse_mongodb_requires_database_scoping():
    # dbname= is required: without it the attach would expose EVERY database
    # on the server as a schema of the alias
    env = _env({"name": "mdb", "type": "mongodb", "host": "h", "password_env": ""})
    with pytest.raises(ValueError, match="database is required"):
        parse_attach_config(env)


def test_parse_mongodb_rejects_control_chars_in_database():
    env = _env(
        {"name": "mdb", "type": "mongodb", "host": "h", "database": "op\x00s", "password_env": ""}
    )
    with pytest.raises(ValueError, match="NUL/control"):
        parse_attach_config(env)


def test_parse_mongodb_params_passthrough_tls_srv():
    env = _env(
        {
            "name": "mdb",
            "type": "mongodb",
            "host": "cluster0.xxxxx.mongodb.net",
            "database": "opsdb",
            "password_env": "MPW",
            "params": {"srv": "true", "tls": "true"},
        },
        MPW="pw",
    )
    (spec,) = parse_attach_config(env)
    sql = build_attach_sql(spec, spec.password(env))
    assert "srv=true" in sql and "tls=true" in sql
    assert "TYPE mongo" in sql and "READ_ONLY" in sql


def test_parse_mongodb_rejects_duplicate_endpoint_params():
    # host/port/dbname/user/password ride the entry fields — params that
    # duplicate them are rejected by the SHARED _validate_params (inherited,
    # not reimplemented)
    env = _env(
        {
            "name": "mdb",
            "type": "mongodb",
            "host": "h",
            "database": "d",
            "password_env": "",
            "params": {"host": "other"},
        }
    )
    with pytest.raises(ValueError, match="not allowed"):
        parse_attach_config(env)


def test_parse_mongodb_rejects_whitespace_values():
    # The mongo DSN is serialized bare (space-separated key=value pairs —
    # the same shape as mysql_scanner's), so whitespace-bearing values are
    # rejected at startup with the field named.
    base = {"name": "mdb", "type": "mongodb", "host": "h", "database": "d", "password_env": ""}
    for entry, needle in (
        ({**base, "host": "my host"}, "field/value host"),
        ({**base, "database": "two words"}, "field/value database"),
        ({**base, "params": {"tls_ca_file": "/certs/a b.pem"}}, "field/value params.tls_ca_file"),
    ):
        with pytest.raises(ValueError, match=needle):
            parse_attach_config(_env(entry))


# ---------------------------------------------------------------------------
# mongodb: SQL generation + scrubbing
# ---------------------------------------------------------------------------


def test_build_attach_sql_mongodb_shape():
    spec = parse_attach_config(
        _env(
            {
                "name": "mdb",
                "type": "mongodb",
                "host": "mongo.internal",
                "database": "opsdb",
                "user": "ro",
                "password_env": "MPW",
            },
            MPW="pw1",
        )
    )[0]
    sql = build_attach_sql(spec, "pw1")
    assert sql.startswith("ATTACH 'host=mongo.internal port=27017 dbname=opsdb user=ro")
    assert "password=pw1" in sql
    assert 'AS "mdb" (TYPE mongo, READ_ONLY)' in sql


def test_build_attach_sql_mongodb_omits_absent_user_and_password():
    spec = parse_attach_config(
        _env({"name": "mdb", "type": "mongodb", "host": "h", "database": "d", "password_env": ""})
    )[0]
    sql = build_attach_sql(spec, None)
    assert "user=" not in sql and "password=" not in sql


def test_build_attach_sql_mongodb_rejects_whitespace_password_without_echo():
    env = _env(
        {
            "name": "mdb",
            "type": "mongodb",
            "host": "h",
            "database": "d",
            "user": "u",
            "password_env": "MPW",
        },
        MPW="pw1",
    )
    spec = parse_attach_config(env)[0]
    with pytest.raises(ValueError, match="resolved password contains whitespace") as ei:
        build_attach_sql(spec, "secret with spaces")
    assert "secret with spaces" not in str(ei.value)


def test_apply_external_mongodb_scrubs_password():
    env = _env(
        {
            "name": "mdb",
            "type": "mongodb",
            "host": "h",
            "database": "d",
            "user": "u",
            "password_env": "MPW",
        },
        MPW="pw1",
    )
    (spec,) = parse_attach_config(env)
    boom = _FakeCon(fail_on=2, error=Exception("auth failed for user=u password=pw1"))
    with pytest.raises(ExternalAttachError) as ei:
        apply_external(boom, [spec], env)
    assert "pw1" not in str(ei.value) and "***" in str(ei.value)


# ---------------------------------------------------------------------------
# bigquery: config parsing
# ---------------------------------------------------------------------------


def test_parse_bigquery_project_scope():
    env = _env({"name": "bq", "type": "bigquery", "database": "my-gcp-project"})
    (spec,) = parse_attach_config(env)
    assert spec.type == "bigquery"
    assert spec.host == "" and spec.port == 0  # no server endpoint
    assert spec.database == "my-gcp-project"
    assert spec.password_env == ""  # ADC is the default auth
    assert spec.display_uri == "bigquery://my-gcp-project"


def test_parse_bigquery_project_dataset_scope():
    env = _env({"name": "bq", "type": "bigquery", "database": "my-gcp-project.analytics"})
    (spec,) = parse_attach_config(env)
    assert spec.display_uri == "bigquery://my-gcp-project.analytics"


def test_parse_bigquery_rejects_server_fields():
    base = {"name": "bq", "type": "bigquery", "database": "proj"}
    for extra, needle in (
        ({"host": "h"}, "host"),
        ({"port": 443}, "port"),
        ({"user": "u"}, "user"),
        ({"user_env": "U"}, "user"),
        ({"params": {"foo": "bar"}}, "params"),  # bq_* knobs are DuckDB settings, not DSN keys
    ):
        with pytest.raises(ValueError, match=needle):
            parse_attach_config(_env({**base, **extra}))


def test_parse_bigquery_database_shape_is_enforced():
    base = {"name": "bq", "type": "bigquery", "password_env": ""}
    for bad in ("", "proj.dataset.tbl", "my project", "p;d", "p'd"):
        with pytest.raises(ValueError, match="database"):
            parse_attach_config(_env({**base, "database": bad}))


def test_parse_bigquery_password_env_optional_but_validated_when_given():
    base = {"name": "bq", "type": "bigquery", "database": "proj"}
    (spec,) = parse_attach_config(_env(base))
    assert spec.password() is None
    with pytest.raises(ValueError, match="MISSING_TOKEN"):
        parse_attach_config(_env({**base, "password_env": "MISSING_TOKEN"}))


def test_build_attach_sql_bigquery_shape_with_dataset_and_token():
    env = _env(
        {
            "name": "bq",
            "type": "bigquery",
            "database": "my-gcp-project.analytics",
            "password_env": "BQ_TOKEN",
        },
        BQ_TOKEN="ya29.temp-token-123",
    )
    (spec,) = parse_attach_config(env)
    # The token NEVER rides the ATTACH DSN — the extension's attach options
    # are project/dataset(/billing_project) only. It rides the pre-ATTACH
    # scoped secret instead (build_pre_attach_sql).
    sql = build_attach_sql(spec, spec.password(env))
    assert sql.startswith("ATTACH 'project=my-gcp-project dataset=analytics")
    assert "access_token" not in sql and "ya29.temp-token-123" not in sql
    assert "host=" not in sql and "port=" not in sql
    assert 'AS "bq" (TYPE bigquery, READ_ONLY)' in sql
    pre = build_pre_attach_sql(spec, spec.password(env))
    assert pre is not None
    assert 'CREATE OR REPLACE SECRET "bq"' in pre
    assert "SCOPE 'bq://my-gcp-project'" in pre  # scoped to the data project
    assert "ACCESS_TOKEN 'ya29.temp-token-123'" in pre
    # scrubbing removes the token again (both the secret stmt and the DSN)
    assert "ya29.temp-token-123" not in scrub_secrets(pre, [spec], env)
    assert "ya29.temp-token-123" not in scrub_secrets(sql, [spec], env)


def test_build_attach_sql_bigquery_without_token_has_no_access_token_key():
    (spec,) = parse_attach_config(_env({"name": "bq", "type": "bigquery", "database": "proj"}))
    sql = build_attach_sql(spec, None)
    assert "access_token" not in sql
    assert sql == 'ATTACH \'project=proj\' AS "bq" (TYPE bigquery, READ_ONLY)'
    # ADC is the default auth: no token resolved -> NO secret statement at all
    assert build_pre_attach_sql(spec, None) is None


def test_build_attach_sql_bigquery_billing_project():
    (spec,) = parse_attach_config(
        _env(
            {
                "name": "bq",
                "type": "bigquery",
                "database": "bigquery-public-data.geo_us_boundaries",
                "billing_project": "my-gcp-project",
            }
        )
    )
    sql = build_attach_sql(spec, None)
    assert sql == (
        "ATTACH 'project=bigquery-public-data dataset=geo_us_boundaries "
        'billing_project=my-gcp-project\' AS "bq" (TYPE bigquery, READ_ONLY)'
    )


def test_parse_bigquery_billing_project_validation():
    base = {"name": "bq", "type": "bigquery", "database": "proj"}
    (spec,) = parse_attach_config(_env({**base, "billing_project": "my-gcp-project"}))
    assert spec.billing_project == "my-gcp-project"
    for bad in ("my project", "p;d", "p'd"):
        with pytest.raises(ValueError, match="billing_project"):
            parse_attach_config(_env({**base, "billing_project": bad}))
    # billing_project is bigquery-only: every other type rejects it loudly
    for other in (
        {"name": "x", "type": "postgres", "host": "h", "database": "d", "password_env": ""},
        {"name": "x", "type": "mongodb", "host": "h", "database": "d", "password_env": ""},
        {"name": "x", "type": "ducklake", "catalog": "sqlite:/tmp/c.db"},
    ):
        with pytest.raises(ValueError, match="billing_project"):
            parse_attach_config(_env({**other, "billing_project": "proj"}))


def test_bigquery_token_never_reaches_display_uri():
    env = _env(
        {"name": "bq", "type": "bigquery", "database": "proj", "password_env": "BQ_TOKEN"},
        BQ_TOKEN="ya29.temp-token-123",
    )
    (spec,) = parse_attach_config(env)
    assert "ya29.temp-token-123" not in spec.display_uri
    assert spec.display_uri == "bigquery://proj"


def test_apply_external_bigquery_loads_and_attaches():
    env = _env(
        {
            "name": "bq",
            "type": "bigquery",
            "database": "proj.ds",
            "password_env": "BQ_TOKEN",
        },
        BQ_TOKEN="tok1",
    )
    (spec,) = parse_attach_config(env)
    con = _FakeCon()
    apply_external(con, [spec], env)
    # Sequencing: LOAD -> pre-ATTACH secret (token present) -> ATTACH. The
    # token rides the scoped secret, never the ATTACH DSN.
    assert con.calls == [
        "LOAD bigquery",
        (
            'CREATE OR REPLACE SECRET "bq" (TYPE bigquery, SCOPE \'bq://proj\', '
            "ACCESS_TOKEN 'tok1')"
        ),
        'ATTACH \'project=proj dataset=ds\' AS "bq" (TYPE bigquery, READ_ONLY)',
    ]


def test_apply_external_bigquery_without_token_emits_no_secret_statement():
    # ADC is the default auth: no password_env value -> no CREATE SECRET at
    # all, the statement sequence is LOAD -> ATTACH exactly like other types.
    env = _env({"name": "bq", "type": "bigquery", "database": "proj.ds"})
    (spec,) = parse_attach_config(env)
    con = _FakeCon()
    apply_external(con, [spec], env)
    assert con.calls == [
        "LOAD bigquery",
        'ATTACH \'project=proj dataset=ds\' AS "bq" (TYPE bigquery, READ_ONLY)',
    ]


def test_apply_external_bigquery_secret_error_is_scrubbed():
    env = _env(
        {"name": "bq", "type": "bigquery", "database": "proj", "password_env": "BQ_TOKEN"},
        BQ_TOKEN="tok1",
    )
    (spec,) = parse_attach_config(env)
    # The CREATE SECRET statement itself fails (e.g. the extension rejects
    # the token) — the raised error is scrubbed: the parameter NAME may
    # survive (it is the diagnostic), the token VALUE never does.
    boom = _FakeCon(fail_on=2, error=Exception("invalid ACCESS_TOKEN 'tok1' for project"))
    with pytest.raises(ExternalAttachError) as ei:
        apply_external(boom, [spec], env)
    msg = str(ei.value)
    assert "tok1" not in msg and "***" in msg
    assert "ACCESS_TOKEN" in msg  # the key name survives as the diagnostic


# ---------------------------------------------------------------------------
# Literal-secret rejection is INHERITED, not reimplemented
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("db_type,database,host", [
    ("mongodb", "d", "h"),
    ("bigquery", "proj", ""),
])
def test_literal_password_key_rejected_for_new_types(db_type, database, host):
    entry = {"name": "x", "type": db_type, "database": database, "password": "oops"}
    if host:
        entry["host"] = host
    with pytest.raises(ValueError, match="password_env"):
        parse_attach_config(_env(entry))


@pytest.mark.parametrize("db_type,database,host", [
    ("mongodb", "d", "h"),
    ("bigquery", "proj", ""),
])
def test_literal_password_in_params_rejected_for_new_types(db_type, database, host):
    entry = {
        "name": "x",
        "type": db_type,
        "database": database,
        "password_env": "",
        "params": {"password": "oops"},
    }
    if host:
        entry["host"] = host
    with pytest.raises(ValueError, match="password_env"):
        parse_attach_config(_env(entry))


def test_missing_password_env_var_rejected_for_new_types():
    env = _env(
        {"name": "mdb", "type": "mongodb", "host": "h", "database": "d", "password_env": "NOT_SET"}
    )
    with pytest.raises(ValueError, match="NOT_SET"):
        parse_attach_config(env)


# ---------------------------------------------------------------------------
# ensure_extensions maps the right extension names; not-baked error surfaces
# ---------------------------------------------------------------------------


def test_ensure_extensions_loads_community_extensions_for_new_types():
    con = _FakeCon()
    ensure_extensions(con, {"mongodb", "bigquery"})
    assert con.calls == ["LOAD bigquery", "LOAD mongo"]  # sorted, no dup


def test_not_baked_extension_fails_with_friendly_scrubbed_error():
    """The not-baked path: DuckDB's LOAD raises its raw IO error (what a
    default-image bake list missing mongo/bigquery produces — "Extension …
    not found", naming the .so file but not the FIX); apply_external must
    surface it as the scrubbed ExternalAttachError carrying the friendly
    not-baked hint (extension name + the Dockerfile opt-in), never a
    secret."""
    env = _env(
        {
            "name": "mdb",
            "type": "mongodb",
            "host": "h",
            "database": "d",
            "user": "u",
            "password_env": "MPW",
        },
        MPW="pw1",
    )
    (spec,) = parse_attach_config(env)
    duckdb_io_error = Exception(
        'IO Error: Extension "mongo.duckdb_extension" not found. '
        'Install it first using "INSTALL mongo" password=pw1.'
    )
    boom = _FakeCon(fail_on=1, error=duckdb_io_error)  # the LOAD call fails
    with pytest.raises(ExternalAttachError) as ei:
        apply_external(boom, [spec], env)
    msg = str(ei.value)
    # the friendly wrap: names the extension AND the build-time fix
    assert "not baked into this image" in msg
    assert "INSTALL mongo FROM community" in msg
    assert "Dockerfile" in msg
    # the raw DuckDB detail survives too (it names the missing file)
    assert 'Extension "mongo.duckdb_extension" not found' in msg
    assert "pw1" not in msg and "***" in msg  # secrets never ride the error


def test_not_baked_wrap_is_generic_not_bigquery_or_mongo_only():
    """The LOAD-failure wrap keys on the failing LOAD, not on the two new
    types — any type whose extension is missing gets the same hint."""
    con = _FakeCon(
        fail_on=1,
        error=Exception('IO Error: Extension "mssql.duckdb_extension" not found.'),
    )
    with pytest.raises(LakehouseError, match="not baked into this image") as ei:
        ensure_extensions(con, {"sqlserver"})
    assert "INSTALL mssql FROM community" in str(ei.value)


def test_not_baked_wrap_preserves_nonsense_load_error():
    """Any LOAD failure (not just the not-found shape) is wrapped — the hint
    is additive, the original message stays the prefix."""
    con = _FakeCon(fail_on=1, error=Exception("some unrelated LOAD failure"))
    with pytest.raises(LakehouseError, match="not baked into this image") as ei:
        ensure_extensions(con, {"bigquery"})
    assert "some unrelated LOAD failure" in str(ei.value)
    assert "INSTALL bigquery FROM community" in str(ei.value)
