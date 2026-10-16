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

import hashlib
import logging
import os
import re
import socket
import threading
import time
import uuid
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
                f"Invalid {WRITE_SCRATCH_ROOTS_ENV} entry {entry!r}: empty prefix (expected [name=]<path-or-uri>)."
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


#: A lease shorter than this cannot outlive a real scratch write, so
#: breaking a "stale" lease at that age is a guaranteed double-writer. The
#: audit (2026-10-16) found the old ``max(value, 1)`` clamp turned a
#: configured 0 into a ONE-SECOND lease. Refuse instead of clamping: the
#: value is operator-authored and a silent clamp hides the misconfiguration.
LEASE_TTL_MIN = 60
LEASE_TTL_DEFAULT = 300


def lease_ttl(environ: dict[str, str] | None = None) -> int:
    """Advisory lease seconds (``SQLHANDLER_WRITE_LEASE_TTL``, default 300).

    Values below :data:`LEASE_TTL_MIN` (or above 24h) are a LOUD config
    error, not a clamp (audit 2026-10-16 P3): the old ``max(value, 1)`` clamp
    turned a configured 0 into a ONE-SECOND lease, so ``_break_stale``
    displaced live writers. Non-numeric garbage raises too -- a typo must not
    silently take the default.
    """
    env = os.environ if environ is None else environ
    raw = env.get(WRITE_LEASE_TTL_ENV, "").strip()
    if not raw:
        return LEASE_TTL_DEFAULT
    try:
        ttl = int(raw)
    except ValueError:
        raise WriteError(
            f"{WRITE_LEASE_TTL_ENV}={raw!r} is not an integer (lease seconds)",
            code="E_WRITE_LEASE_TTL",
        ) from None
    if ttl < LEASE_TTL_MIN or ttl > 86400:
        raise WriteError(
            f"{WRITE_LEASE_TTL_ENV}={ttl} is outside the supported range "
            f"[{LEASE_TTL_MIN}, 86400]: a lease shorter than {LEASE_TTL_MIN}s "
            "cannot outlive a real scratch write, so _break_stale would "
            "displace a LIVE writer and two writers would corrupt a "
            "catalog-less Delta target. Use the default "
            f"({LEASE_TTL_DEFAULT}) or another value in range.",
            code="E_WRITE_LEASE_TTL",
        )
    return ttl


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
#: A quoted string literal, comment, or double-quoted identifier — the
#: regions a bare regex scan must NOT read as SQL structure. Used by the
#: "code-only" view of a span before any ``TO``/``SELECT`` keyword hunt.
_SQL_NOISE_RE = re.compile(
    r"--[^\n]*"  # line comment
    r"|/\*.*?\*/"  # block comment
    r"|'(?:[^']|'')*'"  # single-quoted literal (doubled-quote escape)
    r'|"(?:[^"]|"")*"'  # double-quoted identifier
    r"|\$\$.*?\$\$"  # dollar-quoted string
    r"|\$[A-Za-z_][A-Za-z0-9_]*\$.*?\$[A-Za-z_][A-Za-z0-9_]*\$",
    re.DOTALL,
)
#: The COPY ``TO`` KEYWORD. Located on the CODE-ONLY view (literals and
#: comments blanked), so a ``TO '...'`` inside a comment or an inner string
#: can never be mistaken for the destination clause; the literal itself is
#: then read from the ORIGINAL span at that offset (the code-only view has
#: it blanked). The LAST match is the destination — DuckDB's grammar puts
#: the target last, and the optional ``(options)`` that may follow carries
#: no ``TO``.
#: Deliberately the bare keyword (no trailing \s+): the code-only view has
#: the destination literal blanked, so consuming whitespace there would run
#: past the offset the ORIGINAL span must be read at. The literal is located
#: by skipping whitespace in the original.
_COPY_TO_KW_RE = re.compile(r"\bTO\b", re.IGNORECASE)
#: A quoted string literal / quoted identifier, anchored at a given offset.
_COPY_LITERAL_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
# The SELECT payload of a CTAS: after the table name (and optional column
# list), DuckDB's grammar puts AS (or nothing) before the query.
_CTAS_QUERY_RE = re.compile(r"\bAS\s*(\(?\s*SELECT\b.*)$", re.IGNORECASE | re.DOTALL)
# INSERT ... SELECT (the round-trip shape; VALUES writes are refused — the
# tier writes QUERY results, and a literal-only write is always rewritable).
# Matched against the CODE-ONLY view (literals/comments blanked), so a
# literal or comment that merely CONTAINS the word SELECT cannot smuggle a
# VALUES write past the refusal (audit 2026-10-16 P2: "INSERT INTO t
# VALUES ('SELECT')" was admitted).
_INSERT_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
#: The INSERT payload forms the tier REFUSES structurally, independent of any
#: literal text: ``VALUES ...`` and ``DEFAULT VALUES`` write constants, and
#: ``BY NAME``/``SET`` shapes are not query results either.
_INSERT_NON_QUERY_RE = re.compile(r"^\s*(?:DEFAULT\s+VALUES\b|VALUES\b|BY\s+NAME\b)", re.IGNORECASE)


def _code_only(statement: str) -> str:
    """A same-length view of the span with literals/comments blanked.

    Keyword hunting (``TO``, ``SELECT``, ``VALUES``) must never be decided by
    TEXT inside a string literal, a comment or a quoted identifier — that was
    the audit's COPY-decoy and INSERT-VALUES bypass. Every noise region is
    replaced by spaces of the SAME length so all offsets stay valid and the
    result can still be matched with the original span-local regexes.
    """
    if not statement:
        return statement
    out = list(statement)

    def _blank(match: re.Match) -> str:
        start, end = match.span()
        for i in range(start, end):
            # Newlines survive so ``^``-anchored multi-line matching and any
            # line-oriented reasoning stay sane.
            if out[i] != "\n":
                out[i] = " "
        return " " * (end - start)

    _SQL_NOISE_RE.sub(_blank, statement)
    return "".join(out)


def _copy_to_target(statement: str) -> str | None:
    """The COPY destination: the LAST top-level ``TO <literal>`` in the span.

    Two-step on purpose. The ``TO`` keyword is located in the CODE-ONLY
    view (comments and string literals blanked to same-length filler), so a
    decoy ``TO '<allowlisted>'`` hidden in a comment or an inner string can
    never be selected; the literal itself is then read from the ORIGINAL
    span at that exact offset. Returns None when the destination is not a
    quoted literal — the caller refuses, which is the fail-closed direction.
    """
    code = _code_only(statement)
    matches = list(_COPY_TO_KW_RE.finditer(code))
    if not matches:
        return None
    at = matches[-1].end()
    while at < len(statement) and statement[at] in " \t\r\n":
        at += 1
    lit = _COPY_LITERAL_RE.match(statement, at)
    if not lit:
        return None
    raw = lit.group(0)
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw[1:-1].replace('""', '"')


def _insert_payload_start(statement: str) -> int:
    """Offset where an INSERT's payload begins (after target + column list)."""
    m = _INSERT_RE.match(statement)
    if not m:
        return -1
    rest = statement[m.end() :]
    col_list = re.match(r"\s*\((?:[^)(]|\([^)]*\))*\)", rest)
    if col_list:
        rest = rest[col_list.end() :]
    return len(statement) - len(rest)


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

#: Length of the human-recognizable normalized-slug segment (before the
#: collision digest). 48 + 1 join char + 12 digest hex chars = 61, under the
#: 64-char cap with headroom.
_SLUG_PREFIX_LEN = 48

#: The collision-proofing digest: first 12 hex chars of sha256 over the RAW
#: (pre-normalization) subject — twelve hex chars, so two subjects that
#: normalize to the same slug (`Alice.` vs `alice`, `Alice Smith` vs
#: `alice-smith`, two subjects sharing a 64-char prefix) can never share a
#: scratch namespace. Twelve hex chars = 48 bits; the birthday-collision
#: probability among n subjects is ~n^2/2^49 — negligible for any real user
#: population, and a collision additionally requires an identical normalized
#: prefix too.
_SLUG_DIGEST_LEN = 12


def _slug_digest(raw_subject: str) -> str:
    """sha256 over the RAW (pre-normalization) subject, first 12 hex chars.

    The digest is what makes the slug collision-proof: subjects that
    normalize identically still hash differently. Pure and deterministic —
    the same subject always yields the same slug.
    """
    return hashlib.sha256(raw_subject.encode("utf-8")).hexdigest()[:_SLUG_DIGEST_LEN]


def subject_slug(caller) -> str | None:
    """The caller's scratch-namespace slug: their ATTRIBUTED subject.

    Only a resolved subject qualifies (relay attribution or the trusted
    browser rung — identity.py's ladder). A key-fingerprint caller is NOT
    slug-eligible: the fingerprint is a pseudonym that rotates with key
    rotation, and the review's rule is "anonymous keys get NO write
    capability, ever" — a rotating root would strand scratch data the next
    day. Returns None for everything that must not write.

    Collision-proof shape: ``<normalized[:48]>_<sha256(raw)[:12]>`` (61
    chars max). The normalized segment (lowercased, non-``[a-z0-9._-]``
    runs folded to ``-``) keeps it human-recognizable; the digest is taken
    over the RAW pre-normalization subject, so distinct subjects can never
    collapse onto one namespace however similar their normalized forms are
    (``Alice.`` vs ``alice``, ``Alice Smith`` vs ``alice-smith``, two
    subjects sharing a 64-char prefix).

    The join is ``_`` (not ``-``) because the engine surfaces a scratch
    namespace to DuckDB as the ``<slug>_<name>`` qualified identifier: a
    hyphen there would need quoting, and the ``con.register`` path stores
    the quoted form literally — the documented ``FROM <slug>_<name>``
    read-back would break for every subject (verified against the current
    engine; a `_safe_ident`/register fix is engine-owned, not writes.py's).

    Stable per subject (a pure function of the subject string), so a
    caller's namespace does not drift between calls. Hygiene
    (``.strip("-.")``) runs on the FINAL string so neither segment can
    leave a leading/trailing ``-``/``.``.
    """
    if caller is None:
        return None
    subject = getattr(caller, "subject", None)
    if not subject:
        return None
    slug = _SLUG_RE.sub("-", subject.lower()).strip("-.")
    digest = _slug_digest(subject)
    final = f"{slug[:_SLUG_PREFIX_LEN]}_{digest}".strip("-.")
    return final[:64] or None


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
    if len(roots) > 1:
        # Only roots[0] is ever consulted, so a second allowlisted root is
        # DEAD CONFIG: an operator who adds one reasonably expects targets to
        # land in it, and would silently get root[0] instead (audit
        # 2026-10-16 P2). Refuse rather than lie about the allowlist.
        names = ", ".join(repr(n) for n, _ in roots)
        raise WriteError(
            f"Write refused: {WRITE_SCRATCH_ROOTS_ENV} lists {len(roots)} roots "
            f"({names}) but the write tier supports exactly ONE scratch root "
            "(multi-root target selection is not implemented, and silently "
            "using the first would ignore the rest of the allowlist). "
            "Configure a single root.",
            code="E_WRITE_MULTI_ROOT",
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
    # subject namespace. ``a.b`` -> <subject>/a/b.
    #
    # Two separators are folded, and BOTH are checked for traversal before
    # any path is built (audit 2026-10-16 P2):
    #
    # * ``.`` — the logical-name separator;
    # * `/` — which only ever reaches here through a QUOTED identifier
    #   (``"a/b"``). Unchecked, one identifier produced two path levels
    #   (``"a/b"`` -> <subject>/a/b) and ``"x/.."`` aliased the parent.
    #
    # A leading ``/`` (or a ``\\``) would make the joined path ABSOLUTE and
    # discard the subject prefix entirely, so absolute shapes refuse first.
    if logical.startswith(("/", "\\")):
        raise WriteError(
            f"Write refused: target {target!r} is an absolute path; write targets "
            "must be a name under your scratch namespace.",
            code="E_WRITE_TARGET",
        )
    rel_parts: list[str] = []
    for segment in logical.split("."):
        for part in segment.split("/"):
            if part in ("", ".", ".."):
                raise WriteError(
                    f"Write refused: target {target!r} must be a name under your scratch "
                    "namespace (path traversal is not a target).",
                    code="E_WRITE_TARGET",
                )
            if "\x00" in part or "\\" in part:
                raise WriteError(
                    f"Write refused: target {target!r} contains an illegal path character.",
                    code="E_WRITE_TARGET",
                )
            rel_parts.append(part)
    if not rel_parts:
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
        return StatementClass(CLASS_WRITE_OTHER, statement, "PRAGMA", reason="PRAGMA statements are not allowed")
    if stmt_type == "SELECT":
        return StatementClass(CLASS_READ, statement, stmt_type)
    if stmt_type == "EXPLAIN":
        # EXPLAIN of a SELECT is a read (sqlguard's verified rule); EXPLAIN
        # ANALYZE of a write executes it — the inner parse decides.
        inner = _explain_inner_sql(statement)
        try:
            inner_spans = extract_statement_spans(inner)
        except ValueError:
            return StatementClass(CLASS_WRITE_OTHER, statement, stmt_type, reason="EXPLAIN of an unparseable statement")
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
            # Structural decision, on the CODE-ONLY view: the payload must
            # actually START a query. A bare \bSELECT\b search over the raw
            # span let a literal or comment containing the word decide the
            # classification (audit 2026-10-16 P2: "INSERT INTO t
            # VALUES ('SELECT')" was admitted as a scratch write).
            payload_at = _insert_payload_start(statement)
            payload_code = _code_only(statement[payload_at:]) if payload_at >= 0 else ""
            if _INSERT_NON_QUERY_RE.match(payload_code) or not _INSERT_SELECT_RE.search(payload_code):
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
            # The destination is the LAST top-level TO in the code-only view,
            # never a TO inside a comment or an inner string (audit
            # 2026-10-16 P2: a comment decoy decided the allowlisted path
            # while the real destination was unvalidated).
            target = _copy_to_target(statement)
            if not target:
                return StatementClass(
                    CLASS_WRITE_OTHER,
                    statement,
                    stmt_type,
                    reason="COPY without a TO target",
                )
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
        return _copy_to_target(statement)
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
            # ``StatementClass`` is a FROZEN dataclass, so the resolved plan
            # rides the instance through object.__setattr__ -- exactly like
            # assert_writable_sql below. Plain attribute assignment here
            # raised FrozenInstanceError on EVERY call, so this public entry
            # point could never return a plan (audit 2026-10-16 P0-3).
            object.__setattr__(c, "write_backend", backend)
            object.__setattr__(c, "write_path", canonical)
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


def _local_lease_path(path: str) -> str:
    """The lease filename for a LOCAL scratch path; refuse anything else.

    A scratch root may be authored as a URI (``s3://bucket/scratch``,
    ``iceberg://name=...``), but the advisory lease is a POSIX
    ``O_EXCL`` create on a shared filesystem. Joining a URI onto a local
    filename produced a literal RELATIVE directory tree named ``s3:`` in
    the process CWD and silently degraded cross-replica mutual exclusion to
    a per-pod file (audit 2026-10-16 P1). Refuse loudly instead: a URI root
    needs an object-store lease, which is not implemented.
    """
    text = str(path or "").strip()
    if not text:
        raise WriteError(
            "Write refused: empty scratch target cannot carry a write lease.",
            code="E_WRITE_TARGET",
        )
    if "://" in text:
        scheme = text.split("://", 1)[0]
        raise WriteError(
            f"Write refused: scratch root {text!r} is a {scheme}:// URI. The "
            "advisory write lease is a local O_EXCL create on the scratch "
            "filesystem, so a URI root cannot be guarded against concurrent "
            "writers (a catalog-less Delta writer without the lease can "
            "corrupt the target). Point "
            f"{WRITE_SCRATCH_ROOTS_ENV} at a local/shared POSIX path "
            "(a PVC mount), or omit the write tier for this root.",
            code="E_WRITE_LEASE_URI",
        )
    if text.startswith("\\") or (len(text) > 1 and text[1] == ":"):
        raise WriteError(
            f"Write refused: scratch target {text!r} is not a POSIX path "
            "(a UNC/drive path cannot host the O_EXCL lease).",
            code="E_WRITE_LEASE_URI",
        )
    return text.rstrip("/") + ".write-lease"


class WriteLease:
    """Advisory cross-replica lease: O_EXCL create + TTL on the scratch PVC.

    ``acquire`` creates ``<target>.write-lease`` with O_EXCL (atomic on
    POSIX + every RWX filesystem). Contention raises :class:`WriteError`
    with the RETRYABLE ``E_WRITE_CONFLICT`` code and the holder's age, so
    an agent can decide to wait.

    **Ownership.** Every lease carries a per-acquire random ``token`` (plus
    the holder pid and acquisition time). ``release`` unlinks ONLY a lease
    whose on-disk token still matches ours, so a writer whose lease was
    legitimately broken as stale can never delete its successor's lease —
    without the check, A's late ``release`` unlinked B's live lease and a
    third writer walked straight in (audit 2026-10-16 P0-6). The unlink is
    also performed on a verified file DESCRIPTOR rather than a path.

    **Staleness.** A lease is broken only when it is past its TTL *and* (on
    the same host) the recorded holder pid is no longer alive, so a
    still-running writer is never displaced. A lease whose payload is
    missing or unreadable is NOT broken (we cannot prove the holder is
    dead; a transient read error must not hand the target to a second
    writer).
    """

    #: How many times ``acquire`` will break a stale lease and retry before
    #: giving up. Bounded so a pathological break/steal loop cannot spin.
    MAX_ACQUIRE_ATTEMPTS = 5

    def __init__(self, path: str, ttl: int | None = None):
        self.path = path
        self.lease_path = _local_lease_path(path)
        self.ttl = ttl if ttl is not None else lease_ttl()
        self.token = uuid.uuid4().hex
        self._held = False

    # -- payload ---------------------------------------------------------
    def _payload(self) -> str:
        import json as _json

        return _json.dumps(
            {
                "token": self.token,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "acquired": time.time(),
                "ttl": self.ttl,
            }
        )

    def _read_holder(self) -> dict | None:
        """The current on-disk holder payload, or None when unreadable."""
        import json as _json

        try:
            with open(self.lease_path, encoding="utf-8") as fh:
                held = _json.load(fh)
        except Exception:
            return None
        return held if isinstance(held, dict) else None

    def acquire(self) -> None:
        # The parent dir may not exist yet (first write to this namespace) —
        # create it (parents included) before the O_EXCL create. A mkdir
        # race is benign (exist_ok), and an unwritable root fails here with
        # the filesystem's own error (fail closed, no silent skip).
        try:
            os.makedirs(os.path.dirname(self.lease_path) or ".", exist_ok=True)
        except OSError as exc:
            raise WriteError(
                f"Write refused: scratch directory {os.path.dirname(self.lease_path)!r} is not writable ({exc}).",
                code="E_WRITE_TARGET",
            ) from exc
        for _attempt in range(self.MAX_ACQUIRE_ATTEMPTS):
            try:
                fd = os.open(self.lease_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._break_stale():
                    continue  # the stale holder is gone: retry the create
                held = self._read_holder()
                age = time.time() - float(held.get("acquired", 0)) if held else None
                age_note = f"held {age:.0f}s" if age is not None else "holder unknown"
                raise WriteError(
                    f"Write conflict: another writer holds the lease on {self.path} "
                    f"({age_note}, TTL {self.ttl}s). Retry after it completes.",
                    code=E_WRITE_CONFLICT,
                    retryable=True,
                ) from None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._payload())
            self._held = True
            return
        raise WriteError(
            f"Write conflict: could not acquire the lease on {self.path} after "
            f"{self.MAX_ACQUIRE_ATTEMPTS} attempts (another writer keeps "
            "winning the race). Retry after it completes.",
            code=E_WRITE_CONFLICT,
            retryable=True,
        )

    def _holder_is_alive(self, held: dict) -> bool:
        """Whether the recorded holder is provably still running.

        Only answerable for a same-host holder (``pid`` + ``host`` in the
        payload): a foreign-host holder is treated as ALIVE, because we
        cannot prove otherwise and a wrong "dead" verdict double-writes.
        """
        pid = held.get("pid")
        host = held.get("host")
        if not isinstance(pid, int) or pid <= 0:
            return True  # unknown shape: assume alive (fail safe)
        if host and host != socket.gethostname():
            return True  # another pod's pid tells us nothing
        if pid == os.getpid():
            return True  # our own process (a nested/overlapping acquire)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False  # provably dead
        except PermissionError:
            return True  # exists, owned by another user
        except OSError:
            return True
        return True

    def _break_stale(self) -> bool:
        """Break a lease that is BOTH past its TTL and provably unheld.

        Never breaks a lease whose holder is alive (a long write must not be
        displaced by the TTL) and never breaks one whose payload cannot be
        read (an unreadable payload is not evidence of a dead holder).
        """
        held = self._read_holder()
        if held is None:
            return False
        try:
            age = time.time() - float(held.get("acquired", 0))
        except (TypeError, ValueError):
            return False
        if age <= self.ttl:
            return False
        if self._holder_is_alive(held):
            return False
        # Unlink by descriptor identity: only remove the file whose content
        # is still the payload we just judged stale (a concurrent release +
        # re-acquire must not lose its lease).
        try:
            st = os.stat(self.lease_path)
        except FileNotFoundError:
            return True  # raced release: retry the create
        except OSError:
            return False
        current = self._read_holder()
        if current is None or current.get("token") != held.get("token"):
            return True  # changed under us: retry the create without unlinking
        try:
            if os.stat(self.lease_path).st_ino != st.st_ino:
                return True
            os.unlink(self.lease_path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        logger.warning(
            "broke stale write lease %s (age %.0fs > TTL %ss, holder pid %s gone)",
            self.lease_path,
            age,
            self.ttl,
            held.get("pid"),
        )
        return True

    def release(self) -> None:
        """Unlink our own lease — only while our token is still on disk."""
        if not self._held:
            return
        self._held = False
        held = self._read_holder()
        if held is None or held.get("token") != self.token:
            # Our lease was broken as stale and (possibly) re-acquired by
            # another writer. Deleting it now would hand the target to a
            # third writer -- exactly the double-writer this lease prevents.
            logger.warning(
                "write lease %s is no longer ours (token mismatch); leaving it alone",
                self.lease_path,
            )
            return
        try:
            os.unlink(self.lease_path)
        except FileNotFoundError:
            pass
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
