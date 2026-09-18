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
* ``sqlite``    -> ``sqlite_scanner``    (ATTACH ``TYPE sqlite``)
* ``sqlserver`` -> ``mssql``             (ATTACH ``TYPE mssql``)

``"postgresql"`` is deliberately NOT accepted (the config key is spelled
``postgres``). ``mssql`` is a *community* extension (native TDS 7.4 with
TLS — no unixODBC or Microsoft ODBC driver required) and, like the core
scanner extensions, must be PREINSTALLED in the image (auto-install stays
off — no runtime downloads).

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
    socket; for sqlite it may be omitted entirely. A literal ``password``
    key is always an error, inside ``params`` too.)
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

ATTACH_TYPES = ("postgres", "mariadb", "mysql", "sqlite", "sqlserver")
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "mariadb": 3306, "sqlserver": 1433}

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
}

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
    """Reject whitespace in a mysql/mariadb DSN value (bare serialization).

    mysql_scanner's DSN parser splits on whitespace and does NOT strip
    quotes, so mysql/mariadb values ride bare in the ATTACH string — a
    whitespace-bearing value would corrupt the key=value parse. Entry fields
    (host/database/user, including user_env-resolved values) and params
    values are checked here, loudly, at startup with the field named. The
    password is only resolved at attach time; it is checked in
    :func:`build_attach_sql` (and never echoed).
    """
    if _WHITESPACE_RE.search(value):
        raise ValueError(
            f"{where}: mysql/mariadb attach field/value {field} must not contain whitespace — "
            "the mysql_scanner DSN parser takes bare values and splits on whitespace"
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
            raw_entries.extend(_entries_from_json(fh.read(), f"SQLHANDLER_ATTACH_FILE ({file_path})"))
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
            raise ValueError(f"{where}: type {db_type!r} not supported (use one of {', '.join(ATTACH_TYPES)})")
        is_sqlite = db_type == "sqlite"
        host = str(entry.get("host", "")).strip()
        database = str(entry.get("database", "")).strip()
        if is_sqlite:
            # sqlite attaches a FILE: "database" is the file path (operator-
            # authored, minimal validation, like host for the server types);
            # there is no server endpoint to configure.
            if host:
                raise ValueError(
                    f"{where}: 'host' is not valid for type 'sqlite' — it attaches a local "
                    "file; 'database' is the sqlite FILE PATH"
                )
            if not database:
                raise ValueError(f"{where}: database is required (for sqlite: the sqlite FILE PATH)")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in database):  # NUL & control chars
                raise ValueError(f"{where}: sqlite database path contains NUL/control characters")
        else:
            if not host:
                raise ValueError(f"{where}: host is required")
            if not database:
                raise ValueError(f"{where}: database is required")

        port_raw = str(entry.get("port", "")).strip()
        if is_sqlite:
            if port_raw:
                raise ValueError(
                    f"{where}: 'port' is not valid for type 'sqlite' — a sqlite attach is a "
                    "local file, there is no port"
                )
            port = 0  # unused; sqlite has no port and no DEFAULT_PORTS entry
        else:
            port = int(port_raw) if port_raw else DEFAULT_PORTS[db_type]
            if not 0 < port < 65536:
                raise ValueError(f"{where}: port {port} out of range")

        user = str(entry.get("user", "")).strip()
        user_env = str(entry.get("user_env", "")).strip()
        if is_sqlite:
            if user or user_env:
                raise ValueError(
                    f"{where}: 'user'/'user_env' are not valid for type 'sqlite' — "
                    "a sqlite file has no server user"
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
                else:  # sqlserver
                    raise ValueError(
                        f"{where}: type 'sqlserver' requires an explicit 'user' or 'user_env' "
                        "— there is no safe default (refusing to fall back to 'sa')"
                    )

        password_env = str(entry.get("password_env", "")).strip()
        if "password_env" not in entry and not is_sqlite:
            raise ValueError(
                f"{where}: 'password_env' is required (set it to \"\" explicitly for trust-auth over a unix socket)"
            )
        if password_env and password_env not in env:
            raise ValueError(
                f"{where}: password_env {password_env!r} is not present in the "
                "environment — inject it via the deployment secret, never in config"
            )

        params = _validate_params(entry.get("params", {}), where)
        if is_sqlite and params:
            raise ValueError(
                f"{where}: 'params' are not valid for type 'sqlite' — the DSN is just the "
                "file path, there is nothing to parameterize"
            )
        if db_type in ("mysql", "mariadb"):
            # The mysql/mariadb DSN is serialized bare (mysql_scanner's DSN
            # parser splits on whitespace and does not strip quotes), so every
            # value that reaches it must be whitespace-free — including the
            # user_env-resolved user. Fail at startup with the field named.
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


def _merge_params(pairs: list[tuple[str, str]], params: tuple[tuple[str, str], ...], *, case_insensitive: bool = False) -> list[tuple[str, str]]:
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


def ensure_extensions(con, types: set[str]) -> None:
    """Explicitly LOAD the scanner extensions a query needs.

    Preinstalled in the image (auto-install stays disabled); LOAD must run
    BEFORE the filesystem lockdown because DuckDB loads the extension .so
    through its own filesystem layer. Types resolve through
    :data:`_EXTENSIONS` (e.g. mariadb LOADs mysql_scanner, sqlserver LOADs
    the community mssql extension); sorted() keeps the statement order
    deterministic.
    """
    for t in sorted(types):
        try:
            ext = _EXTENSIONS[t]
        except KeyError:
            raise ValueError(
                f"unknown attach type {t!r} (use one of {', '.join(sorted(_EXTENSIONS))})"
            ) from None
        con.execute(f"LOAD {ext}")


def apply_external(con, specs: list[AttachSpec], environ: dict[str, str] | None = None) -> None:
    """LOAD extensions and ATTACH every configured database (READ_ONLY).

    Must run BEFORE the connection's filesystem lockdown (see module
    docstring). Any failure raises ExternalAttachError with a message
    scrubbed of resolved secrets.
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


def scrub_secrets(text: str, specs: list[AttachSpec], environ: Mapping[str, str] | None = None) -> str:
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
