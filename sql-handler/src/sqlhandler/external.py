"""Read-only external-database attach support (Postgres / MySQL / SQLite / SQL Server).

SQLhandler's engine is a *lake* engine: tables are Parquet/Delta datasets
opened by pyarrow and registered into DuckDB as views. Some questions need
the *system of record* instead — the operational database behind the lake.
This module attaches such servers to a query connection via DuckDB's
scanner extensions, strictly read-only, so one query can JOIN lake tables
with live operational data:

    SELECT o.status, count(*)
    FROM ops.public.work_orders o           -- attached Postgres (read-only)
    JOIN work_order_header w ON w.id = o.id -- registered lake table
    GROUP BY 1

Supported config ``type``s and the DuckDB extension each one LOADs:

* ``postgres``  -> ``postgres_scanner``  (ATTACH ``TYPE postgres``)
* ``mysql``     -> ``mysql_scanner``     (ATTACH ``TYPE mysql``)
* ``mariadb``   -> ``mysql_scanner``     (ATTACH ``TYPE mysql`` — alias: same
  wire protocol, so the ATTACH says ``mysql`` while ``spec.type`` keeps the
  operator's ``mariadb`` spelling for config and tool output)
* ``sqlite``     -> ``sqlite_scanner``    (ATTACH ``TYPE sqlite``)
* ``sqlserver``  -> ``mssql``             (ATTACH ``TYPE mssql``)
* ``mongodb``    -> ``mongo``             (community; ATTACH ``TYPE mongo``)
* ``bigquery``   -> ``bigquery``          (community; ATTACH ``TYPE bigquery``)

``"postgresql"`` is deliberately NOT accepted (the config key is spelled
``postgres``). ``mssql`` is a *community* extension (native TDS 7.4 with
TLS — no unixODBC or Microsoft ODBC driver required) and, like the core
scanner extensions, must be PREINSTALLED in the image (auto-install stays
off — no runtime downloads). The same build-time baking applies to the
community ``mongo`` and ``bigquery`` extensions (see the Dockerfile: the
default bake list does NOT include them — a 404 at build time would fail
the image build — so they are opt-in via a one-line bake extension shown
as a commented example there). Two further types are deliberately NOT
config types (honest skips, see below): ``clickhouse`` and ``motherduck``.

Security posture (mirrors :func:`sqlhandler.engine._duckdb_fs_lockdown`):

* ``READ_ONLY`` is forced on every ATTACH — DuckDB itself rejects
  INSERT/UPDATE/DELETE/CREATE on the attached catalog. The database is a
  source, never a sink.
* Credentials never pass through the model or any tool output: the config
  carries env-var *names* (``password_env``); the value is read from the
  process environment at attach time, a raw ``password`` key in the config
  is rejected outright (at the entry level AND inside ``params``), and
  every error message is scrubbed of resolved secrets before it can reach
  a client.
* The connection keeps ``disabled_filesystems='LocalFileSystem'`` —
  attaching a database does NOT re-open DuckDB file reads (no
  ``read_parquet('/etc/passwd')``, no ``COPY ... TO``). Only the scanner
  extensions are LOADed, explicitly, before the lockdown.

Per-type notes:

* ``sqlite`` is a FILE, not a server: ``database`` is the sqlite database
  FILE PATH (operator-authored, exactly like ``host`` for the server types —
  the same trust model as the nfs provider). The file is opened at query
  time by the sqlite3 library bundled inside the scanner extension, NOT
  through DuckDB's filesystem layer, so the ``disabled_filesystems``
  lockdown does not apply to that read (only the extension LOAD itself
  does). There is no host/port/user for sqlite, ``password_env`` may be
  omitted entirely, and ``params`` are not accepted.
* ``sqlserver``: the DSN is an ODBC/ADO.NET-style ``Key=value`` connection
  string and TLS is on by default. ``Encrypt=yes;TrustServerCertificate=yes``
  are appended as lenient defaults (self-signed server certificates are
  accepted); operator ``params`` are appended after them and override them.
  Note that in the mssql extension ``TrustServerCertificate`` is a synonym
  of ``Encrypt`` (a conflicting pair fails the ATTACH loudly). The
  extension validates credentials eagerly at ATTACH time.
* ``ducklake``: DuckDB's lakehouse format — a SQL catalog database (sqlite/
  postgres/MotherDuck/…) storing schemas/tables/snapshots/stats that point
  at Parquet data files on disk or object storage. ``catalog`` is the connect
  string WITHOUT the ``ducklake:`` prefix (operator-authored, sqlite-file /
  host trust model); optional ``data_path`` (DATA_PATH) only matters when the
  attach CREATES a new DuckLake — an existing catalog ignores it; optional
  ``password_env`` carries MotherDuck tokens. The extension is a CORE
  extension on 1.5.x (vendored with the scanners). The ATTACH must NOT carry
  ``TYPE`` — the ``ducklake:`` URL scheme selects the handler; with an
  explicit ``TYPE ducklake`` DuckDB 1.5.5 refuses to probe the existing
  catalog ("creating a new DuckLake is explicitly disabled"). READ_ONLY
  attach IS supported and enforced by DuckDB itself. Empirically on 1.5.5:
  ducklake reads its DATA (inlined catalog rows AND local parquet files
  under data_path) through DuckDB's own filesystem layer, so with the
  ``disabled_filesystems='LocalFileSystem'`` lockdown ON only metadata
  answers (count(*)/min/max from catalog stats) are served and row reads
  fail closed; an operator who needs row reads sets the engine's existing
  SQLHANDLER_DUCKDB_FILE_ACCESS=1 opt-out — writes stay refused regardless
  (READ_ONLY attach + the unconditional sqlguard layer, neither depends on
  that flag).
* ``mongodb``: the community ``mongo`` extension (duckdb.org community
  repo; full ATTACH support on 1.5.5 — ``ATTACH 'host=… port=27017
  dbname=…' AS m (TYPE mongo, READ_ONLY)``, MongoDB URI strings also
  accepted). DSN keys follow the extension's documented connection
  parameters (host/port/user/password/dbname/authsource/srv/tls/
  tls_ca_file/options); ``database`` scopes the attach to one MongoDB
  database (it becomes the catalog's single schema — without it the
  attach exposes every database as a schema, which is a wider blast
  radius than the alias implies). NOT baked by default — see the
  Dockerfile's opt-in comment.
* ``bigquery``: the community ``bigquery`` extension (hafenkran/duckdb-
  bigquery; full ATTACH support on 1.5.5 — ``ATTACH 'project=p
  dataset=d' AS bq (TYPE bigquery, READ_ONLY)``). ``database`` IS the
  ``project`` or ``project.dataset`` scope (datasets become schemas);
  there is no host/port. Auth is NOT part of the ATTACH DSN: the
  extension resolves Google Application Default Credentials
  (``GOOGLE_APPLICATION_CREDENTIALS``, workload identity, gcloud ADC) or
  a DuckDB Secret scoped to ``bq://<project>`` — the ATTACH options are
  ``project``/``dataset``/``billing_project`` ONLY (there is no
  ``access_token`` ATTACH key). ``password_env`` carries a temporary
  OAuth2 ACCESS_TOKEN, applied via a scoped
  ``CREATE OR REPLACE SECRET … (TYPE bigquery, SCOPE 'bq://<project>',
  ACCESS_TOKEN '…')`` emitted BEFORE the ATTACH (see
  :func:`build_pre_attach_sql`) — the token never rides the DSN or
  ``display_uri``; long-lived service-account keys belong in the ADC
  file, outside config entirely. NOT baked by default — see the
  Dockerfile's opt-in comment.
* Deliberately NOT accepted (skipped honestly rather than half-supported):
  ``clickhouse`` — DuckDB 1.5.5 has NO ClickHouse extension with ATTACH
  support. The community repo for 1.5.5 carries ``chsql_native`` (a
  client exposing ``clickhouse_scan``/``clickhouse_native`` table
  functions only — no attach type, no catalog, credentials via
  CLICKHOUSE_URL env vars), and the old ``clickhouse`` ATTACH extension
  is no longer published for this version (probed: the 1.5.5 artifact
  404s). Attaching would need scan-func semantics that cannot present a
  catalog alias (``<alias>.<schema>.<table>`` references would not
  resolve), so the type is refused loudly instead of silently
  misbehaving. Revisit if the extension returns to the community repo
  for a future DuckDB. ``motherduck`` — no scanner either: DuckDB
  resolves a bare ``ATTACH 'md:'`` by AUTO-INSTALLING the signed
  ``motherduck`` extension from MotherDuck's own repository (verified
  empirically on 1.5.5 — it even initiates a browser OAuth login when no
  token is set, which would hang a server pod), and with a token it
  opens an outbound authenticated session outside the DuckDB-repo
  supply chain the Dockerfile bakes from. That is a different trust
  boundary than every type here; MotherDuck remains reachable via the
  ``ducklake`` type (``catalog: "md:<db>"`` + ``password_env`` →
  ``motherduck_token``), which carries the token through the same
  password_env/scrubbing discipline as everything else.

TLS and other driver options flow through the optional per-entry ``params``
object — plain ``{key: value}`` pairs forwarded VERBATIM into the DSN after
the built-in defaults, so ``params`` can override ``connect_timeout`` or add
TLS options. Keys are lowercased and must match ``[a-z][a-z0-9_]*``; values
are JSON scalars (non-strings coerced with ``str()``; nested objects/arrays
rejected) matching ``[A-Za-z0-9_./:@=+ -]`` and at most 256 characters —
single quotes, backslashes, semicolons and braces never reach the DSN, so a
value cannot break out of it under any of the quoting dialects involved
(libpq, libmariadb, ODBC connstr). Keys that would carry secrets or
duplicate the entry's own fields (``password``, ``user``, ``host``,
``port``, ``database``, ``server``, ...) are rejected loudly. For
``mysql``/``mariadb`` values must additionally be whitespace-free: the
mysql_scanner DSN is serialized BARE (its parser splits on whitespace and
does not strip quotes), so whitespace-bearing values are rejected at parse
time, and a whitespace-bearing resolved password fails loudly at attach
time (without being echoed). Values are
forwarded verbatim, so the exact key spelling is the DRIVER's
responsibility (libpq wants ``sslmode``, libmariadb wants ``ssl_mode``) —
a wrong key is an operator-fixable typo, not a code bug:

    {"name": "ops", "type": "postgres", "host": "pg.internal",
     "database": "opsdb", "user": "ro_user", "password_env": "OPS_PG_PASSWORD",
     "params": {"sslmode": "verify-full", "sslrootcert": "/certs/ca.pem"}}

Ordering matters and is verified empirically (duckdb 1.5.x): extension
``LOAD`` and ``ATTACH`` both use DuckDB's filesystem layer internally, but
query-time data fetch does not (libpq talks TCP/unix-socket directly;
sqlite reads its file through the scanner's bundled sqlite3). Hence the
required sequence on a fresh connection:

    LOAD postgres_scanner                                  # fs still open
    ATTACH '<dsn>' AS <alias> (TYPE postgres, READ_ONLY)   # fs still open
    SET disabled_filesystems='LocalFileSystem'             # THEN lock down
    -- attached tables stay queryable; lake views register as usual

    (bigquery additionally emits a scoped CREATE OR REPLACE SECRET for a
    password_env-resolved access token BETWEEN the LOAD and the ATTACH —
    see build_pre_attach_sql; the secret manager needs no fs access.)

Configuration (environment variables):

    SQLHANDLER_ATTACH        inline JSON, same schema as the file
    SQLHANDLER_ATTACH_FILE   path to a JSON file of attach specs

    JSON: {"databases": [ {"name": "ops", "type": "postgres",
            "host": "pg.internal", "port": 5432, "database": "opsdb",
            "user": "ro_user", "password_env": "OPS_PG_PASSWORD",
            "params": {"sslmode": "require"}} ]}
    (a bare array is also accepted; ``user``/``user_env`` are optional for
    the server types — postgres defaults to "postgres", mysql/mariadb to
    "root", sqlserver REQUIRES one, there is no safe default — and
    ``password_env`` is required but may be "" for trust-auth over a unix
    socket; for sqlite and bigquery it may be omitted entirely (sqlite has
    no auth; bigquery defaults to ADC). A literal ``password`` key is
    always an error, inside ``params`` too.)
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .provider import LakehouseError

logger = logging.getLogger("sqlhandler.engine")

ATTACH_TYPES = (
    "postgres",
    "mariadb",
    "mysql",
    "sqlite",
    "sqlserver",
    "ducklake",
    "mongodb",
    "bigquery",
)
DEFAULT_PORTS = {
    "postgres": 5432,
    "mysql": 3306,
    "mariadb": 3306,
    "sqlserver": 1433,
    "mongodb": 27017,
}
# Deliberately NOT in ATTACH_TYPES (see the module docstring's honest-skip
# notes for the full reasons): `clickhouse` (no 1.5.5 extension with ATTACH
# support — the community repo for 1.5.5 has chsql_native only, scan-func
# semantics without a catalog) and `motherduck` (the md: attach auto-installs
# a signed extension from MotherDuck's own repo and can trigger an OAuth
# browser login — outside this module's bake-and-LOAD trust boundary; use the
# ducklake type with an md: catalog for MotherDuck instead).

# Config type -> DuckDB extension to LOAD (see ensure_extensions). "mariadb"
# is an alias served by mysql_scanner (identical wire protocol); "sqlserver"
# maps to the community extension `mssql` (native TDS) — there is no extension
# named `sqlserver` in the DuckDB community repository.
_EXTENSIONS: dict[str, str] = {
    "postgres": "postgres_scanner",
    "mysql": "mysql_scanner",
    "mariadb": "mysql_scanner",
    "sqlite": "sqlite_scanner",
    "sqlserver": "mssql",
    # Community extensions with full ATTACH support on 1.5.5 — NOT in the
    # image's default bake list (see the Dockerfile's opt-in comment): LOAD
    # fails with DuckDB's "Extension … not found" until the operator extends
    # the bake, which is the honest "extension not baked into this image"
    # error rather than a runtime download.
    "mongodb": "mongo",
    "bigquery": "bigquery",
}

# --- ducklake (added surgically; sibling types above are other agents' work) ---
# DuckLake: DuckDB's lakehouse format — a SQL catalog database (sqlite/postgres/
# motherduck/…) storing schemas/tables/snapshots/stats that point at Parquet
# data files on disk or object storage. The catalog connect string rides the
# ducklake: URL scheme itself, so DuckDB resolves the attach handler WITHOUT a
# TYPE keyword: `ATTACH 'ducklake:<catalog>' AS <alias> (…)` — verified
# empirically on duckdb 1.5.5, where an explicit `TYPE ducklake` in the options
# clause DISABLES probing/creation ("Existing DuckLake at metadata catalog …
# does not exist - and creating a new DuckLake is explicitly disabled"). The
# ducklake extension is a CORE (not community) extension on 1.5.x, vendored
# alongside the scanners in duckdb-ext/.
_EXTENSIONS["ducklake"] = "ducklake"

# The catalog connect-string (the `catalog` spec field, WITHOUT the ducklake:
# prefix) is operator-authored and forwarded verbatim into the ATTACH literal.
# Accepted forms (DuckDB 1.5.5 ducklake extension):
#   sqlite:<path>                 local sqlite catalog file (tests/dev/single node)
#   postgres:<libpq key=val ...>  libpq connect string (may embed credentials)
#   md:<database>[?opts]          MotherDuck catalog (token via password_env)
# (duckdb/mysql catalog backends also exist upstream; such a string passes
# through identically.)
# Guardrail: the string must never contain control characters (incl. NUL and
# newlines) — it is interpolated into the ATTACH '…' SQL literal, and control
# characters have no legitimate place in a connect string.
_CATALOG_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# DuckLake's optional DATA_PATH (the directory parquet data files are written
# to / read from). Only meaningful when the attach CREATES a new DuckLake — an
# existing catalog carries its own data path; DuckDB 1.5.5 accepts and ignores
# it there (verified empirically).

# Config type -> the TYPE keyword of the ATTACH statement. It may differ from
# the config spelling when DuckDB registers the attach type under the
# extension's canonical name (mariadb attaches as `mysql`, sqlserver as
# `mssql`); `spec.type` always keeps the operator-facing spelling.
_ATTACH_SQL_TYPE: dict[str, str] = {
    "postgres": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "sqlserver": "mssql",
    # The mongo extension's own docs use (TYPE MONGO); DuckDB's ATTACH type
    # parser is case-insensitive (mssql docs show lowercase), so we emit the
    # extension's canonical lowercase name.
    "mongodb": "mongo",
    "bigquery": "bigquery",
    # ducklake does NOT use the TYPE keyword (see the block above) —
    # build_attach_sql special-cases it BEFORE consulting this map, so this
    # entry documents the handler identity; the value is never emitted.
    "ducklake": "ducklake",
}

_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# DuckDB's built-in catalog names — attaching over them would shadow engine
# internals; reject rather than surprise.
_RESERVED_ALIASES = {"memory", "system", "temp"}
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Connection params (per-entry "params" object): keys are lowercased and then
# must look like lowercase identifiers; values are JSON scalars restricted to
# an injection-safe charset — no single quotes, backslashes, semicolons,
# braces or control characters survive, across libpq, libmariadb and ODBC
# connstring quoting dialects.
_PARAM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PARAM_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@=+ -]+$")
_PARAM_VALUE_MAX_LEN = 256
# Any whitespace character — rejected in mysql/mariadb DSN values (see
# _reject_dsn_whitespace: the mysql_scanner DSN is serialized BARE and its
# parser splits on whitespace, so whitespace would corrupt the key=value
# parse).
_WHITESPACE_RE = re.compile(r"\s")
# Params keys that are rejected loudly: secrets (they ride password_env, never
# the DSN params) and keys that would duplicate the entry's own structured
# fields (those are configured top-level, once, validated).
_REJECTED_PARAM_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "sslpassword",
        "user",
        "username",
        "uid",
        "host",
        "hostaddr",
        "port",
        "dbname",
        "database",
        "server",
        "trusted_connection",
    }
)


class ExternalAttachError(LakehouseError):
    """An external database could not be attached (scrubbed message)."""


# Key=value credentials that may ride INSIDE a ducklake catalog connect string
# (the postgres libpq form carries password=…; MotherDuck catalogs carry
# motherduck_token=…). display_uri scrubbing blanks every value of these keys,
# so an operator-authored catalog string with an inline credential never
# reaches tool output.
_CATALOG_SECRET_KEY_RE = re.compile(
    r"(password|passwd|pwd|motherduck_token|token|access_token|secret_access_key)"
    r"=(?P<val>[^\s&;]*)",
    re.IGNORECASE,
)


def _scrub_catalog_secrets(catalog: str) -> str:
    """Blank the values of credential keys inside a catalog connect string."""
    return _CATALOG_SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}=***", catalog)


@dataclass(frozen=True)
class AttachSpec:
    """One read-only external database to ATTACH on demand.

    ``password_env`` names the environment variable holding the password;
    the empty string means trust-auth (e.g. a unix-socket connection) and
    sends no password at all. The secret value itself never lives in this
    object — only in the process environment.

    ``params`` holds optional operator-supplied connection parameters
    (TLS options, timeouts, ...) as ``(key, value)`` pairs, forwarded
    verbatim into the DSN after the built-in defaults — validated at parse
    time (see :func:`_validate_params`), stored immutable and hashable.
    """

    name: str  # DuckDB catalog alias used in SQL: <name>.<schema>.<table>
    type: str  # one of ATTACH_TYPES
    host: str  # hostname, IP, or a unix-socket directory (postgres); "" for sqlite
    port: int  # 0 for sqlite (a file attach has no port)
    database: str  # database name; the sqlite FILE PATH for type "sqlite"
    user: str  # "" for sqlite (no server user)
    password_env: str
    read_only: bool = True  # v1: always True; the field documents intent
    params: tuple[tuple[str, str], ...] = ()  # verbatim DSN extras (TLS etc.)
    # ducklake only: the catalog connect string WITHOUT the ducklake: prefix
    # (e.g. "sqlite:/path/catalog.db", "postgres:dbname=… host=…"), and the
    # optional DATA_PATH directory. Both unused by every other type.
    catalog: str = ""
    data_path: str = ""
    # bigquery only: the optional billing/quota project (the extension's
    # `billing_project` ATTACH option — needed when the DATA project differs
    # from the project that supplies quota, e.g. public datasets). Unused by
    # every other type.
    billing_project: str = ""

    def password(self, environ: Mapping[str, str] | None = None) -> str | None:
        env = os.environ if environ is None else environ
        if not self.password_env:
            return None
        return env.get(self.password_env, "")

    @property
    def display_uri(self) -> str:
        """Credential-free identifier for tool output."""
        if self.type == "sqlite":
            return f"sqlite://{self.database}"  # a file path — no host/port/user
        if self.type == "bigquery":
            # project[.dataset] scope — no host/port/user, no secret
            return f"bigquery://{self.database}"
        if self.type == "ducklake":
            # The catalog connect string can embed credentials (postgres
            # key=value form, motherduck_token=…) — scrub EVERY "password="
            # / "motherduck_token=" / "token=" value out of it, plus the
            # password_env-resolved secret, before it reaches tool output.
            catalog = scrub_secrets(_scrub_catalog_secrets(self.catalog), [self])
            return f"ducklake:{catalog}"
        return f"{self.type}://{self.host}:{self.port}/{self.database}"


def _reject_secret_keys(entry: dict, where: str) -> None:
    for key in ("password", "passwd", "secret", "pwd"):
        if key in entry:
            raise ValueError(
                f"{where}: literal {key!r} keys are not allowed in attach config — "
                f"pass the env-var NAME via 'password_env' instead; secrets flow "
                "environment -> pod, never through config or the model."
            )


def _reject_dsn_whitespace(value: str, where: str, field: str) -> None:
    """Reject whitespace in a mysql/mariadb/mongodb DSN value (bare serialization).

    The mysql_scanner and mongo DSN parsers split on whitespace and do NOT
    strip quotes, so values for those types ride bare in the ATTACH string —
    a whitespace-bearing value would corrupt the key=value parse. Entry
    fields (host/database/user, including user_env-resolved values) and
    params values are checked here, loudly, at startup with the field named.
    The password is only resolved at attach time; it is checked in
    :func:`build_attach_sql` (and never echoed).
    """
    if _WHITESPACE_RE.search(value):
        raise ValueError(
            f"{where}: attach field/value {field} must not contain whitespace — "
            "the bare-serialized DSN (mysql_scanner / mongo) takes bare values and "
            "splits on whitespace"
        )


def _validate_params(raw: object, where: str) -> tuple[tuple[str, str], ...]:
    """Validate a ``params`` object into immutable ``(key, value)`` pairs.

    Keys: lowercased, ``[a-z][a-z0-9_]*``, never a rejected key (secrets or
    duplicates of the entry's own fields). Values: JSON scalars only (nested
    objects/arrays rejected, non-strings coerced with ``str()``), at most
    :data:`_PARAM_VALUE_MAX_LEN` characters and matching
    :data:`_PARAM_VALUE_RE` — so a value can never smuggle quotes,
    backslashes, semicolons, braces or control characters into the DSN.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise TypeError(f"{where}: 'params' must be a JSON object of connection key/value pairs")
    # Secrets never ride params either — same loud rejection as the entry level.
    _reject_secret_keys(raw, f"{where} params")
    out: list[tuple[str, str]] = []
    for raw_key, raw_val in raw.items():
        key = str(raw_key).strip().lower()
        if not _PARAM_KEY_RE.match(key):
            raise ValueError(
                f"{where}: params key {str(raw_key)!r} is not a valid connection parameter "
                "name (lowercase letters/digits/underscore, starting with a letter)"
            )
        if key in _REJECTED_PARAM_KEYS:
            raise ValueError(
                f"{where}: params key {key!r} is not allowed — params must not carry secrets "
                "or duplicate the entry's own fields; set credentials via 'password_env' and "
                "the endpoint via the top-level 'host'/'port'/'database'/'user' keys"
            )
        if isinstance(raw_val, (dict, list)):
            raise TypeError(
                f"{where}: params value for {key!r} must be a JSON scalar "
                "(string/number/bool), not a nested object/array"
            )
        value = raw_val if isinstance(raw_val, str) else str(raw_val)
        if len(value) > _PARAM_VALUE_MAX_LEN:
            raise ValueError(
                f"{where}: params value for {key!r} exceeds {_PARAM_VALUE_MAX_LEN} characters"
            )
        if not _PARAM_VALUE_RE.match(value):
            raise ValueError(
                f"{where}: params value for {key!r} contains characters outside the allowed "
                "set (letters, digits and _./:@=+ space) — quotes, backslashes, semicolons, "
                "braces and control characters are rejected so a value can never break out "
                "of the DSN"
            )
        out.append((key, value))
    return tuple(out)


def parse_attach_config(environ: dict[str, str] | None = None) -> list[AttachSpec]:
    """Parse SQLHANDLER_ATTACH / SQLHANDLER_ATTACH_FILE into validated specs.

    Raises ValueError on any malformed entry — attach config is
    security-relevant and operator-authored, so it fails loudly at startup
    rather than degrading silently (unlike the optional semantic catalog).
    """
    env = os.environ if environ is None else environ
    raw_entries: list[dict] = []

    file_path = env.get("SQLHANDLER_ATTACH_FILE", "").strip()
    if file_path:
        with open(file_path, encoding="utf-8") as fh:
            raw_entries.extend(
                _entries_from_json(fh.read(), f"SQLHANDLER_ATTACH_FILE ({file_path})")
            )
    inline = env.get("SQLHANDLER_ATTACH", "").strip()
    if inline:
        raw_entries.extend(_entries_from_json(inline, "SQLHANDLER_ATTACH"))

    specs: list[AttachSpec] = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw_entries):
        where = f"attach config entry [{idx}]"
        if not isinstance(entry, dict):
            raise TypeError(f"{where} must be a JSON object")
        _reject_secret_keys(entry, where)
        name = str(entry.get("name", "")).strip()
        if not _ALIAS_RE.match(name):
            raise ValueError(
                f"{where}: name {name!r} is not a valid catalog alias "
                "(letters/digits/underscore, must not start with a digit)"
            )
        if name.lower() in _RESERVED_ALIASES:
            raise ValueError(f"{where}: name {name!r} is reserved by DuckDB")
        if name.lower() in seen:
            raise ValueError(f"{where}: duplicate attach name {name!r}")
        seen.add(name.lower())

        db_type = str(entry.get("type", "")).strip().lower()
        if db_type not in ATTACH_TYPES:
            raise ValueError(
                f"{where}: type {db_type!r} not supported (use one of {', '.join(ATTACH_TYPES)})"
            )
        is_sqlite = db_type == "sqlite"
        is_ducklake = db_type == "ducklake"
        is_mongo = db_type == "mongodb"
        is_bigquery = db_type == "bigquery"
        host = str(entry.get("host", "")).strip()
        database = str(entry.get("database", "")).strip()
        if not is_bigquery and entry.get("billing_project"):
            # billing_project is a bigquery-only field (the extension's
            # billing/quota-project ATTACH option) — reject it everywhere
            # else rather than silently ignoring it.
            raise ValueError(
                f"{where}: 'billing_project' is only valid for type 'bigquery'"
            )
        billing_project = ""
        if is_bigquery:
            # bigquery attaches a PROJECT or PROJECT.DATASET scope — there is
            # no server endpoint. "database" IS that scope (the extension's
            # DSN carries it as project=… / dataset=…); host is meaningless
            # (auth is ADC / a scoped DuckDB secret).
            if host:
                raise ValueError(
                    f"{where}: 'host' is not valid for type 'bigquery' — the scope is "
                    "'database' (a GCP project or project.dataset); there is no server endpoint"
                )
            if not database:
                raise ValueError(
                    f"{where}: database is required (for bigquery: the GCP project ID "
                    "or 'project.dataset')"
                )
            parts = database.split(".")
            if not 1 <= len(parts) <= 2 or not all(
                re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts
            ):
                raise ValueError(
                    f"{where}: bigquery database {database!r} must be a GCP project ID "
                    "or 'project.dataset' (letters/digits/dash/underscore)"
                )
            # Optional billing/quota project (the extension's billing_project
            # ATTACH option — for public/cross-project data where the project
            # holding the data differs from the one billed for queries).
            billing_project = str(entry.get("billing_project", "")).strip()
            if billing_project and not re.fullmatch(r"[A-Za-z0-9_-]+", billing_project):
                raise ValueError(
                    f"{where}: bigquery billing_project {billing_project!r} must be a GCP "
                    "project ID (letters/digits/dash/underscore)"
                )
        elif is_mongo:
            # mongodb keeps the server-endpoint shape (host[:port] + optional
            # auth + database scoping): host is required, database names the
            # MongoDB database to scope the attach to (the extension's
            # dbname= key; omitting it would expose EVERY database on the
            # server as a schema of the attached catalog — rejected as too
            # wide a default for an alias the model references).
            if not host:
                raise ValueError(f"{where}: host is required")
            if not database:
                raise ValueError(
                    f"{where}: database is required (for mongodb: the MongoDB database "
                    "the attach is scoped to — the extension's dbname key)"
                )
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in database):
                raise ValueError(f"{where}: mongodb database contains NUL/control characters")
        elif is_ducklake:
            # ducklake attaches a CATALOG, not a server endpoint: "catalog" is
            # the connect string WITHOUT the ducklake: prefix (operator-authored,
            # exactly like the sqlite file path / the server host — same trust
            # model). No host/port/database/user fields.
            # The control-character check runs BEFORE the strip: a stripped
            # control character is a silently-different connect string, not a
            # validated one.
            catalog = str(entry.get("catalog", ""))
            if _CATALOG_CONTROL_RE.search(catalog):
                raise ValueError(
                    f"{where}: ducklake catalog connect string contains NUL/control characters"
                )
            catalog = catalog.strip()
            if not catalog:
                raise ValueError(
                    f"{where}: catalog is required for type 'ducklake' — the connect "
                    "string WITHOUT the ducklake: prefix (e.g. 'sqlite:/path/catalog.db' "
                    "or 'postgres:dbname=… host=…')"
                )
            if host:
                raise ValueError(
                    f"{where}: 'host' is not valid for type 'ducklake' — the endpoint is "
                    "the 'catalog' connect string"
                )
            if database:
                raise ValueError(
                    f"{where}: 'database' is not valid for type 'ducklake' — the endpoint is "
                    "the 'catalog' connect string"
                )
        elif is_sqlite:
            # sqlite attaches a FILE: "database" is the file path (operator-
            # authored, minimal validation, like host for the server types);
            # there is no server endpoint to configure.
            if host:
                raise ValueError(
                    f"{where}: 'host' is not valid for type 'sqlite' — it attaches a local "
                    "file; 'database' is the sqlite FILE PATH"
                )
            if not database:
                raise ValueError(
                    f"{where}: database is required (for sqlite: the sqlite FILE PATH)"
                )
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in database):  # NUL & control chars
                raise ValueError(f"{where}: sqlite database path contains NUL/control characters")
        else:
            if not host:
                raise ValueError(f"{where}: host is required")
            if not database:
                raise ValueError(f"{where}: database is required")

        port_raw = str(entry.get("port", "")).strip()
        if is_bigquery:
            # No port: the scope is project[.dataset], auth is ADC/secret.
            if port_raw:
                raise ValueError(
                    f"{where}: 'port' is not valid for type 'bigquery' — the scope is "
                    "'database' (project or project.dataset), there is no server endpoint"
                )
            port = 0  # unused; bigquery has no endpoint port
        elif is_sqlite or is_ducklake:
            if port_raw:
                raise ValueError(
                    f"{where}: 'port' is not valid for type {db_type!r} — "
                    + (
                        "a ducklake catalog is addressed by its connect string, there is no port"
                        if is_ducklake
                        else "a sqlite attach is a local file, there is no port"
                    )
                )
            port = 0  # unused; sqlite/ducklake have no port and no DEFAULT_PORTS entry
        else:
            port = int(port_raw) if port_raw else DEFAULT_PORTS[db_type]
            if not 0 < port < 65536:
                raise ValueError(f"{where}: port {port} out of range")

        user = str(entry.get("user", "")).strip()
        user_env = str(entry.get("user_env", "")).strip()
        if is_bigquery:
            # Auth is ADC / a scoped DuckDB secret — a username field has no
            # meaning and would only mislead (the extension never reads one).
            if user or user_env:
                raise ValueError(
                    f"{where}: 'user'/'user_env' are not valid for type 'bigquery' — "
                    "auth rides GOOGLE_APPLICATION_CREDENTIALS / the gcloud ADC or a "
                    "temporary access token via 'password_env'"
                )
        elif is_sqlite or is_ducklake:
            if user or user_env:
                raise ValueError(
                    f"{where}: 'user'/'user_env' are not valid for type {db_type!r} — "
                    + (
                        "a ducklake catalog carries its credentials inside the connect "
                        "string (postgres form) or via 'password_env' (MotherDuck)"
                        if is_ducklake
                        else "a sqlite file has no server user"
                    )
                )
        else:
            if user and user_env:
                raise ValueError(f"{where}: use either 'user' or 'user_env', not both")
            if user_env:
                user = env.get(user_env, "")
                if not user:
                    raise ValueError(
                        f"{where}: user_env {user_env!r} is set but the environment variable is missing or empty"
                    )
            if not user:
                if db_type == "postgres":
                    user = "postgres"
                elif db_type in ("mysql", "mariadb"):
                    user = "root"
                elif db_type == "mongodb":
                    user = ""  # no safe default — unauthenticated local/dev or explicit user
                else:  # sqlserver
                    raise ValueError(
                        f"{where}: type 'sqlserver' requires an explicit 'user' or 'user_env' "
                        "— there is no safe default (refusing to fall back to 'sa')"
                    )

        password_env = str(entry.get("password_env", "")).strip()
        if "password_env" not in entry and not (is_sqlite or is_ducklake or is_bigquery):
            raise ValueError(
                f"{where}: 'password_env' is required (set it to \"\" explicitly for trust-auth over a unix socket)"
            )
        if password_env and password_env not in env:
            raise ValueError(
                f"{where}: password_env {password_env!r} is not present in the "
                "environment — inject it via the deployment secret, never in config"
            )

        data_path = ""
        if is_ducklake:
            # Optional DATA_PATH: only consulted when the attach CREATES a new
            # DuckLake; an existing catalog ignores it (verified empirically on
            # 1.5.5). Same control-character guardrail as the catalog string —
            # checked BEFORE the strip (a stripped control character is a
            # silently-different path, not a validated one).
            data_path = str(entry.get("data_path", ""))
            if _CATALOG_CONTROL_RE.search(data_path):
                raise ValueError(
                    f"{where}: ducklake data_path contains NUL/control characters"
                )
            data_path = data_path.strip()

        params = _validate_params(entry.get("params", {}), where)
        if (is_sqlite or is_ducklake) and params:
            raise ValueError(
                f"{where}: 'params' are not valid for type {db_type!r} — "
                + (
                    "the ATTACH is just the catalog connect string (+ optional data_path), "
                    "there is nothing to parameterize"
                    if is_ducklake
                    else "the DSN is just the file path, there is nothing to parameterize"
                )
            )
        if is_bigquery and params:
            # The extension's knobs (bq_* settings) are DuckDB SETtings, not
            # ATTACH DSN keys — a params object here would silently do
            # nothing; reject it rather than pretend.
            raise ValueError(
                f"{where}: 'params' are not valid for type 'bigquery' — the bq_* options are "
                "DuckDB settings, not ATTACH DSN keys; auth rides ADC / the scoped access-token secret"
            )
        if db_type in ("mysql", "mariadb", "mongodb"):
            # The mysql/mariadb/mongodb DSNs are serialized bare (the
            # mysql_scanner and mongo extensions split on whitespace and do
            # not strip quotes — mongo builds a mongodb:// URI from the
            # key=value pairs), so every value that reaches them must be
            # whitespace-free — including the user_env-resolved user. Fail
            # at startup with the field named.
            for field, value in (("host", host), ("database", database), ("user", user)):
                _reject_dsn_whitespace(value, where, field)
            for key, value in params:
                _reject_dsn_whitespace(value, where, f"params.{key}")

        specs.append(
            AttachSpec(
                name=name,
                type=db_type,
                host=host,
                port=port,
                database=database,
                user=user,
                password_env=password_env,
                params=params,
                catalog=catalog if is_ducklake else "",
                data_path=data_path,
                billing_project=billing_project,
            )
        )
    return specs


def _entries_from_json(raw: str, source: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("databases")
    if not isinstance(data, list) or not data:
        raise ValueError(f"{source} must be a non-empty JSON array (or {{'databases': [...]}})")
    return data


def _sql_quote(value: str) -> str:
    """Quote one DSN value (libpq/ODBC style: single quotes, doubled inside)."""
    return "'" + value.replace("'", "''") + "'"


def _merge_params(
    pairs: list[tuple[str, str]],
    params: tuple[tuple[str, str], ...],
    *,
    case_insensitive: bool = False,
) -> list[tuple[str, str]]:
    """Overlay operator ``params`` onto built DSN ``(key, value)`` pairs.

    An existing key is replaced IN PLACE (so ``connect_timeout`` can be
    overridden without duplicating it); a new key is appended after the
    defaults. No duplicate key ever reaches the DSN. ``case_insensitive``
    is used for connection-string dialects whose keys are case-insensitive
    (ODBC/ADO.NET for sqlserver), so e.g. ``encrypt`` overrides the built-in
    ``Encrypt=yes``.
    """

    def norm(key: str) -> str:
        return key.lower() if case_insensitive else key

    out = list(pairs)
    index = {norm(k): i for i, (k, _) in enumerate(out)}
    for key, value in params:
        nkey = norm(key)
        if nkey in index:
            out[index[nkey]] = (key, value)
        else:
            index[nkey] = len(out)
            out.append((key, value))
    return out


def build_attach_sql(spec: AttachSpec, password: str | None) -> str:
    """The exact ATTACH statement for one spec (READ_ONLY always enforced).

    Each type builds its default key->value DSN pairs, overlays the spec's
    ``params`` (operator values win, new keys append — no duplicate key can
    reach the DSN), then serializes. The whole DSN sits inside the ATTACH
    ``'…'`` SQL literal, so it is escaped once more at this level
    (``'`` -> ``''``) regardless of type.
    """
    try:
        sql_type = _ATTACH_SQL_TYPE[spec.type]
    except KeyError:
        raise ValueError(f"unknown attach type {spec.type!r}") from None

    if spec.type == "sqlite":
        # The DSN is just the file path; quoting happens at the ATTACH-literal
        # level below (single quotes doubled). No key=value pairs at all.
        dsn = spec.database
    elif spec.type == "ducklake":
        # The catalog connect string rides the ducklake: URL scheme, which is
        # what makes DuckDB dispatch to the ducklake extension — an explicit
        # `TYPE ducklake` in the options clause DISABLES probing/creation on
        # 1.5.5 (verified empirically), so the options clause carries NO TYPE.
        # The catalog string is operator-authored and forwarded verbatim
        # (modulo the '' escaping below); the resolved password (MotherDuck
        # catalogs) rides the motherduck_token option inside the URL — never
        # echoed, scrubbed from errors by apply_external like every type.
        dsn = f"ducklake:{spec.catalog}"
        if password:
            dsn += f"?motherduck_token={password}" if "?" not in spec.catalog else f"&motherduck_token={password}"
        # The catalog string is operator-authored and cannot carry a single
        # quote (the ATTACH-literal '' doubling below would corrupt it into a
        # two-token statement), so doubling it here is a loud DuckDB syntax
        # error — not an injection.
        dsn = dsn.replace("'", "''")
        options = [f"DATA_PATH '{spec.data_path.replace(chr(39), chr(39) * 2)}'"] if spec.data_path else []
        if spec.read_only:
            options.append("READ_ONLY")
        opts = f" ({', '.join(options)})" if options else ""
        return f"ATTACH '{dsn}' AS \"{spec.name}\"{opts}"
    elif spec.type in ("mysql", "mariadb"):
        # mysql_scanner's DSN keys follow MySQL conventions (database /
        # passwd); TLS keys are ssl_mode/ssl_ca/... — forwarded verbatim via
        # params. Values are serialized BARE: the scanner's DSN parser splits
        # on whitespace and does NOT strip quotes, so a quoted value would
        # carry literal quote characters into the connection (empirically,
        # host='127.0.0.1' fails with "Unknown server host ''127.0.0.1''").
        # Safety: entry fields and params values are validated whitespace-free
        # at parse time (_reject_dsn_whitespace); the password — only resolved
        # here — is checked below and never echoed. Single quotes inside a
        # value are literal characters and harmless: the ATTACH-level ''
        # doubling below collapses before the extension sees the string.
        pairs: list[tuple[str, str]] = [
            ("host", spec.host),
            ("port", str(spec.port)),
            ("database", spec.database),
            ("user", spec.user),
        ]
        if password:
            if _WHITESPACE_RE.search(password):
                raise ValueError(
                    f"attach {spec.name} ({spec.type}): the resolved password contains whitespace, "
                    "which the mysql_scanner DSN cannot carry unquoted — use a read-only account "
                    "whose password is whitespace-free, or front the server with a proxy"
                )
            pairs.append(("passwd", password))
        pairs = _merge_params(pairs, spec.params)
        dsn = " ".join(f"{k}={v}" for k, v in pairs)
    elif spec.type == "sqlserver":
        # ODBC/ADO.NET-style connection string (the mssql extension parses
        # this form natively; keys are case-insensitive there). Values are
        # raw — params validation already bans ; ' \ { } — and TLS defaults
        # are lenient (self-signed certificates accepted); params overlay
        # can tighten them.
        pairs = [
            ("Server", f"{spec.host},{spec.port}"),
            ("Database", spec.database),
            ("Uid", spec.user),
            ("Pwd", password or ""),
            ("Encrypt", "yes"),
            ("TrustServerCertificate", "yes"),
        ]
        pairs = _merge_params(pairs, spec.params, case_insensitive=True)
        dsn = ";".join(f"{k}={v}" for k, v in pairs)
    elif spec.type == "mongodb":
        # The mongo extension's key-value DSN (its documented connection
        # parameters): host/port/user/password/dbname + srv/tls via params.
        # The database name rides `dbname` (the extension's key — `database`
        # is a rejected params key, so no collision). Values are serialized
        # BARE (the extension builds a mongodb:// URI from them; a
        # whitespace-bearing value would corrupt that URI the same way it
        # corrupts mysql_scanner's) — entry fields are validated whitespace-
        # free at parse time, and the password is checked below, never
        # echoed. An SRV/Atlas attach passes the host through unchanged and
        # sets srv=true via params.
        pairs: list[tuple[str, str]] = [
            ("host", spec.host),
            ("port", str(spec.port)),
            ("dbname", spec.database),
        ]
        if spec.user:
            pairs.append(("user", spec.user))
        if password:
            if _WHITESPACE_RE.search(password):
                raise ValueError(
                    f"attach {spec.name} ({spec.type}): the resolved password contains whitespace, "
                    "which the mongo DSN cannot carry unquoted — use a read-only account "
                    "whose password is whitespace-free, or front the server with a proxy"
                )
            pairs.append(("password", password))
        pairs = _merge_params(pairs, spec.params)
        dsn = " ".join(f"{k}={v}" for k, v in pairs)
    elif spec.type == "bigquery":
        # The bigquery extension's ATTACH DSN: project= / dataset= /
        # billing_project= — its DOCUMENTED attach options, and all of them
        # (verified against the extension's docs: there is NO access_token
        # ATTACH key). Auth is NOT DSN-based: the extension resolves Google
        # Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS /
        # workload identity / gcloud ADC) or a DuckDB Secret scoped to
        # bq://<project>. A password_env-resolved access token therefore
        # rides a CREATE OR REPLACE SECRET statement emitted BEFORE the
        # ATTACH (see build_pre_attach_sql) — NEVER this DSN, which keeps it
        # out of every ATTACH echo too.
        scope = spec.database.split(".", 1)
        pairs = [("project", scope[0])]
        if len(scope) == 2:
            pairs.append(("dataset", scope[1]))
        if spec.billing_project:
            pairs.append(("billing_project", spec.billing_project))
        pairs = _merge_params(pairs, spec.params)
        dsn = " ".join(f"{k}={v}" for k, v in pairs)
    else:  # postgres
        pairs = [
            ("host", _sql_quote(spec.host)),
            ("port", str(spec.port)),
            ("dbname", _sql_quote(spec.database)),
            ("user", _sql_quote(spec.user)),
        ]
        if password:
            pairs.append(("password", _sql_quote(password)))
        pairs.append(("connect_timeout", "10"))
        pairs = _merge_params(pairs, tuple((k, _sql_quote(v)) for k, v in spec.params))
        dsn = " ".join(f"{k}={v}" for k, v in pairs)

    # The DSN itself sits inside the ATTACH '…' SQL literal: values quoted by
    # _sql_quote (e.g. a socket path host='/tmp/…') must be escaped once more
    # at this level (' -> '') or the inner quote terminates the literal.
    dsn = dsn.replace("'", "''")
    ro = ", READ_ONLY" if spec.read_only else ""
    return f"ATTACH '{dsn}' AS \"{spec.name}\" (TYPE {sql_type}{ro})"


def build_pre_attach_sql(spec: AttachSpec, password: str | None) -> str | None:
    """The pre-ATTACH statement for one spec, or ``None`` when it needs none.

    Only ``bigquery`` has one today: the extension's ATTACH options are
    ``project``/``dataset``/``billing_project`` ONLY (there is no
    ``access_token`` ATTACH key), so a ``password_env``-resolved temporary
    OAuth2 token rides a DuckDB Secret scoped to the attached project —
    ``CREATE OR REPLACE SECRET … (TYPE bigquery, SCOPE 'bq://<project>',
    ACCESS_TOKEN '…')``, the extension's documented token form. The
    statement is emitted right before the ATTACH (after the extension
    LOAD, before the fs lockdown — the secret manager needs no fs access),
    ``OR REPLACE`` so an expired token can be re-applied on the same
    connection, and scoped so it can never leak into other attached
    projects. The token never rides the ATTACH DSN or ``display_uri``.
    Every other type (and bigquery without a resolved token — ADC is the
    default auth) returns ``None`` and emits nothing.
    """
    if spec.type == "bigquery" and password:
        project = spec.database.split(".", 1)[0]
        # One statement per attached project; '' doubled for the SQL literal.
        token = password.replace("'", "''")
        return (
            f"CREATE OR REPLACE SECRET \"{spec.name}\" (TYPE bigquery, "
            f"SCOPE 'bq://{project}', ACCESS_TOKEN '{token}')"
        )
    return None


def ensure_extensions(con, types: set[str]) -> None:
    """Explicitly LOAD the scanner extensions a query needs.

    Preinstalled in the image (auto-install stays disabled); LOAD must run
    BEFORE the filesystem lockdown because DuckDB loads the extension .so
    through its own filesystem layer. Types resolve through
    :data:`_EXTENSIONS` (e.g. mariadb LOADs mysql_scanner, sqlserver LOADs
    the community mssql extension, mongodb/bigquery LOAD their community
    extensions — which are NOT in the default bake list, so their LOAD
    fails with DuckDB's "Extension … not found" IO error until the image is
    rebuilt with them; see the Dockerfile's opt-in comment); sorted() keeps
    the statement order deterministic.

    A failed LOAD is re-raised with the build-time hint appended: the raw
    DuckDB error names the missing extension file but not the FIX (the
    image's bake list is build-time, auto-install stays disabled), so the
    operator-facing message points at the Dockerfile's opt-in comment.
    """
    for t in sorted(types):
        try:
            ext = _EXTENSIONS[t]
        except KeyError:
            raise ValueError(
                f"unknown attach type {t!r} (use one of {', '.join(sorted(_EXTENSIONS))})"
            ) from None
        try:
            con.execute(f"LOAD {ext}")
        except Exception as exc:
            raise LakehouseError(
                f"extension {ext!r} is not baked into this image (add "
                f"'INSTALL {ext} FROM community' to the Dockerfile's opt-in bake list); "
                f"DuckDB said: {exc}"
            ) from exc


def apply_external(con, specs: list[AttachSpec], environ: dict[str, str] | None = None) -> None:
    """LOAD extensions and ATTACH every configured database (READ_ONLY).

    Must run BEFORE the connection's filesystem lockdown (see module
    docstring). Per spec the sequence is: optional pre-ATTACH statements
    (bigquery's scoped access-token secret — :func:`build_pre_attach_sql`),
    then the ATTACH itself. Any failure raises ExternalAttachError with a
    message scrubbed of resolved secrets.
    """
    env = os.environ if environ is None else environ
    if not specs:
        return
    ext_dir = env.get("SQLHANDLER_DUCKDB_EXTENSION_DIR", "").strip()
    if ext_dir:
        con.execute(f"SET extension_directory='{ext_dir.replace(chr(39), chr(39) * 2)}'")
    try:
        ensure_extensions(con, {s.type for s in specs})
        for spec in specs:
            pre = build_pre_attach_sql(spec, spec.password(env))
            if pre:
                con.execute(pre)
            con.execute(build_attach_sql(spec, spec.password(env)))
    except Exception as exc:
        raise ExternalAttachError(_scrub(str(exc), specs, env)) from exc


def sql_references_attach(sql: str, specs: list[AttachSpec]) -> list[AttachSpec]:
    """Return the specs whose catalog alias appears in the SQL.

    Matches ``<alias>.`` (with an optional closing quote before the dot for
    ``"ops".public.t`` style identifiers) on word boundaries, so an alias
    ``ops`` does not fire on ``operations.x`` or a table named ``opslog``.
    Deliberately a heuristic: a false positive merely attaches an extra
    (read-only) catalog; a false negative surfaces as a clean
    "catalog/alias not found" binder error.
    """
    if not specs or not sql:
        return []
    matched = []
    for spec in specs:
        pattern = re.compile(rf"\b{re.escape(spec.name)}[\"']?\s*\.", re.IGNORECASE)
        if pattern.search(sql):
            matched.append(spec)
    return matched


def scrub_secrets(
    text: str, specs: list[AttachSpec], environ: Mapping[str, str] | None = None
) -> str:
    """Remove resolved passwords (and a failure's DSN echoes) from ``text``."""
    env = os.environ if environ is None else environ
    for spec in specs:
        pw = spec.password(env)
        if pw:
            text = text.replace(pw, "***")
    return text


def _scrub(text: str, specs: list[AttachSpec], env: Mapping[str, str]) -> str:
    return scrub_secrets(text, specs, env)


def validate_qualified_name(alias: str, table: str) -> str:
    """Validate an ``<alias>.<...>`` table reference for safe SQL embedding.

    Splits on dots; every part must be a bare identifier (letters/digits/
    underscore — no quotes, semicolons, whitespace, or comments survive).
    Returns the validated dotted string; raises LakehouseError otherwise.
    Two parts (``alias.table``) resolve through the server's search_path;
    three parts (``alias.schema.table``) are fully qualified.
    """
    if not table.lower().startswith(alias.lower() + "."):
        raise LakehouseError(f"Table {table!r} is not an attached-database reference (<{alias}.…>)")
    parts = table.split(".")
    if len(parts) not in (2, 3) or not all(_IDENT_RE.match(p) for p in parts):
        raise LakehouseError(
            f"Attached table reference {table!r} must be "
            f"<{alias}.<table>> or <{alias}.<schema>.<table>> with plain identifiers"
        )
    return table
