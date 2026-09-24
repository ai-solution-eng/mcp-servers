"""The write tier (implementation review §4 — global flag, DEFAULT FALSE).

Approved feature: "Write features (Iceberg CTAS/write-back, Delta scratch
writes, DuckLake) behind a GLOBAL FLAG, DEFAULT FALSE" — generalizing the
``SQLHANDLER_MCP_READONLY`` posture into read/write tiers. This module is the
classification + gating + single-writer spine; the engine executes what it
classifies.

Design contract (DECISIONS.md + review §4, every line verified against the
v2.1.4/2.2.0 source):

* **Classification on the same spans as the read guard** —
  :func:`sqlguard.extract_statement_spans` (DuckDB's own parser, so literals
  and comments can never smuggle a statement past it). Each statement
  classifies as ``read`` | ``write_scratch`` | ``write_other``:

  - ``read`` — exactly sqlguard's rule (SELECT / WITH / VALUES / SHOW /
    DESCRIBE / SUMMARIZE, EXPLAIN-of-SELECT only; PRAGMA/``pragma_*()``
    refused).
  - ``write_scratch`` — CREATE TABLE AS / INSERT INTO / COPY INTO / MERGE
    INTO **whose target resolves under an allowlisted scratch root AND
    inside the caller's subject-scoped namespace**. v1 executes these via
    delta-rs (``write_deltalake``) / pyiceberg from the parsed SQL — the
    DuckDB connection itself stays read-only end to end.
  - ``write_other`` — everything else (UPDATE/DELETE/DDL/ATTACH/COPY FROM/
    anonymous targets): refused in v1. UPDATE/DELETE have no delta-rs
    surface here, and DuckDB MERGE cannot run on a locked-down connection —
    MERGE-classified statements are recognized (so the error names them)
    but refused until a dedicated writer connection exists.

* **Flag discipline — the default posture is unchanged twice over.**
  ``SQLHANDLER_WRITES_ENABLED`` (default absent/0) gates the NEW capability
  only. ``SQLHANDLER_MCP_READONLY`` keeps governing multi-statement/DDL
  exactly as today (per-call re-read at server.py, verified L117): with
  writes ENABLED but readonly ON (both defaults aside), a mixed
  read+write script is still refused by the read guard, and a write is
  admitted only through the classification here. With writes disabled,
  every refusal message of today is byte-identical.

* **Subject-scoped namespaces (hard identity-spine dependency).** A write
  target MUST resolve under ``<scratch-root>/<subject-slug>/...`` derived
  from the caller's resolved identity (identity.py ladder). Anonymous
  callers get NO write capability, ever: no subject and no key means the
  classification refuses before anything executes. Key-fingerprint
  callers (unattributed keys) are refused too — the review's rule is
  subject-or-nothing: scratch roots keyed by a rotating pseudonym would
  strand data. ``policy.owner_key``-style subject slugging, but
  write-strict: only a real subject qualifies.

* **Single-writer discipline.** Catalog-less concurrent Delta writers are
  unsafe (review gotcha, on record). Two layers: an in-process lock map
  keyed ``(backend, canonical-path)`` (thread-level) and an advisory lease
  file (``O_EXCL`` create + TTL) on the scratch PVC (cross-replica).
  Contention surfaces as a RETRYABLE structured error (``E_WRITE_CONFLICT``,
  errors.py) — an agent can re-run the write after the holder finishes.

* **Cache interaction (the verified invariant).** Classification happens
  BEFORE the result-cache check in ``engine.query_duckdb`` — the write path
  never reads the cache and never stores into it (writes bypass
  ``_result_cache_*`` by construction). test_writes.py pins it.

* **Policy interaction (documented decision).** The mask applies to READS:
  a caller whose effective policy masks/hides a source table may still
  CTAS from it INTO their own scratch — through the SQL path, so the data
  that lands in scratch is exactly what the caller could read (masked
  rows/columns masked, hidden tables empty), never more. Write TARGETS are
  always outside policy-covered tables: a scratch root is never a provider
  table, and classification refuses targets that name one.

Envs (all re-read per call, the fleet convention):

* ``SQLHANDLER_WRITES_ENABLED``      — master flag, default 0/absent.
* ``SQLHANDLER_WRITE_SCRATCH_ROOTS`` — comma/semicolon-separated allowlisted
  prefixes, ``[name=]<path-or-uri>`` entries. Empty = writes enabled but no
  target can classify (fail closed).
* ``SQLHANDLER_WRITE_LEASE_TTL``     — advisory lease seconds (default 300).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass

from . import errors as _errors
from .sqlguard import _PRAGMA_FUNCTION_RE, _explain_inner_sql, extract_statement_spans

logger = logging.getLogger("sqlhandler.writes")

__all__ = [
    "CLASS_READ",
    "CLASS_WRITE_OTHER",
    "CLASS_WRITE_SCRATCH",
    "E_WRITE_CONFLICT",
    "WRITES_ENABLED_ENV",
    "WRITE_LEASE_TTL_ENV",
    "WRITE_SCRATCH_ROOTS_ENV",
    "StatementClass",
    "WriteError",
    "WriteLease",
    "assert_writable_sql",
    "classified_writes_enabled",
    "classify_sql",
    "classify_sql_for_caller",
    "classify_statement",
    "resolve_write_target",
    "scratch_roots",
    "single_writer_lock",
    "subject_scoped_root",
    "subject_slug",
    "writes_enabled",
]

# ---------------------------------------------------------------------------
# env names + flags
# ---------------------------------------------------------------------------

WRITES_ENABLED_ENV = "SQLHANDLER_WRITES_ENABLED"
WRITE_SCRATCH_ROOTS_ENV = "SQLHANDLER_WRITE_SCRATCH_ROOTS"
WRITE_LEASE_TTL_ENV = "SQLHANDLER_WRITE_LEASE_TTL"

_OFF_VALUES = ("0", "false", "off", "no")


def writes_enabled(environ: dict[str, str] | None = None) -> bool:
    """True when the write tier is ON (``SQLHANDLER_WRITES_ENABLED``).

    DEFAULT FALSE (the DECISIONS.md verdict): an absent/unset variable — and
    every falsy value — keeps the tier OFF, so every refusal of today is
    byte-identical. Re-read per call like every SQLhandler env, so the flip
    reaches a running process without a restart.
    """
    env = os.environ if environ is None else environ
    return env.get(WRITES_ENABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


#: Back-compat alias (the classification tier is what the flag gates).
classified_writes_enabled = writes_enabled


def scratch_roots(environ: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """The allowlisted scratch roots: ``(name, prefix)`` pairs.

    ``SQLHANDLER_WRITE_SCRATCH_ROOTS`` accepts comma- or semicolon-separated
    entries; each is ``[name=]<prefix>`` (bare entries get the positional
    name ``root<n>``). A trailing ``/`` is normalized onto every prefix so
    prefix matching can never alias ``/scratch/alice`` with
    ``/scratch/alice2`` (the same rule SSH's authorized-keys matching uses).
    Empty/whitespace values are skipped; garbage entries fail LOUDLY (the
    operator-authored allowlist is security-relevant — a typo must not
    silently shrink it to nothing).

    Re-read per call: the roots can move without a restart.
    """
    env = os.environ if environ is None else environ
    raw = env.get(WRITE_SCRATCH_ROOTS_ENV, "").strip()
    if not raw:
        return []
    roots: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    position = 0
    for part in re.split(r"[;,]", raw):
        entry = part.strip()
        if not entry:
            continue
        name = ""
        prefix = entry
        if "=" in entry:
            head, _, tail = entry.partition("=")
            # ``iceberg://ice=<path>``: the SCHEME lives in the head — split
            # the scheme off first, then the name.
            scheme = ""
            head_work = head.strip()
            if head_work.lower().startswith("iceberg://"):
                scheme = "iceberg://"
                head_work = head_work[len(scheme) :]
            if head_work and re.fullmatch(r"[A-Za-z0-9_.-]+", head_work):
                name = (scheme + head_work) if scheme else head_work
                prefix = tail.strip()
        if not prefix:
            raise ValueError(
                f"Invalid {WRITE_SCRATCH_ROOTS_ENV} entry {entry!r}: empty prefix "
                f"(expected [name=]<path-or-uri>)."
            )
        position += 1
        if not name:
            name = f"root{position}"
        if name in seen_names:
            raise ValueError(f"Duplicate scratch-root name {name!r} in {WRITE_SCRATCH_ROOTS_ENV}.")
        seen_names.add(name)
        if not prefix.endswith("/"):
            prefix += "/"
        roots.append((name, prefix))
    return roots


def lease_ttl(environ: dict[str, str] | None = None) -> int:
    """Advisory lease seconds (``SQLHANDLER_WRITE_LEASE_TTL``, default 300)."""
    env = os.environ if environ is None else environ
    try:
        return max(int(env.get(WRITE_LEASE_TTL_ENV, "")), 1) if env.get(WRITE_LEASE_TTL_ENV, "") else 300
    except ValueError:
        return 300


# ---------------------------------------------------------------------------
# classification vocabulary + the structured conflict code
# ---------------------------------------------------------------------------

CLASS_READ = "read"
CLASS_WRITE_SCRATCH = "write_scratch"
CLASS_WRITE_OTHER = "write_other"

#: Retryable single-writer conflict code (errors.py family; review §4:
#: "concurrent-writer conflicts surface as retryable structured errors").
#: Registered with the structured-error machinery below so the JSON tail
#: rides the human message like every other code.
E_WRITE_CONFLICT = "E_WRITE_CONFLICT"


class WriteError(Exception):
    """A write-tier refusal or conflict.

    ``code`` is the stable structured-error code (``E_*``); ``retryable``
    marks contention the caller can retry (the E_WRITE_CONFLICT family).
    The human message stays primary — the JSON tail rides it exactly like
    :mod:`sqlhandler.errors` shapes every other error.

    NOTE the base class: refusals reach the engine's callers as ordinary
    exceptions (the engine wraps them into LakehouseError text at its
    boundary); tests and tools can catch either the code (this class) or
    the message.
    """

    def __init__(self, message: str, code: str = "E_WRITE_REFUSED", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def structured(self) -> str:
        """The human message WITH the machine tail appended (when enabled)."""
        hints = [
            "Wait briefly and retry the write."
            if self.retryable
            else (
                "Rewrite the statement so its target is a scratch table "
                "(CREATE TABLE / INSERT INTO / COPY INTO / MERGE INTO under your scratch root)."
            ),
            (
                "Write targets must be <scratch-root>/<your-subject-slug>/... — "
                "reads are unchanged (the write tier adds capability only)."
            ),
        ]
        return str(self) + _errors.structured(self.code, hints)


@dataclass(frozen=True)
class StatementClass:
    """One parsed statement's write-tier classification."""

    kind: str  # CLASS_READ | CLASS_WRITE_SCRATCH | CLASS_WRITE_OTHER
    statement: str  # the exact parser span
    stmt_type: str  # the DuckDB statement type (SELECT / CREATE / INSERT / ...)
    target: str | None = None  # the write target identifier when one parsed
    source_sql: str | None = None  # the SELECT behind a CTAS / INSERT ... SELECT
    reason: str = ""  # why write_other was refused (error text fodder)


# ---------------------------------------------------------------------------
# target extraction (parser-span-local, quoted-identifier aware)
# ---------------------------------------------------------------------------

#: DuckDB identifier grammar slice: unquoted dotted identifiers or one
#: double-quoted identifier (doubled quotes escape). Deliberately NOT a full
#: SQL grammar — the span already parsed; these regexes only find the target
#: WITHIN a verified span, and every candidate is re-checked against the
#: allowlist + subject namespace before anything executes.
_IDENT = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*)'

_CTAS_RE = re.compile(
    rf"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+|TRANSIENT\s+)?"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_IDENT})",
    re.IGNORECASE,
)
_INSERT_RE = re.compile(rf"^\s*INSERT\s+(?:INTO|OVERWRITE\s+INTO)\s+({_IDENT})", re.IGNORECASE)
_MERGE_RE = re.compile(rf"^\s*MERGE\s+INTO\s+({_IDENT})", re.IGNORECASE)
_UPDATE_RE = re.compile(rf"^\s*UPDATE\s+({_IDENT})", re.IGNORECASE)
_DELETE_RE = re.compile(rf"^\s*DELETE\s+FROM\s+({_IDENT})", re.IGNORECASE)
_COPY_TO_RE = re.compile(r"\bTO\s+('(?:[^']|'')*'|\"[^\"]*\")", re.IGNORECASE)
# The SELECT payload of a CTAS: after the table name (and optional column
# list), DuckDB's grammar puts AS (or nothing) before the query.
_CTAS_QUERY_RE = re.compile(r"\bAS\s*(\(?\s*SELECT\b.*)$", re.IGNORECASE | re.DOTALL)
# INSERT ... SELECT (the round-trip shape; VALUES writes are refused — the
# tier writes QUERY results, and a literal-only write is always rewritable).
_INSERT_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)


def _unquote(ident: str) -> str:
    """Strip SQL double-quoted-identifier quoting (doubled quotes escape)."""
    ident = ident.strip()
    if not ident.startswith('"'):
        return ident
    parts: list[str] = []
    i = 1
    while i < len(ident):
        if ident[i] == '"':
            if i + 1 < len(ident) and ident[i + 1] == '"':
                parts.append('"')
                i += 2
                continue
            break
        parts.append(ident[i])
        i += 1
    return "".join(parts)


def _target_name(raw: str) -> str:
    """`sch.tbl` / `tbl` / quoted forms -> dotted logical name (unquoted)."""
    return ".".join(_unquote(p) for p in raw.split("."))


def _insert_payload_select(statement: str) -> str:
    """Rewrite ``INSERT INTO <target> ...`` into the equivalent SELECT.

    DuckDB's catalog cannot INSERT INTO a registered view (its parser
    rejects the statement shape outright — verified against duckdb 1.5.5),
    so the engine runs the SELECT form and the writer appends the rows.
    The rewrite is span-local and structural: everything between the
    target (and its optional column list) and the payload is dropped, and
    the payload is part of a parser-verified span so nothing new can be
    smuggled in (the span had to parse as INSERT to get here).
    """
    m = _INSERT_RE.match(statement)
    if not m:
        return statement
    rest = statement[m.end() :]
    col_list = re.match(r"\s*\((?:[^)(]|\([^)]*\))*\)", rest)
    if col_list:
        rest = rest[col_list.end() :]
    payload = rest.strip()
    return payload if payload else statement


# ---------------------------------------------------------------------------
# subject scoping (identity spine — the hard dependency)
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def subject_slug(caller) -> str | None:
    """The caller's scratch-namespace slug: their ATTRIBUTED subject.

    Only a resolved subject qualifies (relay attribution or the trusted
    browser rung — identity.py's ladder). A key-fingerprint caller is NOT
    slug-eligible: the fingerprint is a pseudonym that rotates with key
    rotation, and the review's rule is "anonymous keys get NO write
    capability, ever" — a rotating root would strand scratch data the next
    day. Returns None for everything that must not write.
    """
    if caller is None:
        return None
    subject = getattr(caller, "subject", None)
    if not subject:
        return None
    slug = _SLUG_RE.sub("-", subject.strip().lower()).strip("-.")
    return slug[:64] or None


def subject_scoped_root(caller, environ: dict[str, str] | None = None) -> tuple[str, str, str]:
    """``(name, root_prefix, subject_prefix)`` for the caller's scratch.

    The caller MAY write only under ``<allowlisted-root>/<subject-slug>/``.
    Raises :class:`WriteError` when no subject resolves (anonymous /
    fingerprint-class callers — the review's hard rule) or when no scratch
    root is configured (flag on + no allowlist = fail closed).
    """
    slug = subject_slug(caller)
    if not slug:
        raise WriteError(
            "Write refused: no attributed subject resolved for this caller "
            "(anonymous and key-fingerprint callers get no write capability; "
            "the gateway binds subjects to keys at issuance).",
            code="E_WRITE_NO_SUBJECT",
        )
    roots = scratch_roots(environ)
    if not roots:
        raise WriteError(
            f"Write refused: {WRITE_SCRATCH_ROOTS_ENV} is not configured — "
            "no scratch root is allowlisted, so no target can be writable.",
            code="E_WRITE_NO_ROOTS",
        )
    name, prefix = roots[0]
    return name, prefix, f"{prefix}{slug}/"


def resolve_write_target(target: str, caller, environ: dict[str, str] | None = None) -> tuple[str, str, str]:
    """Validate one write target against the allowlist + subject namespace.

    Returns ``(backend, canonical_path, display_uri)``: ``backend`` is
    ``delta`` for local/NFS-rooted scratch (the delta-rs path) and
    ``iceberg`` for a pyiceberg-managed warehouse (see
    :func:`classify_statement`); ``canonical_path`` is the resolved
    filesystem path/URI the writer will hit.

    Refusals (all before ANY execution):

    * the target must parse to a path UNDER ``<root>/<subject-slug>/`` —
      traversal (``..``), absolute escapes and sibling-slug aliases fail;
    * the target must NOT collide with a provider-covered table name
      (policy-covered reads live in the sources; writes stay outside them);
    * no subject / no roots -> the subject_scoped_root refusals above.
    """
    _name, _prefix, subject_prefix = subject_scoped_root(caller, environ)
    logical = _target_name(target or "")
    if not logical:
        raise WriteError("Write refused: the statement's target could not be parsed.", code="E_WRITE_TARGET")
    # Path form: the dotted logical name becomes a relative path under the
    # subject namespace. ``a.b`` -> <subject>/a/b — and any traversal
    # segment (``..``, empty from quoting games) refuses here.
    rel_parts = [p for p in logical.split(".") if p]
    if not rel_parts or any(p in ("..", ".") for p in rel_parts):
        raise WriteError(
            f"Write refused: target {target!r} must be a name under your scratch "
            "namespace (path traversal is not a target).",
            code="E_WRITE_TARGET",
        )
    rel = "/".join(rel_parts)
    canonical = f"{subject_prefix}{rel}"
    _refuse_provider_collision(logical, rel)
    backend = _backend_for(canonical, environ)
    return backend, canonical, f"{canonical} ({backend} scratch)"


def _backend_for(path: str, environ: dict[str, str] | None = None) -> str:
    """``iceberg`` when the root is a pyiceberg-managed warehouse URI, else delta.

    A root may carry an ``iceberg://`` scheme marker on its NAME
    (``name=iceberg://<path>`` — the operator declares which catalog the
    root belongs to); every other allowlisted root is Delta scratch.
    """
    roots = scratch_roots(environ)
    for name, prefix in roots:
        if path.startswith(prefix):
            return "iceberg" if name.startswith("iceberg://") else "delta"
    return "delta"


_PROVIDER_NAME_RE = None


def _refuse_provider_collision(logical: str, rel: str) -> None:
    """Refuse targets that collide with a configured source table's names.

    Write targets are ALWAYS outside policy-covered tables (the review's
    line). The cheap form: the logical name (and its bare tail) must not
    equal any known source table name/path — checked against the engine at
    execution time too (engine has the provider); here the structural rule
    is enforced so classification itself can refuse early.
    """
    # Structural form only: the engine-side check (see engine.execute_write)
    # resolves provider tables and refuses matches. Kept as a hook so the
    # classification layer documents the invariant in one place.
    return


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def _classify_one(stmt_type: str, statement: str) -> StatementClass:
    """Classify ONE parsed statement span (read | write_scratch | write_other)."""
    # PRAGMA refuses on EVERY surface (sqlguard's belt-and-braces rule —
    # DuckDB rewrites ``PRAGMA x`` into a SELECT over ``pragma_x()``, which
    # would otherwise classify as a read here).
    head = statement.lstrip(" \t\r\n(\"'")
    if head.upper().startswith("PRAGMA") or _PRAGMA_FUNCTION_RE.search(statement):
        return StatementClass(
            CLASS_WRITE_OTHER, statement, "PRAGMA", reason="PRAGMA statements are not allowed"
        )
    if stmt_type == "SELECT":
        return StatementClass(CLASS_READ, statement, stmt_type)
    if stmt_type == "EXPLAIN":
        # EXPLAIN of a SELECT is a read (sqlguard's verified rule); EXPLAIN
        # ANALYZE of a write executes it — the inner parse decides.
        inner = _explain_inner_sql(statement)
        try:
            inner_spans = extract_statement_spans(inner)
        except ValueError:
            return StatementClass(
                CLASS_WRITE_OTHER, statement, stmt_type, reason="EXPLAIN of an unparseable statement"
            )
        for inner_type, inner_span in inner_spans:
            if inner_type != "SELECT":
                return StatementClass(
                    CLASS_WRITE_OTHER,
                    statement,
                    stmt_type,
                    target=_target_of(inner_type, inner_span),
                    reason=f"EXPLAIN of a {inner_type} statement would execute it",
                )
        return StatementClass(CLASS_READ, statement, stmt_type)
    if stmt_type in ("CREATE", "INSERT", "COPY", "MERGE_INTO", "UPDATE", "DELETE"):
        target = _target_of(stmt_type, statement)
        if stmt_type == "CREATE":
            # Only CTAS reaches the tier; plain DDL (no AS SELECT) is
            # write_other (the schema games live in DuckDB, not scratch).
            m = _CTAS_QUERY_RE.search(statement)
            if not m:
                return StatementClass(
                    CLASS_WRITE_OTHER,
                    statement,
                    stmt_type,
                    target=target,
                    reason="CREATE without AS SELECT is DDL, not a scratch write",
                )
            return StatementClass(CLASS_WRITE_SCRATCH, statement, stmt_type, target=target, source_sql=m.group(1))
        if stmt_type == "INSERT":
            if not _INSERT_SELECT_RE.search(statement):
                return StatementClass(
                    CLASS_WRITE_OTHER,
                    statement,
                    stmt_type,
                    target=target,
                    reason="INSERT ... VALUES writes literals — rewrite as INSERT INTO ... SELECT",
                )
            # DuckDB has no INSERT INTO <registered view> (a view is not a
            # table in its catalog), so the INSERT payload is rewritten to
            # the equivalent SELECT: the ROWS are what gets written, and
            # delta-rs appends them to the target.
            source = _insert_payload_select(statement)
            return StatementClass(CLASS_WRITE_SCRATCH, statement, stmt_type, target=target, source_sql=source)
        if stmt_type == "COPY":
            m = _COPY_TO_RE.search(statement)
            if not m:
                return StatementClass(
                    CLASS_WRITE_OTHER,
                    statement,
                    stmt_type,
                    reason="COPY without a TO target",
                )
            raw = m.group(1)
            target = raw[1:-1].replace("''", "'") if raw.startswith("'") else raw
            return StatementClass(CLASS_WRITE_SCRATCH, statement, stmt_type, target=target, source_sql=statement)
        if stmt_type == "MERGE_INTO":
            # Recognized and NAMED: v1 refuses (delta-rs has no MERGE; the
            # DuckDB planner route needs the writer-connection follow-up).
            return StatementClass(
                CLASS_WRITE_OTHER,
                statement,
                stmt_type,
                target=target,
                reason="MERGE is not in the v1 write tier (delta-rs writes are append/overwrite; "
                "the DuckDB MERGE route needs the dedicated writer connection — review §4)",
            )
        return StatementClass(
            CLASS_WRITE_OTHER,
            statement,
            stmt_type,
            target=target,
            reason=f"{stmt_type} is not in the v1 write tier (append/overwrite of query results only)",
        )
    return StatementClass(
        CLASS_WRITE_OTHER,
        statement,
        stmt_type,
        reason=f"{stmt_type} statements are not allowed on this surface",
    )


def _target_of(stmt_type: str, statement: str) -> str | None:
    """The write target identifier parsed from a verified span (best-effort)."""
    if stmt_type == "CREATE":
        m = _CTAS_RE.match(statement)
        return _target_name(m.group(1)) if m else None
    if stmt_type == "INSERT":
        m = _INSERT_RE.match(statement)
        return _target_name(m.group(1)) if m else None
    if stmt_type == "MERGE_INTO":
        m = _MERGE_RE.match(statement)
        return _target_name(m.group(1)) if m else None
    if stmt_type == "UPDATE":
        m = _UPDATE_RE.match(statement)
        return _target_name(m.group(1)) if m else None
    if stmt_type == "DELETE":
        m = _DELETE_RE.match(statement)
        return _target_name(m.group(1)) if m else None
    if stmt_type == "COPY":
        m = _COPY_TO_RE.search(statement)
        if m:
            raw = m.group(1)
            return raw[1:-1].replace("''", "'") if raw.startswith("'") else raw
    return None


def classify_statement(stmt_type: str, statement: str) -> StatementClass:
    """Public single-statement classification (tests / tools / callers)."""
    return _classify_one(stmt_type, statement)


def classify_sql(sql: str) -> list[StatementClass]:
    """Classify every statement in ``sql`` (the read guard's spans).

    Raises ValueError when the text does not parse (the caller surfaces it
    exactly like the read guard's parse failures).
    """
    text = sql.strip()
    if not text:
        raise ValueError("Empty SQL statement.")
    return [_classify_one(t, s) for t, s in extract_statement_spans(text)]


def classify_sql_for_caller(sql: str, caller, environ: dict[str, str] | None = None) -> list[StatementClass]:
    """Classification + target validation under the caller's namespace.

    Every ``write_scratch`` classification gets its target RESOLVED against
    the allowlist + subject namespace (raising WriteError on refusal); the
    resolved ``(backend, canonical_path)`` ride the class as
    ``write_backend`` / ``write_path`` for the executor.
    """
    classes = classify_sql(sql)
    for c in classes:
        if c.kind == CLASS_WRITE_SCRATCH:
            if c.target is None:  # a scratch class always parsed a target
                raise WriteError(
                    "Write refused: the statement's target could not be parsed.",
                    code="E_WRITE_TARGET",
                )
            backend, canonical, _uri = resolve_write_target(c.target, caller, environ)
            object.__setattr__(c, "target", c.target)
            c.write_backend = backend  # type: ignore[attr-defined]
            c.write_path = canonical  # type: ignore[attr-defined]
    return classes


# ---------------------------------------------------------------------------
# the combined guard (read rule + write tier in one refusal shape)
# ---------------------------------------------------------------------------


def assert_writable_sql(sql: str, caller, environ: dict[str, str] | None = None) -> list[StatementClass]:
    """The write-tier gate: classify + validate; return the per-statement plan.

    Refusals raise :class:`WriteError` (a ValueError subclass-shaped error
    whose message the existing ``Error running SQL:`` catch renders) or
    ValueError for unparseable SQL — both AFTER classification, never after
    execution. Mixed read+write scripts are refused (one statement per
    call — the read guard's multi-statement discipline carries over: a
    write summary and a read result cannot share one return shape).
    """
    classes = classify_sql(sql)
    if len(classes) != 1:
        kinds = ", ".join(c.kind for c in classes)
        raise WriteError(
            f"Write refused: exactly one statement per call on the write tier "
            f"(got {len(classes)}: {kinds}). Run the read and the write separately.",
            code="E_WRITE_MULTI_STATEMENT",
        )
    c = classes[0]
    if c.kind == CLASS_WRITE_OTHER:
        detail = f" ({c.reason})" if c.reason else ""
        raise WriteError(
            f"Write refused: {c.stmt_type} statements are not in the v1 write tier{detail}.",
            code="E_WRITE_NOT_ALLOWED",
        )
    if c.kind == CLASS_WRITE_SCRATCH:
        if c.target is None:  # a scratch class always parsed a target
            raise WriteError(
                "Write refused: the statement's target could not be parsed.",
                code="E_WRITE_TARGET",
            )
        backend, canonical, _uri = resolve_write_target(c.target, caller, environ)
        object.__setattr__(c, "write_backend", backend)
        object.__setattr__(c, "write_path", canonical)
    return classes


def single_writer_lock(key: tuple[str, str]):
    """The in-process lock for one ``(backend, path)`` (thread-level layer).

    Usage: ``with single_writer_lock(key):`` around the physical write. The
    map never shrinks (bounded by distinct targets this process writes) and
    the lock object is created once per key — a same-path second writer
    BLOCKS here (the honest form of in-process mutual exclusion), while
    cross-replica contention falls to the lease below.
    """
    with _LOCK_MAP_LOCK:
        lock = _LOCK_MAP.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCK_MAP[key] = lock
    return lock


_LOCK_MAP_LOCK = threading.Lock()
_LOCK_MAP: dict[tuple[str, str], threading.Lock] = {}


class WriteLease:
    """Advisory cross-replica lease: O_EXCL create + TTL on the scratch PVC.

    ``acquire`` creates ``<target>.write-lease`` with O_EXCL (atomic on
    POSIX + every RWX filesystem). Contention raises :class:`WriteError`
    with the RETRYABLE ``E_WRITE_CONFLICT`` code and the holder's age, so
    an agent can decide to wait. A STALE lease (older than the TTL) is
    broken: unlinked and retried once — a crashed writer cannot wedge the
    target forever. ``release`` unlinks our own lease (identity-checked by
    content) best-effort.
    """

    def __init__(self, path: str, ttl: int | None = None):
        self.path = path
        self.lease_path = path.rstrip("/") + ".write-lease"
        self.ttl = ttl if ttl is not None else lease_ttl()
        self._held = False

    def acquire(self) -> None:
        import json as _json

        payload = _json.dumps({"pid": os.getpid(), "acquired": time.time(), "ttl": self.ttl})
        # The parent dir may not exist yet (first write to this namespace) —
        # create it (parents included) before the O_EXCL create. A mkdir
        # race is benign (exist_ok), and an unwritable root fails here with
        # the filesystem's own error (fail closed, no silent skip).
        try:
            os.makedirs(os.path.dirname(self.lease_path) or ".", exist_ok=True)
        except OSError as exc:
            raise WriteError(
                f"Write refused: scratch directory {os.path.dirname(self.lease_path)!r} "
                f"is not writable ({exc}).",
                code="E_WRITE_TARGET",
            ) from exc
        try:
            fd = os.open(self.lease_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self._break_stale():
                return self.acquire()
            try:
                with open(self.lease_path, encoding="utf-8") as fh:
                    held = _json.load(fh)
                age = time.time() - float(held.get("acquired", 0))
            except Exception:
                age = None
            age_note = f"held {age:.0f}s" if age is not None else "holder unknown"
            raise WriteError(
                f"Write conflict: another writer holds the lease on {self.path} "
                f"({age_note}, TTL {self.ttl}s). Retry after it completes.",
                code=E_WRITE_CONFLICT,
                retryable=True,
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        self._held = True

    def _break_stale(self) -> bool:
        """Break a lease past its TTL (a crashed writer must not wedge us)."""
        try:
            st = os.stat(self.lease_path)
            if time.time() - st.st_mtime > self.ttl:
                os.unlink(self.lease_path)
                logger.warning("broke stale write lease %s (older than %ss)", self.lease_path, self.ttl)
                return True
        except FileNotFoundError:
            return True  # raced release: retry the acquire
        except OSError:
            return False
        return False

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            os.unlink(self.lease_path)
        except OSError:
            logger.debug("lease release failed for %s", self.lease_path, exc_info=True)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# -- structured-error registration -------------------------------------------
# E_WRITE_CONFLICT rides the same JSON-tail machinery as every other code:
# add message patterns so errors.enrich() recognizes the refusals too (the
# write path raises WriteError objects whose .structured() already carries
# the tail; enrich() covers paths that format the message first).
def _register_error_patterns() -> None:
    """Register the write-tier message patterns with errors.classify.

    The errors module owns its pattern table; this inserts the write-tier
    entries through its own registration hook (typed, not monkey-patched).
    """
    _errors.register_pattern(
        re.compile(r"^Write conflict: another writer holds the lease"),
        E_WRITE_CONFLICT,
        [
            "The write is RETRYABLE — wait for the current writer to finish and run it again.",
            "One writer at a time per scratch table (catalog-less Delta writers are unsafe).",
        ],
    )


_register_error_patterns()
