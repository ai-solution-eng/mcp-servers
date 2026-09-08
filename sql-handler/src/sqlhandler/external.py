"""Read-only external-database attach support (Postgres / MySQL).

SQLhandler's engine is a *lake* engine: tables are Parquet/Delta datasets
opened by pyarrow and registered into DuckDB as views. Some questions need
the *system of record* instead — the operational database behind the lake.
This module attaches such servers to a query connection via DuckDB's
scanner extensions (``postgres_scanner`` / ``mysql_scanner``), strictly
read-only, so one query can JOIN lake tables with live operational data:

    SELECT o.status, count(*)
    FROM ops.public.work_orders o           -- attached Postgres (read-only)
    JOIN work_order_header w ON w.id = o.id -- registered lake table
    GROUP BY 1

Security posture (mirrors :func:`sqlhandler.engine._duckdb_fs_lockdown`):

* ``READ_ONLY`` is forced on every ATTACH — DuckDB itself rejects
  INSERT/UPDATE/DELETE/CREATE on the attached catalog. The database is a
  source, never a sink.
* Credentials never pass through the model or any tool output: the config
  carries env-var *names* (``password_env``); the value is read from the
  process environment at attach time, a raw ``password`` key in the config
  is rejected outright, and every error message is scrubbed of resolved
  secrets before it can reach a client.
* The connection keeps ``disabled_filesystems='LocalFileSystem'`` —
  attaching a database does NOT re-open DuckDB file reads (no
  ``read_parquet('/etc/passwd')``, no ``COPY ... TO``). Only the network
  scanner extensions are LOADed, explicitly, and they must be PREINSTALLED
  in the image (auto-install stays off — no runtime downloads).

Ordering matters and is verified empirically (duckdb 1.5.x): extension
``LOAD`` and ``ATTACH`` both use DuckDB's filesystem layer internally, but
query-time data fetch does not (libpq talks TCP/unix-socket directly).
Hence the required sequence on a fresh connection:

    LOAD postgres_scanner                                  # fs still open
    ATTACH '<dsn>' AS <alias> (TYPE postgres, READ_ONLY)   # fs still open
    SET disabled_filesystems='LocalFileSystem'             # THEN lock down
    -- attached tables stay queryable; lake views register as usual

Configuration (environment variables):

    SQLHANDLER_ATTACH        inline JSON, same schema as the file
    SQLHANDLER_ATTACH_FILE   path to a JSON file of attach specs

    JSON: {"databases": [ {"name": "ops", "type": "postgres",
            "host": "pg.internal", "port": 5432, "database": "opsdb",
            "user": "ro_user", "password_env": "OPS_PG_PASSWORD"} ]}
    (a bare array is also accepted; ``user``/``user_env`` optional —
    ``password_env`` is required but may be "" for trust-auth over a unix
    socket; a literal ``password`` key is always an error)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from .provider import LakehouseError

logger = logging.getLogger("sqlhandler.engine")

ATTACH_TYPES = ("postgres", "mysql")
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306}
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# DuckDB's built-in catalog names — attaching over them would shadow engine
# internals; reject rather than surprise.
_RESERVED_ALIASES = {"memory", "system", "temp"}
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


class ExternalAttachError(LakehouseError):
    """An external database could not be attached (scrubbed message)."""


@dataclass(frozen=True)
class AttachSpec:
    """One read-only external database to ATTACH on demand.

    ``password_env`` names the environment variable holding the password;
    the empty string means trust-auth (e.g. a unix-socket connection) and
    sends no password at all. The secret value itself never lives in this
    object — only in the process environment.
    """

    name: str  # DuckDB catalog alias used in SQL: <name>.<schema>.<table>
    type: str  # "postgres" | "mysql"
    host: str  # hostname, IP, or a unix-socket directory (postgres)
    port: int
    database: str
    user: str
    password_env: str
    read_only: bool = True  # v1: always True; the field documents intent

    def password(self, environ: dict[str, str] | None = None) -> str | None:
        env = os.environ if environ is None else environ
        if not self.password_env:
            return None
        return env.get(self.password_env, "")

    @property
    def display_uri(self) -> str:
        """Credential-free identifier for tool output."""
        return f"{self.type}://{self.host}:{self.port}/{self.database}"


def _reject_secret_keys(entry: dict, where: str) -> None:
    for key in ("password", "passwd", "secret", "pwd"):
        if key in entry:
            raise ValueError(
                f"{where}: literal {key!r} keys are not allowed in attach config — "
                f"pass the env-var NAME via 'password_env' instead; secrets flow "
                "environment -> pod, never through config or the model."
            )


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
            raise ValueError(
                f"{where}: type {db_type!r} not supported (use one of {', '.join(ATTACH_TYPES)})"
            )
        host = str(entry.get("host", "")).strip()
        database = str(entry.get("database", "")).strip()
        if not host:
            raise ValueError(f"{where}: host is required")
        if not database:
            raise ValueError(f"{where}: database is required")

        port_raw = str(entry.get("port", "")).strip()
        port = int(port_raw) if port_raw else DEFAULT_PORTS[db_type]
        if not 0 < port < 65536:
            raise ValueError(f"{where}: port {port} out of range")

        user = str(entry.get("user", "")).strip()
        user_env = str(entry.get("user_env", "")).strip()
        if user and user_env:
            raise ValueError(f"{where}: use either 'user' or 'user_env', not both")
        if user_env:
            user = env.get(user_env, "")
            if not user:
                raise ValueError(
                    f"{where}: user_env {user_env!r} is set but the environment "
                    "variable is missing or empty"
                )
        if not user:
            user = "postgres" if db_type == "postgres" else "root"

        password_env = str(entry.get("password_env", "")).strip()
        if "password_env" not in entry:
            raise ValueError(
                f"{where}: 'password_env' is required (set it to \"\" explicitly "
                "for trust-auth over a unix socket)"
            )
        if password_env and password_env not in env:
            raise ValueError(
                f"{where}: password_env {password_env!r} is not present in the "
                "environment — inject it via the deployment secret, never in config"
            )

        specs.append(
            AttachSpec(
                name=name,
                type=db_type,
                host=host,
                port=port,
                database=database,
                user=user,
                password_env=password_env,
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


def build_attach_sql(spec: AttachSpec, password: str | None) -> str:
    """The exact ATTACH statement for one spec (READ_ONLY always enforced)."""
    parts: list[str] = [f"host={_sql_quote(spec.host)}", f"port={spec.port}"]
    if spec.database:
        parts.append(f"dbname={_sql_quote(spec.database)}" if spec.type == "postgres" else f"database={_sql_quote(spec.database)}")
    if spec.user:
        parts.append(f"user={_sql_quote(spec.user)}")
    if spec.type == "mysql":
        # mysql_scanner's DSN keys follow MySQL conventions
        parts = [p.replace("dbname=", "database=") for p in parts]
        if password:
            parts.append(f"passwd={_sql_quote(password)}")
    elif password:
        parts.append(f"password={_sql_quote(password)}")
    if spec.type == "postgres":
        parts.append("connect_timeout=10")
    dsn = " ".join(parts)
    # The DSN itself sits inside the ATTACH '…' SQL literal: values quoted by
    # _sql_quote (e.g. a socket path host='/tmp/…') must be escaped once more
    # at this level (' -> '') or the inner quote terminates the literal.
    dsn = dsn.replace("'", "''")
    ro = ", READ_ONLY" if spec.read_only else ""
    return f"ATTACH '{dsn}' AS \"{spec.name}\" (TYPE {spec.type}{ro})"


def ensure_extensions(con, types: set[str]) -> None:
    """Explicitly LOAD the scanner extensions a query needs.

    Preinstalled in the image (auto-install stays disabled); LOAD must run
    BEFORE the filesystem lockdown because DuckDB loads the extension .so
    through its own filesystem layer.
    """
    for t in sorted(types):
        con.execute(f"LOAD {t}_scanner")


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


def scrub_secrets(text: str, specs: list[AttachSpec], environ: dict[str, str] | None = None) -> str:
    """Remove resolved passwords (and a failure's DSN echoes) from ``text``."""
    env = os.environ if environ is None else environ
    for spec in specs:
        pw = spec.password(env)
        if pw:
            text = text.replace(pw, "***")
    return text


def _scrub(text: str, specs: list[AttachSpec], env: dict[str, str]) -> str:
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
        raise LakehouseError(
            f"Table {table!r} is not an attached-database reference (<{alias}.…>)"
        )
    parts = table.split(".")
    if len(parts) not in (2, 3) or not all(_IDENT_RE.match(p) for p in parts):
        raise LakehouseError(
            f"Attached table reference {table!r} must be "
            f"<{alias}.<table>> or <{alias}.<schema>.<table>> with plain identifiers"
        )
    return table
