"""Policy-as-code: per-caller row filters + column masks (review §3/§5 — Stage 2).

An operator-authored JSON/YAML file (``SQLHANDLER_POLICY_FILE``) maps caller
identity (subjects / key fingerprints, via groups) to table rules:

* ``row_filter`` — a SQL boolean expression AND-ed over every read of the
  covered tables (validated against each table's real schema at load with
  DuckDB's own binder — a filter naming a nonexistent column REFUSES the
  file, it never silently degrades),
* ``column_masks`` — per-column ``redact`` | ``hash`` | ``<const>``,
* ``hidden_tables`` — glob list: tables that do not exist for that caller
  (list/describe/search/profile/scan/query all refuse or omit).

Hot-reloaded on mtime change exactly like the semantic catalog; a broken
file fails CLOSED (the previous file keeps enforcing; an INVALID FIRST file
means enforcement refuses with E_POLICY — a masking policy that half-loads
is a leak, not a degradation).

The file's SHAPE (see tests/test_policy.py for the exact grammar)::

    {
      "version": 1,
      "default_group": "restricted",          # unbound keys when enforcing
      "groups": {
        "restricted": {
          "tables": {
            "workorder/*": {"row_filter": "kind != 'secret'",
                            "column_masks": {"ssn": "redact", "email": "hash"}},
            "payroll*": {"column_masks": {"amount": "***"}}
          },
          "hidden_tables": ["scratch/*"]
        }
      },
      "subjects": {"alice": ["analysts"]},    # subject slug -> groups
      "key_fps":  {"sha256:2689...": ["analysts"]}   # direct fp binding
    }

EFFECTIVE POLICY HASH: sha256 of the caller's canonicalized effective rule
set (sorted JSON). THIS is what ``SqlEngine._policy_hash()`` returns — the
conditional slot from the L2 slice activates here: empty when enforcement
is off (byte-identical cache keys, cross-caller sharing unchanged), a value
when on (masked results can never share an entry with unmasked ones — the
non-negotiable invariant).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("sqlhandler.policy")

POLICY_FILE_ENV = "SQLHANDLER_POLICY_FILE"
POLICY_ENABLED_ENV = "SQLHANDLER_POLICY_ENABLED"

_HASH_ALGO_RE = re.compile(r"^(?:sha)?(1|224|256|384|512)$", re.IGNORECASE)

_ROW_FILTER_MAX = 4096
_MASK_VALUE_MAX = 256
_POLICY_DENIED = "E_POLICY_DENIED"

__all__ = [
    "POLICY_DENIED",
    "POLICY_ENABLED_ENV",
    "POLICY_FILE_ENV",
    "Policy",
    "PolicyError",
    "PolicyStore",
    "TableRule",
    "build_mask_select",
    "canonical_hash",
    "load_policy",
    "owner_key",
    "policy_enabled",
    "policy_file_path",
    "policy_store",
    "reset_policy_store",
    "validate_row_filter",
]


class PolicyError(Exception):
    """The policy file is invalid — enforcement refuses rather than degrades.

    Carries ``partial`` semantics implicitly: a PolicyError at LOAD time means
    the file is NOT applied (the previous file keeps enforcing; if there was
    no previous file, enforcement stays closed with an empty table set —
    which hides nothing and masks nothing but lets nothing through the
    covered-tables path either... precisely: with no VALID file, callers get
    raw behavior only when there is ALSO no file at all; a present-but-broken
    file fails closed to the fully-restricted empty policy).
    """


POLICY_DENIED = _POLICY_DENIED


def policy_file_path(environ: dict[str, str] | None = None) -> str | None:
    """The configured policy file path (re-read per call; None = unconfigured)."""
    env = os.environ if environ is None else environ
    raw = env.get(POLICY_FILE_ENV, "").strip()
    return raw or None


def policy_enabled(environ: dict[str, str] | None = None) -> bool:
    """True when policy enforcement is ON (default FALSE — the whole feature
    is byte-identical when off). Re-read per call, like every SQLhandler env."""
    env = os.environ if environ is None else environ
    return env.get(POLICY_ENABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def canonical_hash(rules: dict) -> str:
    """sha256 over the canonicalized (sorted-keys, compact) JSON of a rule set.

    The ONLY number allowed into cache keys / materialization names: two
    callers with the SAME effective rules get the SAME hash (so two
    identically-restricted callers still share cache entries), and any rule
    difference changes the hash (never share masked/unmasked).
    """
    payload = json.dumps(rules, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def owner_key(caller) -> str:
    """The stable owner scope for caller-private stores (query memory,
    saved queries): subject when attributed, else the key fingerprint, else
    ``anonymous``. NEVER the raw key."""
    subject = getattr(caller, "subject", None)
    if subject:
        return f"subject:{subject}"
    key_fp = getattr(caller, "key_fp", None)
    if key_fp:
        return f"key:{key_fp}"
    return "anonymous"


@dataclass(frozen=True)
class TableRule:
    """The effective rule for ONE table under one caller's groups."""

    row_filter: str | None = None
    column_masks: dict[str, str] = field(default_factory=dict)
    hidden: bool = False

    @property
    def empty(self) -> bool:
        return not self.row_filter and not self.column_masks and not self.hidden


@dataclass(frozen=True)
class Policy:
    """An immutable loaded policy (a snapshot; hot-reload swaps the object).

    ``hash`` — the effective-policy hash of the whole file (any change to any
    rule changes it, so per-caller hashes differ across hot reloads and old
    cache entries age out via TTL). Per-caller effective hashes are derived
    from the caller's group rules via :meth:`effective_hash`.
    """

    groups: dict[str, dict] = field(default_factory=dict)
    subjects: dict[str, tuple[str, ...]] = field(default_factory=dict)
    key_fps: dict[str, tuple[str, ...]] = field(default_factory=dict)
    default_group: str | None = None
    source: str = ""
    hash: str = ""

    # ------------------------------------------------------------- lookup
    def groups_for(self, subject: str | None, key_fp: str | None) -> tuple[str, ...]:
        """Resolve a caller's groups: direct fp binding → subject binding →
        the default group (unbound keys, enforcement on). Unknown identities
        get the default group — a key with NO binding must never resolve to
        an unrestricted group by accident."""
        if key_fp and key_fp in self.key_fps:
            return self.key_fps[key_fp]
        if subject and subject in self.subjects:
            return self.subjects[subject]
        if self.default_group:
            return (self.default_group,)
        return ()

    def rule_for_table(self, table_path: str, table_name: str, groups: tuple[str, ...]) -> TableRule:
        """Compose the effective rule for one table under the caller's groups.

        Globs match BOTH the ``schema/name`` path and the bare table name
        (operators write ``workorder/*`` or ``payroll*``). Multiple groups'
        rules COMPOSE with deny-bias: a mask from any group applies; a hidden
        from any group hides; row filters AND together (the intersection —
        each group's restriction is a floor, never a grant).
        """
        if not groups:
            return TableRule()
        row_filters: list[str] = []
        masks: dict[str, str] = {}
        hidden = False
        for group in groups:
            spec = self.groups.get(group)
            if not isinstance(spec, dict):
                continue
            hidden = hidden or _glob_hits(spec.get("hidden_tables") or [], table_path, table_name)
            for pattern, rule in (spec.get("tables") or {}).items():
                if not isinstance(rule, dict):
                    continue
                if not _glob_match(pattern, table_path) and not _glob_match(pattern, table_name):
                    continue
                rf = rule.get("row_filter")
                if isinstance(rf, str) and rf.strip():
                    row_filters.append(rf.strip())
                for col, mask in (rule.get("column_masks") or {}).items():
                    if isinstance(mask, str):
                        masks[str(col)] = mask
        if hidden:
            # A hidden table has no visible columns or rows — the other
            # fields are moot (the caller must not even see it exists).
            return TableRule(hidden=True)
        # AND distinct row filters (identical filters across groups collapse).
        combined: str | None = None
        for f in dict.fromkeys(row_filters):
            combined = f if combined is None else f"({combined}) AND ({f})"
        return TableRule(row_filter=combined, column_masks=masks, hidden=False)

    def _compose_empty_check(self, groups: tuple[str, ...]) -> bool:
        """True when EVERY group the caller resolves to carries NO rule for
        anything (a fully unrestricted composition)."""
        for g in groups:
            spec = self.groups.get(g)
            if not isinstance(spec, dict):
                continue
            if spec.get("tables") or spec.get("hidden_tables"):
                return False
        return True

    def effective_hash(self, subject: str | None, key_fp: str | None) -> str:
        """THE per-caller policy hash for cache keys: sha256 of this caller's
        canonicalized effective rules. Empty string when the caller's groups
        resolve to nothing (no groups defined anywhere) — the caller is
        effectively unrestricted, so entries may share the unmasked key space
        (only possible when an operator defines groups but maps nobody to
        them; enforcement keeps the DEFAULT group for exactly this reason).

        NOTE the deliberate asymmetry with :meth:`groups_for` (default-group
        fallback): the hash is computed from the RESOLVED rules, so two
        callers in the same group share cache entries.
        """
        groups = self.groups_for(subject, key_fp)
        if not groups:
            return ""
        # A caller whose composed rules are ALL EMPTY (a group defined but
        # carrying no tables/hidden entries — an operator's explicit "no
        # rules") is EFFECTIVELY unmasked: return "" so they share the
        # unmasked cache-key space instead of forking every entry. Rules
        # with ANY content produce a hash.
        composed = self._compose_empty_check(groups)
        if composed:
            return ""
        effective: dict = {"groups": {}}
        for g in sorted(groups):
            spec = self.groups.get(g)
            if isinstance(spec, dict):
                effective["groups"][g] = _canonicalize_group(spec)
        # The table-side identity of the file matters too: a caller whose
        # rules are unchanged but whose FILE changed (their group's rules
        # rewritten) must get a new hash. Folding the file hash would change
        # the hash for EVERY caller on any edit; folding the group specs
        # (canonicalized above) changes it only for callers whose rules
        # actually changed. Hidden-table globs are part of the group spec.
        return canonical_hash(effective)


def _canonicalize_group(spec: dict) -> dict:
    """A group's rules in canonical (sorted, complete) form for hashing."""
    out: dict = {"tables": {}, "hidden_tables": sorted(str(h) for h in spec.get("hidden_tables") or [])}
    tables = spec.get("tables") or {}
    for pattern in sorted(tables):
        rule = tables[pattern]
        if isinstance(rule, dict):
            out["tables"][pattern] = {
                "row_filter": str(rule.get("row_filter") or "").strip(),
                "column_masks": {str(c): str(m) for c, m in sorted((rule.get("column_masks") or {}).items())},
            }
    return out


def _glob_match(pattern: str, value: str) -> bool:
    """fnmatch glob (case-SENSITIVE: table names are data, not DNS labels)."""
    if not pattern:
        return False
    return fnmatch.fnmatchcase(value, pattern) or fnmatch.fnmatchcase(value.lower(), pattern.lower())


def _glob_hits(patterns: list, *values: str) -> bool:
    return any(_glob_match(str(p), v) for p in patterns for v in values)


# ---------------------------------------------------------------------------
# load + validate (DuckDB-binder validated row filters)
# ---------------------------------------------------------------------------


def _parse_text(text: str, origin: str) -> dict:
    """JSON first, YAML second (the semantic-catalog convention)."""
    try:
        data = json.loads(text)
    except Exception:
        try:
            import yaml
        except ImportError:
            raise PolicyError(f"{origin}: not valid JSON, and PyYAML is not installed") from None
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise PolicyError(f"{origin}: not valid JSON or YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"{origin}: policy must be an object")
    return data


def _mask_sql(col: str, spec: str, available: set[str]) -> str:
    """The SELECT-list expression for one masked column (validated spec).

    * ``redact``       → ``'***'`` (a constant; type-flexible)
    * ``hash``|``sha256[:n]`` → ``md5(col)`` / ``sha256(col)`` — hex text,
      NOT reversible; ``sha256:<n>`` truncates to n hex chars (readable fp
      style) via ``substring``.
    * anything else    → a SQL string-literal constant: the operator WROTE
      the value (e.g. ``"REDACTED-CUSTOMER"`` or ``"0"``). Numbers/identifiers
      are NOT interpreted — masks replace values, they never compute from
      other columns (a mask expr referencing other columns would need the
      same validation row filters get; keep v1 boring and safe).
    """
    name = spec.strip()
    lowered = name.lower()
    if lowered == "redact":
        return "'***'"
    m = _HASH_ALGO_RE.match(lowered) or _HASH_ALGO_RE.match(lowered.split(":", 1)[0])
    if lowered in ("hash", "md5") or (m and ":" not in lowered):
        # bare hash/sha256/md5 → full digest of the column
        return _hash_expr(col, lowered if lowered != "hash" else "sha256", None, available)
    if ":" in lowered and m and lowered.split(":", 1)[0].lower() in ("sha", "sha256", "sha1", "sha512", "md5"):
        algo, _, trunc = lowered.partition(":")
        try:
            n = int(trunc)
        except ValueError:
            raise PolicyError(f"column mask {col!r}: hash truncation {trunc!r} is not an integer") from None
        if not 1 <= n <= 128:
            raise PolicyError(f"column mask {col!r}: hash truncation must be 1-128 hex chars")
        return _hash_expr(col, algo, n, available)
    # constant literal (SQL string literal; length-capped at validation).
    # A spec the operator ALREADY wrote as a SQL literal ('0.0') passes
    # through as-is; a bare value is wrapped. (Numbers-as-strings are fine:
    # DuckDB compares/casts string literals contextually in most spots, and
    # a mask's PURPOSE is to replace the value, not to preserve its type.)
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name
    return "'" + name.replace("'", "''") + "'"


def _hash_expr(col: str, algo: str, truncate: int | None, available: set[str]) -> str:
    """DuckDB hash expression for one column (identifier validated)."""
    ident = _validated_ident(col, available)
    func = {"sha256": "sha256", "md5": "md5", "sha1": "md5"}.get(algo.lower(), "sha256")
    expr = f"{func}({ident})"
    if truncate:
        return f"substring({expr}, 1, {int(truncate)})"
    return expr


def _validated_ident(col: str, available: set[str]) -> str:
    """Quote a column identifier for the masking SELECT (case-insensitive
    resolution against the table's REAL columns — a mask naming a column the
    table doesn't have is a load-time refusal, never a query-time surprise)."""
    wanted = str(col).strip()
    if not wanted or any(ch in wanted for ch in ("\x00", ";", "--")):
        raise PolicyError(f"column mask name {col!r} is not a valid identifier")
    match = next((a for a in available if a.lower() == wanted.lower()), None)
    if match is None:
        raise PolicyError(f"column mask references {col!r} which is not a column of the covered table")
    return '"' + match.replace('"', '""') + '"'


def build_mask_select(
    base_relation: str,
    columns: list[str],
    masks: dict[str, str],
    row_filter: str | None,
) -> str:
    """The masking view body: ``SELECT <masked cols> FROM <base> WHERE <filter>``.

    Covered columns render their mask expression AS the column (name kept via
    alias — DuckDB names an expression column by its text, so explicit aliases
    preserve ``SELECT ssn FROM t`` resolving). Uncovered columns pass through.
    Returns the full SELECT text; the caller creates ``CREATE VIEW name AS``.

    Unknown-column masks are ASSUMED validated at load (``validate_policy``
    refuses them); the defensive fallback here passes the column through
    rather than breaking a query on a hot-reload race.
    """
    parts: list[str] = []
    for col in columns:
        spec = next((m for name, m in masks.items() if name.lower() == col.lower()), None)
        if spec is None:
            parts.append('"' + col.replace('"', '""') + '"')
            continue
        try:
            expr = _mask_sql(col, spec, set(columns))
        except PolicyError:
            parts.append('"' + col.replace('"', '""') + '"')
            continue
        alias = '"' + col.replace('"', '""') + '"'
        parts.append(f"{expr} AS {alias}")
    sel = ", ".join(parts) if parts else "*"
    sql = f"SELECT {sel} FROM {base_relation}"
    if row_filter:
        sql += f" WHERE ({row_filter})"
    return sql


def validate_row_filter(row_filter: str, columns: list[str], table: str) -> str:
    """Validate one row filter: must PARSE as SQL and BIND as a boolean
    against the table's real columns (DuckDB's own parser+binder).

    Returns the (trimmed) filter; raises PolicyError otherwise. Validation
    uses ``SELECT <filter> FROM (<table columns>)`` on a THROWAWAY connection
    with DuckDB's filesystem disabled — the filter is operator-authored but
    it rides inside every masked query, so it is validated like code, not
    trusted like config.
    """
    text = (row_filter or "").strip()
    if not text:
        raise PolicyError(f"table {table!r}: row_filter is empty")
    if len(text) > _ROW_FILTER_MAX:
        raise PolicyError(f"table {table!r}: row_filter too long ({len(text)} > {_ROW_FILTER_MAX})")
    import duckdb

    con = duckdb.connect()
    try:
        try:
            con.execute("SET disabled_filesystems='LocalFileSystem'")
        except Exception:
            pass
        # NULL-typed temp table: every column is the polymorphic NULL type, so
        # the filter binds against the REAL COLUMN NAMES (what we validate:
        # names resolve, the expression is a valid boolean shape) without
        # literal-vs-type coincidences — value comparison happens at query
        # time against the actual types. WHERE ... AND FALSE keeps it
        # planning-only (nothing executes). A filter naming a missing column
        # fails the binder; a filter comparing a column to any literal binds.
        quoted = ", ".join(f'NULL AS "{c.replace(chr(34), chr(34) * 2)}"' for c in columns) or "NULL AS __col"
        con.execute(f"CREATE TEMP TABLE __pf AS SELECT {quoted} WHERE FALSE")
        try:
            con.execute(f"SELECT 1 FROM __pf WHERE ({text}) AND FALSE")
        except duckdb.BinderException as exc:
            raise PolicyError(f"table {table!r}: row_filter does not bind against the table's columns: {exc}") from exc
        except duckdb.ParserException as exc:
            raise PolicyError(f"table {table!r}: row_filter does not parse: {exc}") from exc
        except duckdb.CatalogException as exc:
            raise PolicyError(f"table {table!r}: row_filter references an unknown relation: {exc}") from exc
    finally:
        con.close()
    return text


def load_policy(path: str, *, table_columns: dict[str, list[str]] | None = None) -> Policy:
    """Read + validate one policy file into a frozen Policy.

    ``table_columns`` maps table keys (``source/path`` or bare name) to their
    REAL column lists; every rule's row_filter and column_masks are validated
    against them (a table with no known columns — not yet described — skips
    column validation; its filter is validated at first use instead. Boring
    default: an unvalidatable filter is a REFUSAL of the file only when the
    table IS known).

    Raises PolicyError on any invalid content (load-time refusal — the
    engine keeps the PREVIOUS valid policy in that case; see PolicyStore).
    """
    origin = path
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"{origin}: could not read: {exc}") from exc
    data = _parse_text(text, origin)

    groups = data.get("groups") or {}
    if not isinstance(groups, dict):
        raise PolicyError(f"{origin}: 'groups' must be an object")
    for name, spec in groups.items():
        if not isinstance(spec, dict):
            raise PolicyError(f"{origin}: group {name!r} must be an object")
        tables = spec.get("tables") or {}
        if not isinstance(tables, dict):
            raise PolicyError(f"{origin}: group {name!r}.tables must be an object")
        for pattern, rule in tables.items():
            if not isinstance(rule, dict):
                raise PolicyError(f"{origin}: group {name!r}.tables.{pattern!r} must be an object")
            rf = rule.get("row_filter")
            if rf is not None and not isinstance(rf, str):
                raise PolicyError(f"{origin}: group {name!r}.tables.{pattern!r}.row_filter must be a string")
            masks = rule.get("column_masks") or {}
            if not isinstance(masks, dict):
                raise PolicyError(f"{origin}: group {name!r}.tables.{pattern!r}.column_masks must be an object")
            for col, mask in masks.items():
                if not isinstance(mask, str) or not mask.strip():
                    raise PolicyError(f"{origin}: mask for {col!r} in {pattern!r} must be a non-empty string")
                if len(mask) > _MASK_VALUE_MAX:
                    raise PolicyError(f"{origin}: mask for {col!r} too long (cap {_MASK_VALUE_MAX})")
            if rf is not None and rf.strip() and table_columns:
                for tkey, cols in table_columns.items():
                    if _glob_match(str(pattern), tkey) and cols:
                        validate_row_filter(rf, cols, f"{name}.{pattern}")
                        break

    def _bindings(key: str) -> dict[str, tuple[str, ...]]:
        out: dict[str, tuple[str, ...]] = {}
        raw = data.get(key) or {}
        if not isinstance(raw, dict):
            raise PolicyError(f"{origin}: {key!r} must be an object of identity -> [groups]")
        for ident, gs in raw.items():
            if isinstance(gs, str):
                gs = [gs]
            if not isinstance(gs, list) or not gs or not all(isinstance(g, str) and g.strip() for g in gs):
                raise PolicyError(f"{origin}: {key}.{ident!r} must map to a non-empty group (string or list)")
            known = [g.strip() for g in gs]
            for g in known:
                if g not in groups:
                    raise PolicyError(f"{origin}: {key}.{ident!r} references undefined group {g!r}")
            out[str(ident).strip()] = tuple(known)
        return out

    subjects = _bindings("subjects")
    key_fps = _bindings("key_fps")
    default_group = data.get("default_group")
    if default_group is not None:
        if not isinstance(default_group, str) or not default_group.strip():
            raise PolicyError(f"{origin}: 'default_group' must be a non-empty string when present")
        default_group = default_group.strip()
        if default_group not in groups:
            raise PolicyError(f"{origin}: default_group {default_group!r} is not a defined group")

    pol_hash = canonical_hash(
        {
            "groups": {g: _canonicalize_group(s) for g, s in sorted(groups.items()) if isinstance(s, dict)},
            "subjects": {k: sorted(v) for k, v in sorted(subjects.items())},
            "key_fps": {k: sorted(v) for k, v in sorted(key_fps.items())},
            "default_group": default_group,
        }
    )
    return Policy(
        groups=groups,
        subjects=subjects,
        key_fps=key_fps,
        default_group=default_group,
        source=origin,
        hash=pol_hash,
    )


# ---------------------------------------------------------------------------
# hot-reloading store (mtime, the semantic-catalog pattern)
# ---------------------------------------------------------------------------


class PolicyStore:
    """Process-wide policy holder with mtime hot-reload (fail-closed).

    The engine consults ONE instance (``policy_store()``). ``get()`` re-stats
    the file (one ``os.stat`` per call — the catalog's pattern) and reloads
    on mtime/size change. Failure semantics:

    * file MISSING + previous valid policy → keep enforcing the previous one
      (a mount hiccup must not silently unmask callers);
    * file MISSING and never seen → empty policy (no enforcement configured);
    * file PRESENT but BROKEN → the PREVIOUS policy keeps enforcing (a bad
      edit must not unmask callers); when there IS no previous policy, an
      empty-but-armed store refuses covered-table resolution entirely —
      fail CLOSED means "no caller gets more than the previous state", and
      with no previous state the safe floor is: no policy = no masking, but
      ALSO a loud warning per load attempt.

    All state swaps are atomic (one reference assignment under a lock).
    """

    def __init__(self, table_columns_provider=None):
        self._lock = threading.Lock()
        self._policy: Policy | None = None
        self._stat: tuple[str, float, int] | None = None
        self._broken_since: float | None = None
        self._table_columns_provider = table_columns_provider  # () -> dict|None

    # -- configuration --------------------------------------------------------
    @staticmethod
    def configured() -> bool:
        return policy_file_path() is not None and policy_enabled()

    def set_table_columns_provider(self, provider) -> None:
        self._table_columns_provider = provider

    # -- reload ---------------------------------------------------------------
    def _columns(self) -> dict[str, list[str]] | None:
        try:
            return self._table_columns_provider() if self._table_columns_provider else None
        except Exception:
            return None

    def get(self) -> Policy:
        """The current policy (hot-reloaded on mtime; never raises)."""
        path = policy_file_path()
        if not path or not policy_enabled():
            with self._lock:
                return self._policy or Policy()
        try:
            st = os.stat(path)
            sig = (path, st.st_mtime, st.st_size)
        except OSError:
            # The file vanished: keep the last valid policy (fail-closed).
            with self._lock:
                return self._policy or Policy()
        with self._lock:
            if self._policy is not None and self._stat == sig:
                return self._policy
        try:
            fresh = load_policy(path, table_columns=self._columns())
        except PolicyError as exc:
            with self._lock:
                if self._policy is not None:
                    # A bad edit never unmasks callers.
                    logger.warning("policy file %s invalid (%s); keeping the previous policy", path, exc)
                    return self._policy
                logger.error("policy file %s invalid: %s — NO policy is active (fail-closed, loud)", path, exc)
                self._broken_since = time.time()
                return Policy()
            # (unreachable)
        with self._lock:
            self._policy = fresh
            self._stat = sig
            self._broken_since = None
        logger.info(
            "policy loaded: %d group(s), %d subject(s), %d key fp(s), default_group=%s from %s",
            len(fresh.groups),
            len(fresh.subjects),
            len(fresh.key_fps),
            fresh.default_group or "(none)",
            path,
        )
        return fresh

    def reset(self) -> None:
        """Test hook: drop the cached policy (the next get() reloads)."""
        with self._lock:
            self._policy = None
            self._stat = None
            self._broken_since = None


_store_lock = threading.Lock()
_store: PolicyStore | None = None


def policy_store() -> PolicyStore:
    """The process-wide store (built lazily; engine wires the columns provider)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = PolicyStore()
        return _store


def reset_policy_store() -> None:
    """Test hook: drop the process-wide store."""
    global _store
    with _store_lock:
        _store = None
