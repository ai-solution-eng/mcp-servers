"""Policy-as-code: per-caller row filters + column masks (review §3/§5 — Stage 2).

An operator-authored JSON/YAML file (``SQLHANDLER_POLICY_FILE``) maps caller
identity (subjects / key fingerprints, via groups) to table rules:

* ``row_filter`` — a SQL boolean expression AND-ed over every read of the
  covered tables (validated against each table's real schema at load with
  DuckDB's own binder — a filter naming a nonexistent column REFUSES the
  file, it never silently degrades),
* ``column_masks`` — per-column ``redact`` | ``hash`` | ``<const>``,
* ``hidden_tables`` — glob list: tables that do not exist for that caller
  (list/describe/search/profile/scan/query all refuse or omit),
* ``visible_tables`` — glob list, the ALLOW-list twin of ``hidden_tables``
  (the dataset-ACL vocabulary). Absent from EVERY group the caller resolves
  to ⇒ today's deny-list posture, byte-identical. Non-empty in ANY resolved
  group ⇒ default-DENY: a table is hidden unless some ``visible_tables``
  glob hits it — and an explicit ``hidden_tables`` hit always wins (a block
  is never undone by a grant; deny-bias over the allow-list too).

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

The OPTIONAL dataset-ACL document (keys + globs instead of per-table rules;
compiled onto the SAME group machinery — mutually exclusive with a
hand-written ``groups``)::

    {
      "datasets": {
        "global": ["workorder/*", "public_*"],   # everyone (default group)
        "assignments": {
          "alice":  ["payroll/*"],               # subject slug
          "sha256:2689ab12cd34": ["payroll/*"]   # key fp (bare fp spelling)
        },
        "blocked": ["scratch/*", "*_pii"]        # wins over every grant
      }
    }

``assignments`` keys bind EITHER a subject OR a key fingerprint (bare
``sha256:<12hex>`` — the mint-key spelling — or the self-documenting
``key:sha256:<12hex>``; both accepted, compiled to the bare fp binding).
Compilation is fail-closed: a malformed fp, a non-list/empty glob, or the
presence of BOTH ``datasets`` and ``groups`` refuses the file.

The OPTIONAL ``admins`` designation (the administration plane — orthogonal
to the access plane above)::

    "admins": ["alice", "sha256:2689ab12cd34", "key:sha256:2689ab12cd34"]

A list of subjects and/or key fingerprints naming who may ADMINISTER (the
``/api/admin/*`` routes and the MCP admin twins read it via
``sqlhandler.admin_keys.is_admin``). It composes with BOTH authoring
shapes — hand-written groups AND the compiled ``datasets`` document —
because designation says nothing about grants and grants say nothing about
designation; the ONLY mutual exclusion in the vocabulary remains
datasets-vs-groups. Entries use the same two fp spellings as
``assignments`` (the ``key:``-prefixed spelling normalizes to the bare fp)
and validation is fail-closed under the same conventions: a non-list, a
non-string/empty entry, or an fp-SHAPED entry that fails the real
fingerprint format refuses the file (a typo'd fp must never silently
become "just a weird subject name"). ``admins`` folds into the file hash
(any edit hot-reloads) and loads as ``Policy.admins`` (stripped subjects +
bare fps, insertion order kept).

EFFECTIVE POLICY HASH: sha256 of the caller's canonicalized effective rule
set (sorted JSON). THIS is what ``SqlEngine._policy_hash()`` returns — the
conditional slot from the L2 slice activates here: empty when enforcement
is off (byte-identical cache keys, cross-caller sharing unchanged), a value
when on (masked results can never share an entry with unmasked ones — the
non-negotiable invariant). The payload is the SORTED, DE-DUPLICATED list of
the resolved groups' canonical specs, with the group NAMES left out: the
effective rules are the composition of the specs, so callers whose rules are
identical share one hash (and one cache-key space) regardless of how the
operator labelled the groups. The ``datasets`` document is NOT folded into
this hash — the compiled specs already carry the caller's grants, and
folding the whole document would re-hash every caller on an unrelated edit.
It DOES fold into the FILE hash (``Policy.hash``, hot-reload detection),
and a ``visible_tables`` group is RESTRICTIVE: it never counts as "no
rules", so allow-listed callers always hash into their own cache-key space.
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("sqlhandler.policy")

POLICY_FILE_ENV = "SQLHANDLER_POLICY_FILE"
POLICY_ENABLED_ENV = "SQLHANDLER_POLICY_ENABLED"
#: The keying secret for hash column masks (env, re-read per call). SET =
#: sha256(secret || value) - a KEYED digest, so low-entropy values (SSN,
#: email, salary) cannot be enumerated/rainbow-tabled and the digest is not
#: comparable across deployments. UNSET = the plain digest (logged as one
#: warning at load: enumerable).
MASK_HASH_SECRET_ENV = "SQLHANDLER_MASK_HASH_SECRET"  # secret-scan:allow (env var NAME, not a secret)

#: Hash algorithms the mask grammar SUPPORTS - DuckDB functions that exist in
#: the pinned build. Anything else hash-SHAPED is a load-time refusal, never a
#: silent substitution (sha512 used to become sha256; sha1 used to become md5).
_MASK_HASH_ALGOS = ("sha256", "sha1", "md5")
_HASH_ALGO_RE = re.compile(r"^(?:sha)?(1|224|256|384|512)$", re.IGNORECASE)

_ROW_FILTER_MAX = 4096
_MASK_VALUE_MAX = 256
_POLICY_DENIED = "E_POLICY_DENIED"

__all__ = [
    "MASK_HASH_SECRET_ENV",
    "POLICY_DENIED",
    "POLICY_ENABLED_ENV",
    "POLICY_FILE_ENV",
    "Policy",
    "PolicyError",
    "PolicyStore",
    "TableRule",
    "build_mask_select",
    "canonical_hash",
    "dump_doc",
    "load_policy",
    "mask_hash_secret",
    "owner_key",
    "policy_enabled",
    "policy_file_path",
    "policy_health",
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
    # The dataset-ACL document as authored (canonicalized for hashing); None
    # when the file uses the hand-written groups shape. Enforcement NEVER
    # reads this — compiled groups + bindings above are the enforcement
    # surface; the field exists so hashing can fold the raw vocabulary and
    # callers can introspect what loaded.
    datasets: dict | None = None
    # The AUTHORED document as parsed (either form — datasets doc OR the
    # hand-written groups shape). The grants payload serves it so the UI
    # policy editor prefills for BOTH forms (the datasets field alone is
    # None for groups-form policies, which prefilled an empty editor — and
    # a Save of that empty editor wiped the file). Hash-neutral: hashing
    # folds the compiled fields, not this mirror.
    authored: dict | None = None
    # The admin DESIGNATION (administration plane, orthogonal to grants):
    # validated subjects + bare key fingerprints from the optional top-level
    # ``admins`` list, in file order. Empty tuple = no admins designated
    # (fail-closed: admin_keys.is_admin refuses everyone). Enforcement of
    # table access NEVER reads this — only the admin API/MCP gate does.
    admins: tuple[str, ...] = ()

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

        With the dataset-ACL vocabulary in play the deny-bias extends to the
        ALLOW side: if any resolved group carries a non-empty
        ``visible_tables``, a table is hidden unless some group's globs hit
        it — default-deny — while an explicit ``hidden_tables`` hit still
        hides (a block is never undone by a grant). No ``visible_tables``
        anywhere ⇒ the historical deny-list-only behavior, byte-identical.
        """
        if not groups:
            return TableRule()
        row_filters: list[str] = []
        masks: dict[str, str] = {}
        hidden = False
        visible = False
        has_vis = False
        for group in groups:
            spec = self.groups.get(group)
            if not isinstance(spec, dict):
                continue
            vis = spec.get("visible_tables") or []
            if isinstance(vis, list) and vis:
                # One allow-list anywhere in the resolution flips the whole
                # composition to default-deny (fail-closed across groups).
                has_vis = True
                visible = visible or _glob_hits(vis, table_path, table_name)
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
        if has_vis and not visible:
            hidden = True
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
        anything (a fully unrestricted composition).

        A group carrying ``visible_tables`` is RESTRICTIVE even with no other
        key: it hides everything the globs do not hit. Counting it as "no
        rules" would return "" from :meth:`effective_hash` and let an
        allow-listed caller share the UNMASKED cache-key space — a leak, so
        any visible_tables counts as content here."""
        for g in groups:
            spec = self.groups.get(g)
            if not isinstance(spec, dict):
                continue
            if spec.get("tables") or spec.get("hidden_tables") or spec.get("visible_tables"):
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

        The payload is the SORTED, DE-DUPLICATED list of the resolved groups'
        canonical specs — deliberately WITHOUT the group names. The effective
        rules are the composition of those specs (``rule_for_table`` never
        reads a name, and composing the same spec twice is idempotent), so
        callers whose rules are identical share one hash even when the
        operator named the groups differently, and the content-addressed
        ``datasets`` groups share between identities with identical grants.

        The ``datasets`` document itself is NOT folded in: the compiled group
        specs already carry this caller's global + assignment + blocked globs,
        and folding the whole document would re-hash EVERY caller on any
        unrelated assignment edit — a pure cache fork with no rule change.
        The FILE hash (``Policy.hash``) still folds it, so the store
        hot-reloads on any edit.
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
        specs = []
        for g in sorted(groups):
            spec = self.groups.get(g)
            if isinstance(spec, dict):
                specs.append(_canonicalize_group(spec))
        # Canonical JSON gives a deterministic order; the set drops duplicates
        # (identical rules under two group names compose identically). The
        # payload is the SPECS, not a {name: spec} map: names are labels, not
        # rules, and including them forked the cache for no security gain.
        canonical = sorted({json.dumps(s, sort_keys=True, separators=(",", ":"), default=str) for s in specs})
        payload: dict = {"groups": [json.loads(c) for c in canonical]}
        mask_key = _mask_key_digest()
        if mask_key:
            # A KEYED digest is a different value set: never share a cache
            # entry (or a virtual materialization) with the unkeyed output.
            payload["mask_key"] = mask_key
        return canonical_hash(payload)


# ---------------------------------------------------------------------------
# the BROKEN sentinel: a PRESENT but INVALID policy file, with no previous
# valid policy to keep enforcing
# ---------------------------------------------------------------------------
#
# Returning a plain Policy() for this state (the old behavior) made it
# indistinguishable from "no policy configured": an empty Policy hides
# nothing and masks nothing, so a typo or a truncated mount silently resolved
# every caller to UNRESTRICTED while the log claimed "fail-closed". The
# sentinel makes the two states behave differently AND observably:
#
#   * no file / no enforcement  -> Policy(), groups {} (unchanged);
#   * file present but invalid  -> _BROKEN_POLICY: every caller resolves to a
#     group whose visible_tables glob matches nothing, i.e. default-deny —
#     every covered table is HIDDEN until the file is fixed.
#
# It is a real Policy (frozen dataclass) so every downstream path — list,
# describe, search, profile, scan, query — applies its existing hidden-table
# handling with no special-casing. _BROKEN_NEVER cannot match a real table
# path or name, and a non-empty visible_tables counts as CONTENT in
# _compose_empty_check, so effective_hash is non-empty: broken-state entries
# get their own cache-key space rather than sharing the unmasked one.
_BROKEN_GROUP = "_policy_broken"
_BROKEN_NEVER = "__sqlhandler_policy_broken_never_matches__"

_BROKEN_POLICY = Policy(
    groups={_BROKEN_GROUP: {"visible_tables": [_BROKEN_NEVER]}},
    subjects={},
    key_fps={},
    default_group=_BROKEN_GROUP,
    source="<broken>",
    hash=canonical_hash({"policy_broken": True}),
)


def _canonicalize_group(spec: dict) -> dict:
    """A group's rules in canonical (sorted, complete) form for hashing."""
    out: dict = {
        "tables": {},
        "hidden_tables": sorted(str(h) for h in spec.get("hidden_tables") or []),
        "visible_tables": sorted(str(v) for v in spec.get("visible_tables") or []),
    }
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
    """fnmatch glob, matched BOTH exactly and case-INSENSITIVELY.

    The second clause is deliberate and load-bearing: table names reach this
    module from DuckDB (case-insensitive identifiers) and from object stores
    whose listing case may differ from the operator's file. An operator who
    writes ``visible_tables: ["public_*"]`` therefore also grants
    ``PUBLIC_PII`` - grants are CASE-INSENSITIVE (a deny-list is the safe
    direction: ``hidden_tables: ["Payroll*"]`` hides ``payroll``). This is
    pinned by tests/test_authz_audit_fixes.py; changing it to case-sensitive
    would silently REVOKE grants, so it is behaviour, not a bug. ``fnmatch``
    also lets ``*`` cross ``/`` (``workorder/*`` matches ``workorder/a/b``).
    """
    if not pattern:
        return False
    return fnmatch.fnmatchcase(value, pattern) or fnmatch.fnmatchcase(value.lower(), pattern.lower())


def _glob_hits(patterns: list, *values: str) -> bool:
    return any(_glob_match(str(p), v) for p in patterns for v in values)


# ---------------------------------------------------------------------------
# dataset-ACL document (keys + globs compiled onto the group machinery)
# ---------------------------------------------------------------------------

_ACL_GLOBAL_GROUP = "_acl_global"
_KEY_FP_RE = re.compile(r"^sha256:[0-9a-f]{12}$")  # audit.key_fingerprint's exact shape


def _acl_grants_group(globs: list[str]) -> str:
    """Content-addressed group name for one assignment's glob set.

    The name is derived from the CANONICAL glob list (sorted, de-duplicated),
    never from the assignment's position or identity. Two callers granted the
    same datasets therefore resolve to ONE group — and so share one
    effective-policy hash and one cache-key space — while an edit to an
    UNRELATED assignment no longer renames this one (the old
    ``_acl_id_<index>`` scheme shifted every later index on any insertion,
    silently forking every caller's cache keys).

    Blocked globs are NOT part of the name: they are appended to every
    group's spec, so a block edit changes the SPEC (and the hash) without
    renaming the group.
    """
    payload = json.dumps(sorted({str(g).strip() for g in globs}), separators=(",", ":"))
    return "_acl_grants_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _compile_datasets(data: dict, origin: str) -> tuple[dict, dict, dict, str, dict]:
    """Compile the ``datasets`` document onto the EXISTING group machinery.

    Returns ``(groups, subjects, key_fps, default_group, datasets_doc)`` in
    exactly the shapes ``load_policy``'s hand-written path produces, so the
    rest of the loader (validation, hashing, Policy) is shape-agnostic and
    enforcement never learns the second vocabulary existed.

    Layout (mutually exclusive with a hand-written ``groups`` — both present
    is a load-time refusal)::

        "datasets": {
          "global":     [globs],                       # -> group _acl_global
          "assignments": {"<subject>|sha256:<12hex>|key:sha256:<12hex>": [globs]},
          "blocked":    [globs]                        # -> hidden_tables EVERYWHERE
        }

    Fail-closed on purpose: every glob list must be a NON-EMPTY list of
    non-empty strings; every fp-shaped assignment key must match the real
    fingerprint format (``sha256:`` + 12 lowercase hex, the shape
    ``mcp_fleet_common.audit.key_fingerprint`` emits) and refuse the file
    otherwise; an assignment key that collides with another after fp
    normalization refuses too (the two spellings of one fp must not silently
    merge two different grants).
    """
    doc = data.get("datasets")
    if not isinstance(doc, dict):
        raise PolicyError(f"{origin}: 'datasets' must be an object")

    def _globs(value, where: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise PolicyError(f"{origin}: datasets.{where} must be a non-empty list of glob strings")
        out = []
        for g in value:
            if not isinstance(g, str) or not g.strip():
                raise PolicyError(f"{origin}: datasets.{where} contains a non-string or empty glob")
            out.append(g.strip())
        return out

    global_globs = _globs(doc.get("global") or [], "global")
    blocked_globs = _globs(doc.get("blocked") or [], "blocked") if doc.get("blocked") is not None else []
    raw_assignments = doc.get("assignments") or {}
    if not isinstance(raw_assignments, dict):
        raise PolicyError(f"{origin}: datasets.assignments must be an object of identity -> [globs]")

    generated: dict[str, dict] = {_ACL_GLOBAL_GROUP: {"visible_tables": sorted({str(g).strip() for g in global_globs})}}
    subjects: dict[str, tuple[str, ...]] = {}
    key_fps: dict[str, tuple[str, ...]] = {}
    seen_fps: dict[str, str] = {}
    seen_subjects: dict[str, str] = {}
    for raw_key, globs_raw in sorted(raw_assignments.items(), key=lambda kv: str(kv[0])):
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise PolicyError(f"{origin}: datasets.assignments keys must be non-empty strings")
        key = raw_key.strip()
        globs = _globs(globs_raw, f"assignments.{key!r}")
        # CONTENT-addressed, not index-addressed: identical grants share one
        # group (and therefore one cache-key space), and unrelated assignment
        # edits never rename an existing group.
        group = _acl_grants_group(globs)
        generated[group] = {"visible_tables": sorted({str(g).strip() for g in globs})}
        # Two accepted spellings, one binding: the minted bare form
        # ("sha256:<12hex>") and the self-documenting prefixed one. A key
        # that LOOKS like a fingerprint attempt (either prefix) but fails
        # the real format refuses the file — fail-closed, never "just a
        # weird subject name".
        fp_body = key.removeprefix("key:")
        fp_shaped = fp_body.startswith("sha256:") or key.startswith("key:sha256:")
        if fp_shaped:
            if not _KEY_FP_RE.match(fp_body):
                raise PolicyError(
                    f"{origin}: datasets.assignments key {key!r} looks like a key fingerprint but does not "
                    "match the real format 'sha256:' + 12 lowercase hex (audit.key_fingerprint)"
                )
            fp = fp_body
            if fp in seen_fps and seen_fps[fp] != key:
                raise PolicyError(
                    f"{origin}: datasets.assignments binds {fp!r} twice ({seen_fps[fp]!r} and {key!r} — "
                    "one fingerprint, one binding)"
                )
            seen_fps[fp] = key
            key_fps[fp] = (_ACL_GLOBAL_GROUP, group)
        else:
            if key in seen_subjects:
                raise PolicyError(
                    f"{origin}: datasets.assignments binds subject {key!r} twice "
                    f"({seen_subjects[key]!r} and {raw_key!r} — whitespace-normalized collision)"
                )
            seen_subjects[key] = raw_key
            subjects[key] = (_ACL_GLOBAL_GROUP, group)
    # Blocked wins over every grant: appended as hidden_tables to EVERY
    # generated group (deny-bias makes a block un-overridable by any grant,
    # including the global one).
    if blocked_globs:
        canonical_blocked = sorted({str(g).strip() for g in blocked_globs})
        for spec in generated.values():
            spec["hidden_tables"] = canonical_blocked
    return generated, subjects, key_fps, _ACL_GLOBAL_GROUP, doc


# ---------------------------------------------------------------------------
# admin designation (the optional top-level ``admins`` list)
# ---------------------------------------------------------------------------


def _validate_admins(data: dict, origin: str) -> tuple[str, ...]:
    """Validate the optional top-level ``admins`` list (the admin-designation
    vocabulary, orthogonal to grants: legal alongside hand-written groups AND
    the compiled ``datasets`` document — the ONLY mutual exclusion remains
    datasets-vs-groups).

    Returns the canonical tuple (stripped entries, insertion order kept,
    ``key:``-prefixed fps normalized to the bare fp). Fail-closed under the
    assignments' conventions: non-list, non-string/empty entries, and
    fp-SHAPED-but-malformed entries refuse the file.
    """
    raw = data.get("admins")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PolicyError(f"{origin}: 'admins' must be a list of subjects and/or key fingerprints")
    # An explicit empty list is legal and means "nobody administers" — the
    # fail-closed direction (refusing it would leave a previous policy's
    # admins enforcing while the operator is trying to un-designate them).
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise PolicyError(f"{origin}: 'admins' entries must be non-empty strings")
        e = entry.strip()
        # Same two spellings, same fp-shaped-but-malformed refusal as
        # datasets.assignments (the 2026-09-30 fail-closed rule): a value
        # that LOOKS like a fingerprint attempt must match the real format
        # or the file refuses — a typo'd fp never silently becomes a subject.
        fp_body = e.removeprefix("key:")
        if fp_body.startswith("sha256:") or e.startswith("key:sha256:"):
            if not _KEY_FP_RE.match(fp_body):
                raise PolicyError(
                    f"{origin}: 'admins' entry {e!r} looks like a key fingerprint but does not match the real "
                    "format 'sha256:' + 12 lowercase hex (audit.key_fingerprint)"
                )
            e = fp_body
        out.append(e)
    return tuple(out)


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


def dump_doc(data: dict, fmt: str = "yaml") -> str:
    """The inverse of :func:`_parse_text` — a policy document as editable text.

    YAML is the user-facing default (the admin UI's policy editor prefills
    from it); JSON is the one-format fallback when pyyaml is missing — the
    editor must always show text :func:`_parse_text` parses back. Same
    serializer conventions as the semantic catalog's editor
    (``engine._serialize_doc``): ``sort_keys=False`` keeps the operator's
    key order, ``allow_unicode`` keeps non-ASCII subjects readable,
    ``default_flow_style=False`` keeps it block-style (diff-friendly).
    """
    if fmt not in ("yaml", "json"):
        raise ValueError(f"format must be 'yaml' or 'json', got {fmt!r}")
    if fmt == "json":
        return json.dumps(data, indent=2, default=str) + "\n"
    try:
        import yaml
    except ImportError:
        return json.dumps(data, indent=2, default=str) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def mask_hash_secret(environ: dict[str, str] | None = None) -> str:
    """The keying secret for hash masks ('' when unset). Re-read per call."""
    env = os.environ if environ is None else environ
    return env.get(MASK_HASH_SECRET_ENV, "").strip()


def _mask_key_digest() -> str:
    """A non-secret digest of the mask key ('' when unkeyed).

    Folded into the effective-policy hash so enabling or ROTATING
    SQLHANDLER_MASK_HASH_SECRET moves every masked caller into a fresh
    cache-key space: a keyed digest must never be served from an entry (or a
    virtual materialization) computed unkeyed, or from another key's output.
    Empty when unset, so unkeyed/enforcement-off keys stay byte-identical.
    """
    secret = mask_hash_secret()
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16] if secret else ""


def _mask_sql(col: str, spec: str, available: set[str]) -> str:
    """The SELECT-list expression for one masked column (validated spec).

    Grammar (ONE explicit language - anything hash-SHAPED but unsupported is a
    refusal, never a silent substitution):

    * ``redact``          -> ``'***'`` (a constant; type-flexible)
    * ``hash``            -> sha256 of the value, KEYED when
      SQLHANDLER_MASK_HASH_SECRET is set
    * ``sha256`` | ``sha1`` | ``md5`` -> that digest of the value
    * ``<algo>:<n>``      -> the digest truncated to n hex chars (1-128)
      via ``substring``
    * anything else       -> a SQL string-literal constant: the operator
      WROTE the value (e.g. ``"REDACTED-CUSTOMER"`` or ``"0"``). Numbers and
      identifiers are NOT interpreted - masks replace values, they never
      compute from other columns.

    A hash mask is NOT reversible, but an UNKEYED digest of a low-entropy
    value (an SSN, an email, a salary) is enumerable and preserves equality
    across rows and deployments - setting the key closes that (audit
    2026-11-12 P2).
    """
    name = spec.strip()
    lowered = name.lower()
    if lowered == "redact":
        return "'***'"
    algo, sep, trunc = lowered.partition(":")
    if algo == "hash":
        algo = "sha256"
    if algo in _MASK_HASH_ALGOS:
        if sep:
            try:
                n = int(trunc)
            except ValueError:
                raise PolicyError(f"column mask {col!r}: hash truncation {trunc!r} is not an integer") from None
            if not 1 <= n <= 128:
                raise PolicyError(f"column mask {col!r}: hash truncation must be 1-128 hex chars")
            return _hash_expr(col, algo, n, available)
        return _hash_expr(col, algo, None, available)
    if _HASH_ALGO_RE.match(lowered) or algo in ("sha", "sha224", "sha384", "sha512"):
        raise PolicyError(
            f"column mask {col!r}: {spec!r} is a hash spec with an unsupported algorithm "
            f"(supported: {', '.join(_MASK_HASH_ALGOS)}; name the algorithm, not a digest length)"
        )
    # constant literal (SQL string literal; length-capped at validation).
    # A spec the operator ALREADY wrote as a SQL literal ('0.0') passes
    # through as-is; a bare value is wrapped. (Numbers-as-strings are fine:
    # DuckDB compares/casts string literals contextually in most spots, and
    # a mask's PURPOSE is to replace the value, not to preserve its type.)
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name
    return "'" + name.replace("'", "''") + "'"


def _hash_expr(col: str, algo: str, truncate: int | None, available: set[str]) -> str:
    """DuckDB hash expression for one column (identifier validated).

    KEYED when SQLHANDLER_MASK_HASH_SECRET is set: ``sha256(value || secret)``
    (the ``||`` operator, not ``concat`` - it keeps NULL-propagating
    semantics). The key rides the generated masking VIEW body, which exists
    only on the per-query connection and is never logged (the debug line logs
    the SPEC, not the SQL).

    TYPING NOTE (cross-validation 2026-11-12): the keyed form CASTs to
    VARCHAR, so an int column hashes its TEXT form; the unkeyed form hashes
    DuckDB's native binary value. The two spaces never mix - Policy
    .effective_hash folds the key digest in, so a keyed and an unkeyed result
    cannot share a cache entry - and changing the unkeyed form would silently
    change every existing digest for a non-string column, so the asymmetry is
    documented rather than "fixed".
    """
    ident = _validated_ident(col, available)
    if algo.lower() not in _MASK_HASH_ALGOS:  # _mask_sql refuses these first
        raise PolicyError(f"column mask {col!r}: unsupported hash algorithm {algo!r}")
    func = algo.lower()
    secret = mask_hash_secret()
    if secret:
        literal = "'" + secret.replace("'", "''") + "'"
        expr = f"{func}(CAST({ident} AS VARCHAR) || {literal})"
    else:
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


def _compile_mask_spec(
    origin: str, pattern: str, col: str, spec: str, table_columns: dict[str, list[str]] | None
) -> str:
    """Compile ONE mask spec at LOAD time; raise PolicyError when it cannot be.

    The primary fix for audit 2026-11-12 P1-3: a mask spec is code, and a spec
    that does not compile used to load cleanly and then silently serve the RAW
    column (sha256:12x / sha256:0). Two checks, deliberately separate:

    * the SPEC ITSELF must compile - _mask_sql is run against the mask's own
      column name so a column-existence problem can never mask a malformed
      spec. This needs no catalog, so it runs for every mask on every file.
    * when the catalog already knows the covered table(s), the mask's column
      must resolve against at least one of them (_validated_ident's rule,
      which build_mask_select's docstring already promised). A pattern
      matching several tables passes when ANY of them carries the column -
      for the others the mask is a no-op by construction (they have no such
      column to leak).
    """
    try:
        expr = _mask_sql(str(col), spec, {str(col).strip()})
    except PolicyError as exc:
        raise PolicyError(
            f"{origin}: table {pattern!r}: column mask {col!r} = {spec!r} cannot be compiled: {exc}"
        ) from exc
    if not table_columns:
        return expr
    matches = [cols for tkey, cols in table_columns.items() if cols and _glob_match(pattern, str(tkey))]
    if not matches:
        # The covered table has not been described yet: the spec-syntax check
        # above is all that is knowable now (the runtime path renders the
        # redact constant rather than the raw column if it still cannot bind).
        return expr
    first_error: str | None = None
    for cols in matches:
        try:
            _mask_sql(str(col), spec, set(cols))
            # The spec-independent COLUMN check: redact/constant specs never
            # reach _validated_ident inside _mask_sql, yet a mask naming a
            # column the table does not have is dead config - the intended
            # column is then served RAW (the same class of fail-open as a
            # malformed spec), so it refuses too.
            _validated_ident(str(col), set(cols))
            return expr
        except PolicyError as exc:
            first_error = first_error or str(exc)
    raise PolicyError(
        f"{origin}: table {pattern!r}: column mask {col!r} = {spec!r} does not resolve against the "
        f"covered table's columns ({first_error})"
    )


#: Masks already reported by the query-time fallback (one WARNING per pair).
_MASK_FALLBACK_WARNED: set[tuple[str, str]] = set()

#: One-shot flag: the unkeyed-hash-mask warning (per process, not per reload).
_UNKEYED_MASK_WARNED = False


def _warn_unkeyed_masks(origin: str) -> None:
    """One WARNING per process: unkeyed hash masks are enumerable."""
    global _UNKEYED_MASK_WARNED
    if _UNKEYED_MASK_WARNED:
        return
    _UNKEYED_MASK_WARNED = True
    logger.warning(
        "policy %s uses UNKEYED hash column masks: a digest of a low-entropy value (SSN, email, "
        "salary) is enumerable and preserves equality across rows and deployments. Set %s to key "
        "them (sha256(secret||value)).",
        origin,
        MASK_HASH_SECRET_ENV,
    )


def _warn_mask_fallback(col: str, spec: str, exc: Exception) -> None:
    """One WARNING per (column, spec) when a mask cannot compile at query time."""
    key = (str(col), str(spec))
    if key in _MASK_FALLBACK_WARNED or len(_MASK_FALLBACK_WARNED) >= 256:
        return
    _MASK_FALLBACK_WARNED.add(key)
    logger.warning(
        "column mask for %r (%r) cannot be compiled (%s) - rendering the REDACT constant instead of "
        "the raw column; fix the policy file (load-time validation should have refused it)",
        col,
        spec,
        exc,
    )


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
    refuses them); a mask that STILL cannot compile here (a hot-reload race,
    a table described after the policy loaded) renders the REDACT constant -
    never the raw column (audit 2026-11-12 P1-3: this fallback used to leak).
    """
    parts: list[str] = []
    for col in columns:
        spec = next((m for name, m in masks.items() if name.lower() == col.lower()), None)
        if spec is None:
            parts.append('"' + col.replace('"', '""') + '"')
            continue
        try:
            expr = _mask_sql(col, spec, set(columns))
        except PolicyError as exc:
            # FAIL CLOSED (audit 2026-11-12 P1-3): a mask that cannot be
            # compiled must never degrade to the RAW column - the whole point
            # of the rule is that this column is not readable. Load-time
            # validation refuses such a file; this branch covers the
            # hot-reload race and tables described after the policy loaded,
            # where the safe floor is the constant 'redact' renders.
            _warn_mask_fallback(col, spec, exc)
            expr = "'***'"
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

    # The dataset-ACL document: an ALTERNATIVE authoring shape compiled onto
    # the group machinery below. Mutually exclusive with hand-written groups
    # (both present is a refusal — one file, one source of truth about who
    # sees what); when present it REPLACES the groups/subjects/key_fps/
    # default_group parsed from the file.
    datasets_doc: dict | None = None
    # Pre-declared so both branches bind the same `str | None` shape: the
    # compiled path yields a str, the hand-written path re-reads it from the
    # document (where `data.get` is Optional).
    default_group: str | None = None
    if "datasets" in data:
        if data.get("groups"):
            raise PolicyError(
                f"{origin}: 'datasets' and 'groups' are mutually exclusive — "
                "a file either maps identity with hand-written groups or with the datasets document, never both"
            )
        groups, subjects, key_fps, default_group, datasets_doc = _compile_datasets(data, origin)
    else:
        groups = data.get("groups") or {}
        if not isinstance(groups, dict):
            raise PolicyError(f"{origin}: 'groups' must be an object")
    saw_hash_mask = False
    for name, spec in groups.items():
        if not isinstance(spec, dict):
            raise PolicyError(f"{origin}: group {name!r} must be an object")
        vis = spec.get("visible_tables")
        # The dataset-ACL vocabulary at the GROUP level: same shape rules
        # as hidden_tables (fail-closed on bad globs), for the
        # hand-written-groups path and the compiled one alike.
        if vis is not None and (not isinstance(vis, list) or not all(isinstance(g, str) and g.strip() for g in vis)):
            raise PolicyError(f"{origin}: group {name!r}.visible_tables must be a list of non-empty glob strings")
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
                # COMPILE the spec at load (audit 2026-11-12 P1-3): a typo'd
                # mask used to load cleanly and silently serve the raw column.
                expr = _compile_mask_spec(origin, str(pattern), str(col), mask, table_columns)
                if "sha256(" in expr or "sha1(" in expr or "md5(" in expr:
                    saw_hash_mask = True
            if rf is not None and rf.strip() and table_columns:
                for tkey, cols in table_columns.items():
                    if _glob_match(str(pattern), tkey) and cols:
                        validate_row_filter(rf, cols, f"{name}.{pattern}")
                        break

    if saw_hash_mask and not mask_hash_secret():
        # Keyed hashing is opt-in operator configuration; say so loudly ONCE
        # rather than silently shipping enumerable digests.
        _warn_unkeyed_masks(origin)


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

    if datasets_doc is None:
        subjects = _bindings("subjects")
        key_fps = _bindings("key_fps")
        default_group = data.get("default_group")
        if default_group is not None:
            if not isinstance(default_group, str) or not default_group.strip():
                raise PolicyError(f"{origin}: 'default_group' must be a non-empty string when present")
            default_group = default_group.strip()
            if default_group not in groups:
                raise PolicyError(f"{origin}: default_group {default_group!r} is not a defined group")
        elif groups:
            # A file that DEFINES groups but names no default is ambiguous
            # about every identity it does not bind: Policy.groups_for falls
            # back to default_group, and with none set that fallback returns
            # () — NO groups — which rule_for_table treats as "no rules",
            # i.e. FULLY UNRESTRICTED. An operator who wrote a group but
            # forgot the default therefore got the opposite of the
            # fail-closed intent for every unbound key. Refuse the file at
            # LOAD time (loud, before any request is served) instead of
            # silently resolving unbound callers to unrestricted.
            #
            # The datasets document is exempt: _compile_datasets always
            # synthesizes _acl_global as the default, so it cannot be
            # ambiguous.
            raise PolicyError(
                f"{origin}: 'default_group' is required when 'groups' is non-empty — "
                "without it, identities that match no subject/key_fps binding would resolve to "
                "NO groups (unrestricted). Name the most restrictive group as the default."
            )
    else:
        # The compiled document owns identity + default_group; the file's
        # hand-written spelling of either alongside datasets would silently
        # conflict with the compilation, so it is refused (fail-closed).
        # subjects/key_fps/default_group stay what _compile_datasets
        # returned (the compiled vocabulary).
        for extra in ("subjects", "key_fps", "default_group"):
            if data.get(extra):
                raise PolicyError(f"{origin}: 'datasets' and {extra!r} are mutually exclusive")

    # The admin designation: validated INDEPENDENTLY of the authoring shape
    # (groups or the datasets document — it composes with both; the only
    # mutual exclusion is datasets-vs-groups). Fail-closed per entry.
    admins = _validate_admins(data, origin)

    pol_hash = canonical_hash(
        {
            "groups": {g: _canonicalize_group(s) for g, s in sorted(groups.items()) if isinstance(s, dict)},
            "subjects": {k: sorted(v) for k, v in sorted(subjects.items())},
            "key_fps": {k: sorted(v) for k, v in sorted(key_fps.items())},
            "default_group": default_group,
            # Fold the datasets document: ANY edit to the ACL vocabulary must
            # change the file hash (the store hot-reloads on mtime; the hash
            # is what callers derive cache-key spaces from).
            **({"datasets": datasets_doc} if datasets_doc is not None else {}),
            # The admins designation folds in too: designating (or
            # un-designating) an admin is a policy edit and must hot-reload.
            **({"admins": list(admins)} if admins else {}),
        }
    )
    return Policy(
        groups=groups,
        subjects=subjects,
        key_fps=key_fps,
        default_group=default_group,
        source=origin,
        hash=pol_hash,
        datasets=datasets_doc,
        admins=admins,
        # The authored mirror: the WHOLE parsed document (either form), so
        # the grants payload / UI editor can show the file as the operator
        # wrote it — top-level admins list included, groups form included.
        # Hash-neutral (hashing folds the compiled fields above, not this).
        authored=data,
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

    def __init__(self, table_columns_provider: Callable[[], dict[str, list[str]] | None] | None = None) -> None:
        self._lock = threading.Lock()
        self._policy: Policy | None = None
        self._stat: tuple[str, float, int] | None = None
        self._broken_since: float | None = None
        self._table_columns_provider = table_columns_provider  # () -> dict|None

    # -- configuration --------------------------------------------------------
    @staticmethod
    def configured() -> bool:
        return policy_file_path() is not None and policy_enabled()

    def set_table_columns_provider(self, provider: Callable[[], dict[str, list[str]] | None]) -> None:
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
                self._broken_since = time.time()
                if self._policy is not None:
                    # A bad edit never unmasks callers.
                    logger.warning("policy file %s invalid (%s); keeping the previous policy", path, exc)
                    return self._policy
                # FIRST-FILE failure: there is no previous policy to keep, and
                # this is NOT the same state as "no policy configured".
                # Returning a plain Policy() here made an operator's typo
                # (or a truncated mount) resolve to an UNRESTRICTED policy —
                # the log line said "fail-closed" while the behavior was
                # fail-OPEN, because an empty Policy hides nothing and masks
                # nothing. Return the BROKEN sentinel instead: a Policy whose
                # groups resolve every caller to a fully-hidden rule, so a
                # present-but-invalid file refuses covered tables rather than
                # silently exposing them.
                logger.error(
                    "policy file %s invalid: %s — NO valid policy has EVER loaded; "
                    "failing CLOSED (all covered tables hidden) until the file is fixed",
                    path,
                    exc,
                )
                return _BROKEN_POLICY
            # (unreachable)
        with self._lock:
            self._policy = fresh
            self._stat = sig
            self._broken_since = None
        if fresh.datasets is not None:
            ds = fresh.datasets
            assignments = ds.get("assignments") or {}
            logger.info(
                "policy loaded: %d group(s), %d subject(s), %d key fp(s), default_group=%s, "
                "datasets: global=%d blocked=%d assignments=%d, admins=%d from %s",
                len(fresh.groups),
                len(fresh.subjects),
                len(fresh.key_fps),
                fresh.default_group or "(none)",
                len(ds.get("global") or []),
                len(ds.get("blocked") or []),
                len(assignments),
                len(fresh.admins),
                path,
            )
        else:
            logger.info(
                "policy loaded: %d group(s), %d subject(s), %d key fp(s), default_group=%s, admins=%d from %s",
                len(fresh.groups),
                len(fresh.subjects),
                len(fresh.key_fps),
                fresh.default_group or "(none)",
                len(fresh.admins),
                path,
            )
        return fresh

    def health(self) -> dict:
        """Machine-readable enforcement posture for /health and /ready.

        Surfaces ``_broken_since`` — the state that previously existed only as
        a log line. A present-but-invalid FIRST policy file now FAILS CLOSED
        (_BROKEN_POLICY hides every covered table), and an operator needs to
        see that from the probe surface rather than infer it from a refusal:

        * ``enabled``    — enforcement configured (file + flag);
        * ``loaded``     — a VALID policy is currently enforcing;
        * ``broken``     — the file is present but invalid AND no previous
        *                  valid policy exists (fail-closed sentinel active);
        * ``degraded``   — the file went invalid but a previous policy is
                           still enforcing (serving stale-but-safe rules);
        * ``broken_since`` — epoch seconds of the first failure in this state;
        * ``source``     — the file path (None when unconfigured).

        Read-only and never raises: a probe must not be able to fail the
        process it is checking. ``groups`` is the count of loaded groups, so
        a silent "loaded but empty" file is visible too.
        """
        path = policy_file_path()
        enabled = bool(path) and policy_enabled()
        with self._lock:
            pol = self._policy
            broken_since = self._broken_since
        broken = broken_since is not None and pol is None
        return {
            "enabled": enabled,
            "loaded": pol is not None,
            "broken": broken,
            "degraded": broken_since is not None and pol is not None,
            "broken_since": broken_since,
            "source": path,
            "groups": len(pol.groups) if pol is not None else 0,
            "default_group": pol.default_group if pol is not None else None,
        }

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


def policy_health() -> dict:
    """The process-wide enforcement posture (see PolicyStore.health).

    The accessor /health and /ready should call: it reports whether policy is
    enforcing, whether a valid file has EVER loaded, and since when a broken
    file has been failing closed. Deliberately does NOT call get() — a probe
    must not trigger a reload or depend on one succeeding.
    """
    try:
        return policy_store().health()
    except Exception:  # a probe must never raise
        return {
            "enabled": False,
            "loaded": False,
            "broken": False,
            "degraded": False,
            "broken_since": None,
            "source": None,
            "groups": 0,
            "default_group": None,
        }


def reset_policy_store() -> None:
    """Test hook: drop the process-wide store."""
    global _store
    with _store_lock:
        _store = None
