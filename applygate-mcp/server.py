"""
applygate-mcp — the GOVERNED WRITE half of the fleet's Kubernetes story.

The fleet's K8s MCP server is read-only by design (it can look, never
touch). This server is the deliberate, guarded write half: server-side
apply of manifests into ALLOWLISTED namespaces — with plan/confirm
semantics, a kind allowlist, default-deny namespace policy, and a JSONL
audit trail. The guardrails ARE the product: when in doubt, refuse loudly
with a self-describing message (every refusal tells the caller exactly
which knob would have allowed it and why it is still probably a bad idea).

Tools:
  plan_apply          validate a manifest and predict the apply — ALWAYS a
                      dry-run ("All"), never mutates anything
  apply_manifest      real server-side apply per document — refuses unless
                      confirm_apply=True (call plan_apply first)
  delete_resource     delete one allowlisted resource — refuses unless
                      confirm_delete=True
  get_resource_status read-only status excerpt (verify what you applied)

Guardrail stack (checked on EVERY write, in this order):
  1. DNS-1123 hygiene    — namespace (label) and resource names (subdomain)
                           are validated BEFORE they flow into the
                           DynamicClient URL path: malformed input gets a
                           clear validation refusal, never an odd API 404.
  2. Namespace policy  — DEFAULT-DENY: APPLYGATE_ALLOWED_NAMESPACES unset/
                         empty means NOTHING is writable; BLOCKED always
                         wins; fnmatch globs supported.
  3. Kind allowlist    — APPLYGATE_ALLOWED_KINDS, namespaced kinds ONLY;
                         cluster-scoped kinds and Secret are refused even
                         if someone allowlists them.
  4. Manifest hygiene  — multi-doc YAML parsed by an ALIAS-LIMITED SafeLoader
                         (alias count + node caps + expansion budget: YAML
                         billion-laughs / quines are refused, sane manifests
                         parse identically); each doc needs
                         apiVersion/kind/metadata.name, doc namespace must
                         match the tool parameter, caps on docs (8) and
                         bytes (256 KiB).
  5. Plan binding (D11)— apply_manifest is sha256-bound to the EXACT bytes a
                         plan_apply recorded for the namespace (carried via
                         plan_sha256 or referenced from session state);
                         missing plan or tampered bytes refuse by default
                         (APPLYGATE_UNPLANNED_APPLY=deny|warn|allow).
  6. Confirm gates     — real mutation requires confirm_apply=True /
                         confirm_delete=True.
  7. Audit trail       — every apply/delete/plan (and refusal) appended as a
                         HASH-CHAINED JSONL entry (prev_sha256 + non-secret
                         caller fingerprint) to APPLYGATE_AUDIT_FILE
                         (best-effort).

Configuration (environment variables):
  APPLYGATE_ALLOWED_NAMESPACES  comma-separated namespace allowlist (globs
                                OK, e.g. "team-*"). UNSET/EMPTY = every
                                write is refused (default-deny). REQUIRED
                                to be non-empty for this server to be
                                useful at all.
  APPLYGATE_BLOCKED_NAMESPACES  comma-separated blocklist; ALWAYS wins over
                                the allowlist (e.g. "kube-system,*-system").
  APPLYGATE_ALLOWED_KINDS       comma-separated kind allowlist; may NARROW
                                the default set but can never widen it
                                (unknown kinds and cluster-scoped kinds
                                stay refused). Default covers the everyday
                                write surface: ConfigMap, Service,
                                Deployment, StatefulSet, Job, CronJob,
                                Ingress, ServiceAccount,
                                PodDisruptionBudget, HorizontalPodAutoscaler.
  APPLYGATE_AUDIT_FILE          JSONL audit path (default /data/audit.jsonl)
  APPLYGATE_UNPLANNED_APPLY     D11 transition knob: deny (default; apply
                                without a matching plan refuses) | warn
                                (applies + logs loudly) | allow (pre-D11
                                behavior). Invalid values fail closed.
  APPLYGATE_METRICS_ENABLED     serve /metrics (prometheus-client when
                                installed, honest fallback text otherwise).
                                Default off; the chart wires it from
                                values.metrics.enabled (default false).

Secrets never flow through this server: no reads, no writes, no deletes —
manage them out-of-band (kubectl create secret / external-secrets).
"""

import argparse
import asyncio
import contextvars
import fnmatch
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import traceback
from datetime import UTC, datetime
from typing import Any, NamedTuple

import yaml
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import mcp_auth

mcp = MCPServer("applygate-mcp")

# ---------------------------------------------------------------------------
# Guardrail configuration (read from the environment at CALL time, not import
# time — the policy must react to env changes and tests monkeypatch env).
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_KINDS = (
    "ConfigMap,Service,Deployment,StatefulSet,Job,CronJob,Ingress,"
    "ServiceAccount,PodDisruptionBudget,HorizontalPodAutoscaler"
)
MAX_DOCS = 8
MAX_MANIFEST_BYTES = 256 * 1024  # 256 KiB
DEFAULT_AUDIT_FILE = "/data/audit.jsonl"
FIELD_MANAGER = "applygate-mcp"

# --- D11 plan-binding (ratified 2026-09): apply_manifest is bound to the
# EXACT bytes plan_apply recorded. The transition knob trades strictness for
# migration time; the DEFAULT (deny) intentionally breaks automation that
# skips plan_apply — that is the ratified point of D11.
UNPLANNED_APPLY_ENV = "APPLYGATE_UNPLANNED_APPLY"
_PLAN_BINDING_MODES = ("deny", "warn", "allow")

# --- /metrics (additive, default off; chart key values.metrics.enabled) ----
METRICS_ENV = "APPLYGATE_METRICS_ENABLED"

# Built-in kind registry: kind -> (apiGroup, namespaced). This is the
# admission vocabulary — a kind the registry does not know is refused even
# if allowlisted, because we cannot prove it is namespaced. The env
# allowlist can therefore only NARROW this set, never widen it.
_KIND_REGISTRY = {
    # core (apiGroup "")
    "ConfigMap": ("", True),
    "Service": ("", True),
    "ServiceAccount": ("", True),
    "Pod": ("", True),
    "PersistentVolumeClaim": ("", True),
    "ReplicationController": ("", True),
    "LimitRange": ("", True),
    "ResourceQuota": ("", True),
    "Endpoints": ("", True),
    "EndpointSlice": ("", True),
    "Event": ("", True),
    "Binding": ("", True),
    # apps
    "Deployment": ("apps", True),
    "StatefulSet": ("apps", True),
    "DaemonSet": ("apps", True),
    "ReplicaSet": ("apps", True),
    "ControllerRevision": ("apps", True),
    # batch
    "Job": ("batch", True),
    "CronJob": ("batch", True),
    # networking.k8s.io
    "Ingress": ("networking.k8s.io", True),
    "NetworkPolicy": ("networking.k8s.io", True),
    # policy
    "PodDisruptionBudget": ("policy", True),
    # autoscaling
    "HorizontalPodAutoscaler": ("autoscaling", True),
    "VerticalPodAutoscaler": ("autoscaling.k8s.io", True),
    # rbac (namespaced half only)
    "Role": ("rbac.authorization.k8s.io", True),
    "RoleBinding": ("rbac.authorization.k8s.io", True),
    # coordination
    "Lease": ("coordination.k8s.io", True),
    # --- cluster-scoped kinds: NEVER writable through this server -----------
    "Namespace": ("", False),
    "Node": ("", False),
    "PersistentVolume": ("", False),
    "ComponentStatus": ("", False),
    "ClusterRole": ("rbac.authorization.k8s.io", False),
    "ClusterRoleBinding": ("rbac.authorization.k8s.io", False),
    "StorageClass": ("storage.k8s.io", False),
    "VolumeAttachment": ("storage.k8s.io", False),
    "CSIDriver": ("storage.k8s.io", False),
    "CSINode": ("storage.k8s.io", False),
    "CustomResourceDefinition": ("apiextensions.k8s.io", False),
    "APIService": ("apiregistration.k8s.io", False),
    "MutatingWebhookConfiguration": ("admissionregistration.k8s.io", False),
    "ValidatingWebhookConfiguration": ("admissionregistration.k8s.io", False),
    "ValidatingAdmissionPolicy": ("admissionregistration.k8s.io", False),
    "ValidatingAdmissionPolicyBinding": ("admissionregistration.k8s.io", False),
    "PriorityClass": ("scheduling.k8s.io", False),
    "RuntimeClass": ("node.k8s.io", False),
    "IngressClass": ("networking.k8s.io", False),
    "FlowSchema": ("flowcontrol.apiserver.k8s.io", False),
    "PriorityLevelConfiguration": ("flowcontrol.apiserver.k8s.io", False),
    "IPAddress": ("networking.k8s.io", False),
    "ServiceCIDR": ("networking.k8s.io", False),
    "ClusterCIDR": ("networking.k8s.io", False),
}

_CLUSTER_SCOPED_KINDS = {k for k, (_, namespaced) in _KIND_REGISTRY.items() if not namespaced}


class _Refusal(Exception):
    """A guardrail refusal — message is self-describing on purpose."""


class _ManifestError(ValueError):
    """The manifest itself is unparseable or violates manifest hygiene."""


def _env_list(name: str) -> list:
    """Parse a comma-separated env knob into a clean list (globs allowed)."""
    raw = os.environ.get(name, "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _namespace_allowed(namespace: str):
    """DEFAULT-DENY namespace policy -> (allowed, reason).

    - APPLYGATE_ALLOWED_NAMESPACES unset/empty means NOTHING is writable:
      the server refuses loudly rather than guessing a default-open policy.
    - APPLYGATE_BLOCKED_NAMESPACES ALWAYS wins over the allowlist.
    - fnmatch globs supported in both lists ("team-*", "kube-*").
    """
    for pattern in _env_list("APPLYGATE_BLOCKED_NAMESPACES"):
        if fnmatch.fnmatchcase(namespace, pattern):
            return (
                False,
                f"namespace {namespace!r} matches APPLYGATE_BLOCKED_NAMESPACES pattern {pattern!r} — the blocklist always wins",
            )
    allowed = _env_list("APPLYGATE_ALLOWED_NAMESPACES")
    if not allowed:
        return False, (
            "DEFAULT-DENY: APPLYGATE_ALLOWED_NAMESPACES is unset or empty — no namespaces are enabled, "
            "so every write is refused. Set APPLYGATE_ALLOWED_NAMESPACES (comma-separated, globs like 'team-*' "
            "allowed) to make anything writable."
        )
    for pattern in allowed:
        if fnmatch.fnmatchcase(namespace, pattern):
            return True, ""
    return False, (
        f"namespace {namespace!r} is not matched by APPLYGATE_ALLOWED_NAMESPACES "
        f"(currently: {', '.join(allowed)!r}) — ask the operator to allowlist it explicitly"
    )


# ---------------------------------------------------------------------------
# DNS-1123 hygiene — validated BEFORE anything reaches a DynamicClient URL
# path. Kubernetes namespaces are RFC1123 DNS *labels* (≤63 chars); object
# names are RFC1123 DNS *subdomains* (dot-separated labels, ≤253 total).
# Without this gate a crafted namespace/name either percent-encodes into an
# odd 404 from the API server or smuggles path characters; with it, malformed
# input gets a self-describing validation refusal like every other gate.
# ---------------------------------------------------------------------------

_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_DNS1123_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$")
_MAX_NAME_LENGTH = 253  # RFC1123 DNS subdomain (the Kubernetes object-name ceiling)

_LABEL_RULE = "an RFC1123 DNS label: lowercase alphanumeric or '-', must start and end alphanumeric, max 63 characters"
_SUBDOMAIN_RULE = (
    "an RFC1123 DNS subdomain: dot-separated DNS-1123 labels "
    "(lowercase alphanumeric or '-', start/end alphanumeric, each ≤63), max 253 characters total"
)


def _dns_label_error(value, what: str) -> str | None:
    """Refusal message when `value` is not an RFC1123 DNS label, else None."""
    if not isinstance(value, str) or not value:
        return f"{what} is required and must be {_LABEL_RULE}"
    if len(value) > 63 or not _DNS1123_LABEL_RE.match(value):
        return (
            f"{what} {value!r} is not valid — it must be {_LABEL_RULE} "
            "(refused before it can reach the Kubernetes API URL path)"
        )
    return None


def _dns_subdomain_error(value, what: str) -> str | None:
    """Refusal message when `value` is not an RFC1123 DNS subdomain, else None."""
    if not isinstance(value, str) or not value:
        return f"{what} is required and must be {_SUBDOMAIN_RULE}"
    if len(value) > _MAX_NAME_LENGTH or not _DNS1123_SUBDOMAIN_RE.match(value):
        return (
            f"{what} {value!r} is not valid — it must be {_SUBDOMAIN_RULE} "
            "(refused before it can reach the Kubernetes API URL path)"
        )
    return None


def _validate_target(namespace: str, name=None, *, what_name: str = "resource name") -> str | None:
    """DNS-1123 hygiene for the values that become URL path segments.

    Returns a refusal message, or None when both are clean. `name` may be
    None for the plan/apply tools (doc-level names are validated in
    _parse_manifest instead).
    """
    bad = _dns_label_error(namespace, "namespace")
    if bad:
        return bad
    if name is not None:
        return _dns_subdomain_error(name, what_name)
    return None


def _check_kind(kind: str) -> None:
    """Kind allowlist + namespaced-only admission; raises _Refusal.

    Order matters and is defense-in-depth: Secret and cluster-scoped kinds
    are refused EVEN IF someone puts them in APPLYGATE_ALLOWED_KINDS — the
    env knob can only narrow the built-in registry.
    """
    if kind == "Secret":
        raise _Refusal(
            "kind 'Secret' is hard-refused: secrets never flow through this server "
            "(no reads, no writes, no deletes — manifest values would end up in audit/log surfaces). "
            "Manage Secrets out-of-band: kubectl create secret, sealed-secrets, or external-secrets."
        )
    if kind in _CLUSTER_SCOPED_KINDS:
        raise _Refusal(
            f"kind '{kind}' is cluster-scoped — only NAMESPACED kinds are writable through this server "
            "(cluster-wide RBAC changes are exactly what this server exists to prevent)."
        )
    patterns = _env_list("APPLYGATE_ALLOWED_KINDS") or DEFAULT_ALLOWED_KINDS.split(",")
    matched = any(fnmatch.fnmatchcase(kind, p.strip()) for p in patterns if p.strip())
    if not matched:
        raise _Refusal(
            f"kind '{kind}' is not on the kind allowlist "
            f"(APPLYGATE_ALLOWED_KINDS = {', '.join(p.strip() for p in patterns if p.strip())}) — "
            "allowlist it at the operator level if it is truly needed"
        )
    if kind not in _KIND_REGISTRY:
        raise _Refusal(
            f"kind '{kind}' is allowlisted but unknown to the built-in namespaced-kind registry — "
            "refused because its scope cannot be proven. The allowlist can only narrow the registry, "
            "not introduce new kinds; extend the registry in server.py if the kind is verified namespaced."
        )


def _kind_api_version(kind: str) -> str:
    """kind -> apiVersion for DynamicClient lookup (all admitted kinds are registered)."""
    group, _ = _KIND_REGISTRY[kind]
    return f"{group}/v1" if group else "v1"


# ---------------------------------------------------------------------------
# Manifest parsing / hygiene
# ---------------------------------------------------------------------------

# --- Alias-bomb-safe YAML loading ------------------------------------------
# A YAML alias bomb ("billion laughs") is a handful of anchors referencing
# each other: the composer walks it in linear time because aliases share ONE
# node object, but anything that consumes the MATERIALIZED tree — json-
# serializing the apply body in the k8s client, audit shaping, status views —
# expands it exponentially. Two independent caps close that:
#   1. loader-level caps on alias resolutions and composed nodes (cheap,
#      bounds parse work), and
#   2. a post-parse EXPANSION BUDGET that counts what a consumer would
#      actually visit — the number the loader caps alone cannot bound —
#      while rejecting self-referential structures (YAML quines like
#      `&a [*a, *a]`) with a dedicated error.
# Sane manifests (kubectl/Helm output, a few anchors) are unaffected: they
# expand to a few hundred nodes, orders of magnitude below the caps.
_MAX_ALIAS_RESOLITIONS = 256
_MAX_LOADER_NODES = 50_000
_MAX_EXPANDED_NODES = 100_000


class _AliasLimitedLoader(yaml.SafeLoader):
    """SafeLoader with fleet alias-bomb caps (raise = refusal, not a crash).

    Overrides compose_node only: every node composition and every alias
    resolution is counted per manifest; the caps are orders of magnitude
    above anything a legitimate manifest composes.
    """

    def __init__(self, stream):
        super().__init__(stream)
        self._ag_nodes = 0
        self._ag_aliases = 0

    def compose_node(self, parent, index):
        self._ag_nodes += 1
        if self._ag_nodes > _MAX_LOADER_NODES:
            raise _ManifestError(
                f"manifest composes more than {_MAX_LOADER_NODES} YAML nodes — "
                "rejected (structure-size protection); split it into smaller applies"
            )
        if self.check_event(yaml.AliasEvent):
            self._ag_aliases += 1
            if self._ag_aliases > _MAX_ALIAS_RESOLITIONS:
                raise _ManifestError(
                    f"manifest resolves more than {_MAX_ALIAS_RESOLITIONS} YAML aliases — "
                    "rejected (alias-bomb protection); inline the anchors instead"
                )
        return super().compose_node(parent, index)


def _expanded_node_count(value, budget: int) -> int:
    """Count the ALIASES-EXPANDED tree size under `budget` (iterative walk).

    Aliases make the parsed structure a DAG: composed nodes are few while the
    expanded (fully materialized) tree is exponential. This walk visits the
    expanded tree exactly like a consumer would and aborts the moment the
    budget is exceeded, so it costs at most `budget` steps no matter how
    vicious the bomb. Self-referential structures (an anchor that, directly
    or transitively, contains its own alias — the YAML quine) are rejected
    with a dedicated message: they parse into cyclic Python objects that can
    never be serialized or applied.
    """
    _ENTER, _EXIT = 0, 1
    count = 0
    on_path = set()
    stack = [(_ENTER, id(value), value)]
    while stack:
        kind, item_id, item = stack.pop()
        if kind == _EXIT:
            on_path.discard(item_id)
            continue
        if item_id in on_path:
            raise _ManifestError(
                "manifest contains a self-referential YAML structure (an anchor that "
                "contains its own alias — a YAML 'quine'); recursive manifests are "
                "rejected because they cannot be serialized or applied"
            )
        count += 1
        if count > budget:
            raise _ManifestError(
                f"manifest expands to more than {budget} nodes after alias expansion — "
                "rejected (billion-laughs protection); remove nested aliases or split the manifest"
            )
        if isinstance(item, dict):
            on_path.add(item_id)
            stack.append((_EXIT, item_id, item))
            for k, v in item.items():
                stack.append((_ENTER, id(k), k))
                stack.append((_ENTER, id(v), v))
        elif isinstance(item, list):
            on_path.add(item_id)
            stack.append((_EXIT, item_id, item))
            for v in item:
                stack.append((_ENTER, id(v), v))
    return count


def _parse_manifest(text: str) -> list:
    """Parse a multi-doc YAML manifest into validated document dicts.

    Raises _ManifestError (with the offending doc index in the message) on:
    oversized manifests, unparseable YAML, alias bombs / quines / over-deep
    nesting, empty/non-mapping docs, docs missing apiVersion/kind/
    metadata.name, docs whose metadata.name is not an RFC1123 DNS subdomain,
    or more than MAX_DOCS docs.
    """
    if not isinstance(text, str) or not text.strip():
        raise _ManifestError("manifest is empty — nothing to plan or apply")
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise _ManifestError(
            f"manifest exceeds the {MAX_MANIFEST_BYTES // 1024} KiB byte cap "
            f"({len(text.encode('utf-8')) // 1024} KiB) — split it into smaller applies"
        )
    # A trailing '---' is an end-of-documents marker, not an empty document:
    # safe_load_all would yield a phantom None doc and refuse every manifest
    # copied out of kubectl/Helm output. Only TRAILING separators are
    # tolerated; an empty doc between documents is still rejected below.
    lines = text.splitlines()
    while lines and lines[-1].strip() in ("", "---"):
        lines.pop()
    if not lines:
        raise _ManifestError("manifest contains no documents (only separators)")
    docs = []
    try:
        parsed = list(yaml.load_all("\n".join(lines), Loader=_AliasLimitedLoader))
    except _ManifestError:
        raise
    except yaml.YAMLError as exc:
        raise _ManifestError(f"manifest is not valid YAML: {exc}") from exc
    except RecursionError as exc:
        raise _ManifestError(
            "manifest nests too deeply to parse safely (RecursionError) — flatten the structure"
        ) from exc
    # Expansion budget + quine detection over the parsed structure, BEFORE
    # any doc is validated: a bomb must never reach the per-doc loop or the
    # k8s seam, where it would be json-serialized exponentially.
    _expanded_node_count(parsed, _MAX_EXPANDED_NODES)
    for i, doc in enumerate(parsed, start=1):
        if not isinstance(doc, dict) or not doc:
            raise _ManifestError(
                f"document #{i} is empty or not a YAML mapping (a bare '---' between documents) "
                "— remove empty documents and retry"
            )
        for field in ("apiVersion", "kind"):
            if not isinstance(doc.get(field), str) or not doc[field].strip():
                raise _ManifestError(f"document #{i} is missing required field '{field}'")
        meta = doc.get("metadata")
        if not isinstance(meta, dict) or not isinstance(meta.get("name"), str) or not meta["name"].strip():
            raise _ManifestError(f"document #{i} ({doc['kind']}) is missing metadata.name")
        bad_name = _dns_subdomain_error(meta["name"], f"document #{i} ({doc['kind']}) metadata.name")
        if bad_name:
            raise _ManifestError(bad_name)
        docs.append(doc)
    if len(docs) > MAX_DOCS:
        raise _ManifestError(
            f"manifest contains {len(docs)} documents — the cap is {MAX_DOCS}; split into smaller applies"
        )
    return docs


def _doc_namespace_mismatch(doc: dict, namespace: str):
    """A doc-level metadata.namespace must agree with the tool parameter."""
    doc_ns = doc.get("metadata", {}).get("namespace")
    if isinstance(doc_ns, str) and doc_ns.strip() and doc_ns != namespace:
        return (
            f"document {doc['kind']}/{doc['metadata']['name']} declares metadata.namespace={doc_ns!r} "
            f"but the tool was called with namespace={namespace!r} — they must match; fix the manifest "
            "(drop metadata.namespace entirely to inherit the tool's namespace parameter)"
        )
    return None


# ---------------------------------------------------------------------------
# Plan binding — decision D11 (ratified 2026-09). apply_manifest is bound to
# plan_apply: the call must CARRY (plan_sha256 parameter) or REFERENCE FROM
# SESSION STATE the sha256 of the EXACT manifest bytes a successful
# plan_apply recorded for that namespace. Missing plan or tampered bytes
# REFUSE by default; APPLYGATE_UNPLANNED_APPLY=warn|allow is the documented
# migration path for automation that predates the binding.
#
# Session state is the per-process record of what this server actually
# planned: {namespace: {"sha256": <hex>, "ts": <iso>}} — the latest plan per
# namespace wins. It is deliberately NOT cross-replica: MCP 2.0 here is
# stateless, so plan and apply must reach the same replica (refusals say so
# and name the replica-local remedy: re-run plan_apply). A tool-level-refused
# plan (namespace policy, unparseable manifest) records NOTHING — the apply
# would die on the same gate anyway.
# ---------------------------------------------------------------------------

_plan_session: dict = {}
_plan_session_lock = threading.Lock()


def _manifest_sha256(manifest: str) -> str:
    """sha256 of the EXACT manifest bytes as passed (UTF-8) — the planned-bytes
    identity D11 binds on."""
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()


def _record_plan(namespace: str, sha: str) -> None:
    with _plan_session_lock:
        _plan_session[namespace] = {
            "sha256": sha,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }


def _planned_sha(namespace: str) -> str | None:
    with _plan_session_lock:
        rec = _plan_session.get(namespace)
        return rec["sha256"] if rec else None


def _plan_binding_mode() -> str:
    """APPLYGATE_UNPLANNED_APPLY, read at call time; unknown values FAIL
    CLOSED to deny (the strictest reading of a knob typo)."""
    raw = (os.environ.get(UNPLANNED_APPLY_ENV) or "").strip().lower()
    if raw in _PLAN_BINDING_MODES:
        return raw
    if raw:
        print(
            f"[plan-binding] WARNING: {UNPLANNED_APPLY_ENV}={raw!r} is not one of "
            "deny|warn|allow — failing closed to deny",
            file=sys.stderr,
        )
    return "deny"


def _check_plan_binding(namespace: str, manifest: str, carried_sha: str = "") -> tuple:
    """D11 gate. Returns (action, message, mode): action is "apply",
    "apply-warn" (proceed + loud log), or "refuse"; mode is the effective
    APPLYGATE_UNPLANNED_APPLY value (deny|warn|allow). A refuse message
    always names the escape hatch and the remedy."""
    mode = _plan_binding_mode()
    manifest_sha = _manifest_sha256(manifest)
    if mode == "allow":
        return "apply", "", mode
    # A carried sha is a caller-explicit claim: it must match THIS call's
    # bytes in every mode except allow (a mismatch is a caller bug, not a
    # migration case) and must reference a plan this server actually recorded.
    if carried_sha.strip():
        carried = carried_sha.strip().lower()
        if carried != manifest_sha:
            return (
                "refuse",
                (
                    f"plan binding (D11): refusing apply — plan_sha256 {carried_sha.strip()!r} does not match the "
                    f"sha256 of THIS call's manifest bytes ({manifest_sha}); the manifest changed after planning "
                    "or the wrong plan hash was passed. Re-run plan_apply on the exact bytes you intend to apply."
                ),
                mode,
            )
        planned = _planned_sha(namespace)
        if planned is None:
            return (
                "refuse",
                (
                    f"plan binding (D11): refusing apply — plan_sha256 was carried, but no plan_apply is recorded "
                    f"for namespace {namespace!r} on this server (session state is per-process/replica; run "
                    "plan_apply against the same replica first)."
                ),
                mode,
            )
        if planned != manifest_sha:
            return (
                "refuse",
                (
                    f"plan binding (D11): refusing apply — the carried plan_sha256 matches these bytes but is not the "
                    f"latest plan for namespace {namespace!r} (planned {planned}); re-run plan_apply."
                ),
                mode,
            )
        return "apply", "", mode
    planned = _planned_sha(namespace)
    if planned is None:
        if mode == "warn":
            return (
                "apply-warn",
                (
                    f"apply_manifest proceeded WITHOUT a matching plan_apply for namespace {namespace!r} "
                    f"({UNPLANNED_APPLY_ENV}=warn — set it to deny to re-enforce the binding, or migrate the "
                    "automation to plan first)"
                ),
                mode,
            )
        return (
            "refuse",
            (
                f"plan binding (D11): refusing apply — no plan_apply has been recorded for namespace {namespace!r} "
                "on this server. Run plan_apply with the exact same manifest bytes first, then re-apply (optionally "
                f"passing its manifest_sha256 as plan_sha256). Automation that must apply without a plan can set "
                f"{UNPLANNED_APPLY_ENV}=warn (logged, migration path) or allow (pre-D11 behavior) — the default "
                "deny is intentional."
            ),
            mode,
        )
    if planned != manifest_sha:
        if mode == "warn":
            return (
                "apply-warn",
                (
                    f"apply_manifest bytes do not match the planned bytes for namespace {namespace!r} "
                    f"(planned {planned}, applying {manifest_sha}) ({UNPLANNED_APPLY_ENV}=warn — set it to deny "
                    "to re-enforce the binding)"
                ),
                mode,
            )
        return (
            "refuse",
            (
                f"plan binding (D11): refusing apply — the manifest bytes do not match the planned bytes for "
                f"namespace {namespace!r} (planned sha256 {planned}, this manifest {manifest_sha}). Re-run "
                f"plan_apply on the exact bytes you intend to apply. {UNPLANNED_APPLY_ENV}=warn|allow overrides "
                "during the D11 migration window."
            ),
            mode,
        )
    return "apply", "", mode


# ---------------------------------------------------------------------------
# Audit trail — HASH-CHAINED, caller-attributed JSONL (best-effort). A broken
# audit sink never blocks or silently passes a write: it screams on stderr
# but the tool result reports the truth of the k8s operation.
#
# Additive schema (readers of the old 7-key format stay compatible — new
# fields only): every entry carries
#   prev_sha256 — sha256 of the PREVIOUS line's JSON text (no trailing
#                 newline); the very first line of an empty trail uses the
#                 64-zero genesis. Tampering with, truncating, or reordering
#                 any line breaks every later link (README documents the
#                 verification procedure; verify_audit_chain() runs it).
#   caller      — non-secret caller identity resolved at the auth layer
#                 (fleet pattern: K8S-MCP's _Caller): a stable sha256
#                 FINGERPRINT of the matched API key (never the key itself)
#                 and the client host:port. Anonymous (nulls) for the
#                 read-only console path and stdio use, by design.
# ---------------------------------------------------------------------------

_AUDIT_GENESIS = "0" * 64
_audit_lock = threading.Lock()

_caller_context: contextvars.ContextVar = contextvars.ContextVar("applygate_caller", default=None)


class _Caller(NamedTuple):
    """Resolved identity of the request's caller for the audit trail.

    Fleet pattern mirrors K8S-MCP's _Caller (its per-client name registry has
    no applygate equivalent yet): `key_fp` is a stable, NON-SECRET fingerprint
    of the matched key (sha256, first 12 hex) — the raw key never enters the
    audit trail; `client` is the ASGI client host:port when known.
    """

    key_fp: str | None
    client: str | None


def _resolve_caller(scope) -> _Caller:
    """Resolve the caller from raw ASGI scope — the same inputs the auth
    middleware uses, re-resolved here because the shared mcp_auth middleware
    authenticates without exposing the matched identity."""
    client = scope.get("client")
    client_str = f"{client[0]}:{client[1]}" if client else None
    presented = mcp_auth.presented_keys(scope)
    if not presented:
        return _Caller(key_fp=None, client=client_str)
    for candidate in presented:
        for valid in mcp_auth.configured_keys(AUTH_ENV_NAMES):
            if hmac.compare_digest(candidate.encode("utf-8"), valid.encode("utf-8")):
                return _Caller(
                    key_fp="sha256:" + hashlib.sha256(valid.encode("utf-8")).hexdigest()[:12],
                    client=client_str,
                )
    return _Caller(key_fp=None, client=client_str)


class _CallerAuditMiddleware:
    """Outermost ASGI wrapper: capture WHO is calling into a contextvar so
    the audit trail can attribute entries, then delegate to the real auth
    middleware. Runs on every path (console included) — attribution is
    best-effort and never gates anything. Exposes `.routes` pass-through so
    introspection of the wrapped app keeps working."""

    def __init__(self, app):
        self.app = app

    @property
    def routes(self):
        return self.app.routes

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            _caller_context.set(_resolve_caller(scope))
        await self.app(scope, receive, send)


def _audit_caller() -> dict:
    caller = _caller_context.get()
    if caller is None:
        return {"key_fp": None, "client": None}
    return {"key_fp": caller.key_fp, "client": caller.client}


def _last_audit_line_sha(path: str) -> str:
    """sha256 of the audit file's last complete line (no trailing newline) —
    the value the next entry stores as prev_sha256. An empty/missing file (or
    an unreadable one) seeds the chain from the genesis constant: best-effort
    by design, never a write blocker. In-process writes are serialized by
    _audit_lock so concurrent tool calls chain correctly."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return _AUDIT_GENESIS
            window = min(size, 64 * 1024)  # audit lines are ~1 KiB; headroom ample
            fh.seek(size - window)
            data = fh.read(window)
        last = [ln for ln in data.split(b"\n") if ln.strip()]
        if not last:
            return _AUDIT_GENESIS
        return hashlib.sha256(last[-1]).hexdigest()
    except OSError:
        return _AUDIT_GENESIS


def _audit(
    tool: str, namespace: str, kind: str, name: str, dry_run: bool, outcome: str, extra: dict | None = None
) -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "tool": tool,
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "dry_run": bool(dry_run),
        "outcome": outcome,
    }
    if extra:
        entry.update(extra)
    try:
        path = os.environ.get("APPLYGATE_AUDIT_FILE") or DEFAULT_AUDIT_FILE
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _audit_lock:
            entry["prev_sha256"] = _last_audit_line_sha(path)
            entry["caller"] = _audit_caller()
            line = json.dumps(entry, sort_keys=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:
        # Best-effort by design — but never quiet.
        print(f"[audit] WARNING: could not append audit entry to {path}: {exc}", file=sys.stderr)
    # Self-metrics at the single audit choke point (import-guarded; a missing
    # prometheus-client leaves the counters as no-ops, never the audit).
    counter = _metric_tool_calls()
    if counter is not None:
        try:
            counter.labels(tool=tool, outcome=outcome).inc()
        except Exception:
            pass


def verify_audit_chain(path: str) -> dict:
    """Verify the audit hash chain over a whole trail file — the executable
    form of the README procedure. Each non-empty line must carry
    prev_sha256 == sha256(previous line); pre-hardening entries (no
    prev_sha256 field) are treated as chain roots, so old trails verify from
    the first chained entry onward. A blank/injected line, a modified line,
    or a reordered trail is reported with the first offending line number."""
    result: dict[str, Any] = {
        "file": path,
        "exists": True,
        "entries": 0,
        "legacy_entries": 0,
        "ok": True,
        "first_bad_line": None,
        "error": "",
    }
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        result["exists"] = False
        return result
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"could not read audit file: {exc}"
        return result
    prev = _AUDIT_GENESIS
    for lineno, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            result["ok"] = False
            result["first_bad_line"] = lineno
            result["error"] = f"line {lineno} is blank — the chain has a gap (injected or corrupted line)"
            return result
        try:
            entry = json.loads(line)
        except ValueError:
            result["ok"] = False
            result["first_bad_line"] = lineno
            result["error"] = f"line {lineno} is not valid JSON — the trail was modified"
            return result
        if not isinstance(entry, dict):
            result["ok"] = False
            result["first_bad_line"] = lineno
            result["error"] = f"line {lineno} is not a JSON object"
            return result
        got = entry.get("prev_sha256")
        if got is None:
            # Pre-hardening entry: no chain link to check; it seeds the chain.
            result["legacy_entries"] += 1
        elif got != prev:
            result["ok"] = False
            result["first_bad_line"] = lineno
            result["error"] = (
                f"line {lineno}: prev_sha256 does not match the sha256 of the previous line — "
                "the trail was tampered with, truncated, or reordered"
            )
            return result
        prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
        result["entries"] += 1
    return result


# ---------------------------------------------------------------------------
# Self-metrics — /metrics route, ADDITIVE and OFF BY DEFAULT (env
# APPLYGATE_METRICS_ENABLED, wired from the chart's values.metrics.enabled,
# default false). prometheus-client is import-guarded: where it is absent
# (the fleet unit-test venv, slim images) the route serves an honest
# explanation instead of failing, and the tool counter stays a no-op —
# metrics can never gate or break the write path. Labels carry tool/outcome
# only — never namespaces, names, or manifests.
# ---------------------------------------------------------------------------

_METRIC_TOOL_CALLS = None  # lazily built Counter; False = unavailable sentinel
_metric_lock = threading.Lock()


def _metric_tool_calls():
    """prometheus_client Counter for tool calls; None when the lib is absent.

    Lock-guarded lazy init: concurrent first audits (to_thread workers) must
    not race two Counter() constructions — prometheus-client refuses a
    duplicate registration, which would permanently disable self-metrics."""
    global _METRIC_TOOL_CALLS
    if _METRIC_TOOL_CALLS is None:
        with _metric_lock:
            if _METRIC_TOOL_CALLS is None:
                try:
                    from prometheus_client import Counter

                    _METRIC_TOOL_CALLS = Counter(
                        "applygate_tool_calls_total",
                        "applygate MCP tool calls by tool and outcome",
                        ("tool", "outcome"),
                    )
                except Exception:
                    _METRIC_TOOL_CALLS = False  # import-guarded: unavailable in this env
    return _METRIC_TOOL_CALLS or None


def metrics_enabled(environ: dict | None = None) -> bool:
    """The APPLYGATE_METRICS_ENABLED gate (chart values: metrics.enabled).
    Unset/empty/falsy = OFF (the default render ships no /metrics route)."""
    env = os.environ if environ is None else environ
    raw = (env.get(METRICS_ENV) or "").strip().lower()
    return raw in ("1", "true", "yes", "on", "enabled")


# ---------------------------------------------------------------------------
# Kubernetes seam — lazily imported, monkeypatch-able. The test venv has NO
# kubernetes package; tests stub these three functions.
# ---------------------------------------------------------------------------


_dynamic_client_cache = None


def _dynamic_client():
    """Build (once) a kubernetes DynamicClient: in-cluster SA first, then kubeconfig."""
    global _dynamic_client_cache
    if _dynamic_client_cache is not None:
        return _dynamic_client_cache
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes import dynamic as k8s_dynamic

    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()  # local/dev fall-back
    _dynamic_client_cache = k8s_dynamic.DynamicClient(k8s_client.ApiClient())
    return _dynamic_client_cache


def _ssa_apply(namespace: str, doc: dict, dry_run: bool) -> dict:
    """Seam: server-side apply one document. Returns the applied object as a dict.

    Content type is application/apply-patch+yaml with field_manager
    "applygate-mcp" (declarative ownership); dry_run passes "All" so the API
    server computes the full diff/result without persisting anything.
    """
    api = _dynamic_client()
    resource = api.resources.get(api_version=_kind_api_version(doc["kind"]), kind=doc["kind"])
    kwargs = {
        "body": doc,
        "field_manager": FIELD_MANAGER,
        "namespace": namespace,
    }
    if dry_run:
        kwargs["dry_run"] = "All"
    result = resource.server_side_apply(**kwargs)
    if hasattr(result, "to_dict"):
        result = result.to_dict()
    return result if isinstance(result, dict) else {"result": str(result)}


def _get_status(namespace: str, kind: str, name: str) -> dict:
    """Seam: read one object (status excerpt shaping happens in the tool layer)."""
    api = _dynamic_client()
    resource = api.resources.get(api_version=_kind_api_version(kind), kind=kind)
    obj = resource.get(namespace=namespace, name=name)
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    return obj if isinstance(obj, dict) else {"result": str(obj)}


def _delete(namespace: str, kind: str, name: str) -> dict:
    """Seam: delete one object; returns the API's deletion response as a dict."""
    api = _dynamic_client()
    resource = api.resources.get(api_version=_kind_api_version(kind), kind=kind)
    result = resource.delete(namespace=namespace, name=name)
    if hasattr(result, "to_dict"):
        result = result.to_dict()
    return {"deleted": {"kind": kind, "name": name, "namespace": namespace}, "response": result}


# ---------------------------------------------------------------------------
# Shared sync tool bodies (the async tool wrappers offload via to_thread)
# ---------------------------------------------------------------------------


def _refused(tool: str, namespace: str, kind: str, name: str, dry_run: bool, message: str) -> str:
    _audit(tool, namespace, kind, name, dry_run, "refused")
    return json.dumps({"ok": False, "refused": True, "error": message}, indent=2)


def _per_doc(kind: str, doc: dict, ok: bool, message: str) -> dict:
    return {"kind": kind, "name": doc.get("metadata", {}).get("name", ""), "ok": ok, "message": message}


def _plan_apply_sync(namespace: str, manifest: str, force: bool) -> str:
    bad_target = _validate_target(namespace)
    if bad_target:
        return _refused("plan_apply", namespace, "", "", True, bad_target)
    ok, reason = _namespace_allowed(namespace)
    if not ok:
        return _refused("plan_apply", namespace, "", "", True, reason)
    try:
        docs = _parse_manifest(manifest)
    except _ManifestError as exc:
        return _refused("plan_apply", namespace, "", "", True, f"manifest rejected: {exc}")
    manifest_sha = _manifest_sha256(manifest)
    results = []
    for doc in docs:
        kind, name = doc["kind"], doc["metadata"]["name"]
        mismatch = _doc_namespace_mismatch(doc, namespace)
        if mismatch:
            results.append(_per_doc(kind, doc, False, mismatch))
            _audit("plan_apply", namespace, kind, name, True, "refused")
            continue
        try:
            _check_kind(kind)
            _ssa_apply(namespace, doc, dry_run=True)  # ALWAYS dry-run — never mutates
            results.append(_per_doc(kind, doc, True, "dry-run passed — would be applied; nothing was changed"))
            _audit("plan_apply", namespace, kind, name, True, "dry-run")
        except _Refusal as exc:
            results.append(_per_doc(kind, doc, False, str(exc)))
            _audit("plan_apply", namespace, kind, name, True, "refused")
        except Exception as exc:
            results.append(_per_doc(kind, doc, False, f"{type(exc).__name__}: {exc}"))
            _audit("plan_apply", namespace, kind, name, True, "failed")
    # The plan happened — its sha is what apply_manifest (D11) binds to.
    _record_plan(namespace, manifest_sha)
    return json.dumps(
        {
            "ok": all(r["ok"] for r in results),
            "dry_run": True,
            # force is accepted for call-site symmetry but NEVER affects the
            # plan: no flag can turn a plan into a mutation.
            "force": bool(force),
            "note": "plan_apply is ALWAYS a dry-run (dry_run=All) — call apply_manifest with confirm_apply=true to mutate",
            "namespace": namespace,
            "manifest_sha256": manifest_sha,
            "documents": results,
            "summary": {
                "total": len(results),
                "ok": sum(1 for r in results if r["ok"]),
                "failed": sum(1 for r in results if not r["ok"]),
            },
        },
        indent=2,
    )


def _apply_sync(namespace: str, manifest: str, confirm_apply: bool, plan_sha256: str = "") -> str:
    if not confirm_apply:
        return _refused(
            "apply_manifest",
            namespace,
            "",
            "",
            False,
            "confirm_apply is False — refusing to mutate. Run plan_apply first, then re-call "
            "apply_manifest with confirm_apply=true.",
        )
    bad_target = _validate_target(namespace)
    if bad_target:
        return _refused("apply_manifest", namespace, "", "", False, bad_target)
    ok, reason = _namespace_allowed(namespace)
    if not ok:
        return _refused("apply_manifest", namespace, "", "", False, reason)
    try:
        docs = _parse_manifest(manifest)
    except _ManifestError as exc:
        return _refused("apply_manifest", namespace, "", "", False, f"manifest rejected: {exc}")
    # D11 plan binding (default deny — see _check_plan_binding / README).
    action, binding_message, mode = _check_plan_binding(namespace, manifest, plan_sha256)
    if action == "refuse":
        return _refused("apply_manifest", namespace, "", "", False, binding_message)
    if action == "apply-warn":
        print(f"[plan-binding] WARNING: {binding_message}", file=sys.stderr)
    # The binding label reflects the MODE that let this apply through:
    # deny → enforced, warn → warn, allow → allow (pre-D11 behavior).
    binding = {"deny": "enforced", "warn": "warn", "allow": "allow"}[mode]
    audit_extra = {"manifest_sha256": _manifest_sha256(manifest)}
    if action == "apply-warn":
        audit_extra["plan_binding"] = "warn"
    results = []
    for doc in docs:
        kind, name = doc["kind"], doc["metadata"]["name"]
        mismatch = _doc_namespace_mismatch(doc, namespace)
        if mismatch:
            results.append(_per_doc(kind, doc, False, mismatch))
            _audit("apply_manifest", namespace, kind, name, False, "refused", dict(audit_extra))
            continue
        try:
            _check_kind(kind)
            applied = _ssa_apply(namespace, doc, dry_run=False)
            rv = (applied.get("metadata") or {}).get("resourceVersion", "?")
            results.append(
                _per_doc(
                    kind, doc, True, f"applied (server-side apply, field_manager={FIELD_MANAGER}, resourceVersion={rv})"
                )
            )
            _audit("apply_manifest", namespace, kind, name, False, "applied", dict(audit_extra))
        except _Refusal as exc:
            results.append(_per_doc(kind, doc, False, str(exc)))
            _audit("apply_manifest", namespace, kind, name, False, "refused", dict(audit_extra))
        except Exception as exc:
            results.append(_per_doc(kind, doc, False, f"{type(exc).__name__}: {exc}"))
            _audit("apply_manifest", namespace, kind, name, False, "failed", dict(audit_extra))
    return json.dumps(
        {
            "ok": all(r["ok"] for r in results),
            "confirm_apply": True,
            "plan_binding": binding,
            "manifest_sha256": audit_extra["manifest_sha256"],
            "namespace": namespace,
            "documents": results,
            "summary": {
                "total": len(results),
                "ok": sum(1 for r in results if r["ok"]),
                "failed": sum(1 for r in results if not r["ok"]),
            },
        },
        indent=2,
    )


def _delete_sync(namespace: str, kind: str, name: str, confirm_delete: bool) -> str:
    if not confirm_delete:
        return _refused(
            "delete_resource",
            namespace,
            kind,
            name,
            False,
            "confirm_delete is False — refusing to delete. Re-call delete_resource with confirm_delete=true "
            "(and verify the object first with get_resource_status).",
        )
    bad_target = _validate_target(namespace, name)
    if bad_target:
        return _refused("delete_resource", namespace, kind, name, False, bad_target)
    ok, reason = _namespace_allowed(namespace)
    if not ok:
        return _refused("delete_resource", namespace, kind, name, False, reason)
    try:
        _check_kind(kind)
    except _Refusal as exc:
        return _refused("delete_resource", namespace, kind, name, False, str(exc))
    try:
        _delete(namespace, kind, name)
    except Exception as exc:
        _audit("delete_resource", namespace, kind, name, False, "failed")
        return json.dumps({"ok": False, "error": f"delete failed: {type(exc).__name__}: {exc}"}, indent=2)
    _audit("delete_resource", namespace, kind, name, False, "deleted")
    return json.dumps({"ok": True, "deleted": {"kind": kind, "name": name, "namespace": namespace}}, indent=2)


def _shape_status(kind: str, obj: dict) -> dict:
    """Human-meaningful status excerpt per kind (else raw phase/conditions)."""
    status = obj.get("status", {}) if isinstance(obj, dict) else {}
    status = status if isinstance(status, dict) else {}
    if kind == "Deployment":
        summary = {
            "replicas": status.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "updated": status.get("updatedReplicas", 0),
        }
    elif kind == "StatefulSet":
        summary = {
            "replicas": status.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "current": status.get("currentReplicas", 0),
        }
    elif kind == "Job":
        summary = {
            "succeeded": status.get("succeeded", 0),
            "failed": status.get("failed", 0),
            "active": status.get("active", 0),
        }
    else:
        summary = {"phase": status.get("phase", "")}
    conditions = [
        {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason", "")}
        for c in status.get("conditions", []) or []
        if isinstance(c, dict)
    ]
    return {"summary": summary, "conditions": conditions}


def _status_sync(namespace: str, kind: str, name: str) -> str:
    # The read tool stays inside the same governed fence as the writes: this
    # server is the WRITE half, not a general-purpose reader (the fleet's
    # read-only K8s MCP covers discovery) — and 'Secret' is refused here too.
    bad_target = _validate_target(namespace, name)
    if bad_target:
        return _refused("get_resource_status", namespace, kind, name, True, bad_target)
    ok, reason = _namespace_allowed(namespace)
    if not ok:
        return _refused("get_resource_status", namespace, kind, name, True, reason)
    try:
        _check_kind(kind)
    except _Refusal as exc:
        return _refused("get_resource_status", namespace, kind, name, True, str(exc))
    try:
        obj = _get_status(namespace, kind, name)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"status lookup failed: {type(exc).__name__}: {exc}"}, indent=2)
    return json.dumps(
        {
            "ok": True,
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "status": _shape_status(kind, obj),
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# MCP tools (async wrappers offload the blocking k8s calls via to_thread)
# ---------------------------------------------------------------------------


@mcp.tool(
    title="Plan Apply (always dry-run)",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def plan_apply(namespace: str, manifest: str, force: bool = False) -> str:
    """Validate a manifest and predict the server-side apply — ALWAYS a
    dry-run (dry_run=All): nothing is created, patched, or deleted. The
    natural first call before apply_manifest; also answers "would this
    manifest be refused, and why". The result carries manifest_sha256 —
    the sha256 of the EXACT planned bytes, which apply_manifest (D11) binds
    to.

    Args:
        namespace: Target namespace (must be allowlisted; default-deny otherwise).
        manifest: YAML manifest (multi-doc OK; each doc needs apiVersion, kind,
            metadata.name; doc metadata.namespace must equal this parameter).
        force: Accepted for call-site symmetry but NEVER affects the plan —
            no flag can turn a plan into a mutation.
    """
    try:
        return await asyncio.to_thread(_plan_apply_sync, namespace, manifest, force)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return json.dumps({"ok": False, "error": f"plan_apply failed: {type(e).__name__}: {e}"}, indent=2)


@mcp.tool(
    title="Apply Manifest (server-side apply)",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
)
async def apply_manifest(namespace: str, manifest: str, confirm_apply: bool = False, plan_sha256: str = "") -> str:
    """Server-side apply each document of the manifest into the namespace —
    the real, mutating write. REFUSES unless confirm_apply=True; run
    plan_apply first and read its per-doc verdicts. Bound to the plan (D11):
    the call must carry (plan_sha256) or reference from session state the
    sha256 of the EXACT bytes plan_apply recorded for this namespace — a
    missing plan or tampered bytes refuse by default (APPLYGATE_UNPLANNED_
    APPLY=warn|allow is the documented migration path). Every document is
    applied and audit-logged individually.

    Args:
        namespace: Target namespace (must be allowlisted; default-deny otherwise).
        manifest: YAML manifest (multi-doc OK; same hygiene rules as plan_apply).
        confirm_apply: Must be true to actually mutate. Anything else refuses loudly.
        plan_sha256: Optional sha256 of the planned bytes (plan_apply's
            manifest_sha256). When carried it must match both this call's
            manifest bytes and the plan recorded in session state.
    """
    try:
        return await asyncio.to_thread(_apply_sync, namespace, manifest, confirm_apply, plan_sha256)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return json.dumps({"ok": False, "error": f"apply_manifest failed: {type(e).__name__}: {e}"}, indent=2)


@mcp.tool(
    title="Delete Resource (gated)",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
)
async def delete_resource(namespace: str, kind: str, name: str, confirm_delete: bool = False) -> str:
    """Delete one allowlisted, namespaced resource. Refuses unless
    confirm_delete=True, and only within the same namespace/kind guardrails
    as the apply tools. Audit-logged.

    Args:
        namespace: Namespace of the object (must be allowlisted; default-deny otherwise).
        kind: Resource kind (must be on the allowlist; Secret and cluster-scoped
            kinds are refused unconditionally).
        name: Object name.
        confirm_delete: Must be true to actually delete.
    """
    try:
        return await asyncio.to_thread(_delete_sync, namespace, kind, name, confirm_delete)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return json.dumps({"ok": False, "error": f"delete_resource failed: {type(e).__name__}: {e}"}, indent=2)


@mcp.tool(
    title="Get Resource Status",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_resource_status(namespace: str, kind: str, name: str) -> str:
    """Read-only status excerpt for one resource — verify what you applied.
    Deployment/StatefulSet: ready vs total replicas; Job: succeeded/failed;
    everything else: phase + conditions. Stays inside the same namespace/kind
    guardrails as the write tools (this is the write half, not a general reader).

    Args:
        namespace: Namespace of the object (must be allowlisted; default-deny otherwise).
        kind: Resource kind (allowlist + Secret/cluster-scoped refusals apply).
        name: Object name.
    """
    try:
        return await asyncio.to_thread(_status_sync, namespace, kind, name)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return json.dumps({"ok": False, "error": f"get_resource_status failed: {type(e).__name__}: {e}"}, indent=2)


# ---------------------------------------------------------------------------
# HTTP transport (MCP 2.0 stateless)
# ---------------------------------------------------------------------------

_mcp_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


# ─── API-key authentication (fleet pattern, shared module: pcai_utils/mcp_auth.py) ───
#
# /mcp is the cluster's entire governed write surface; until now it trusted
# network position alone (fleet-audit CRITICAL). Auth here is MANDATORY in
# deployment: the chart wires the key env from an operator-created Secret and
# the pod fails loud until it exists (the K8S-MCP invariant).
#
# * Scope: ONLY paths under /mcp are enforced. The read-only console
#   (/, /ui, /api/*: plan previews that are ALWAYS dry-run, audit tail,
#   effective-policy view) and /health+/healthz stay public — the console
#   has no mutation endpoints (verified in test_webui.py) and no key-input
#   UI yet. External exposure still requires the gateway's authn; giving
#   the console a login is the documented follow-up.
# * One-address wiring: the UNIVERSAL MCP_API_KEYS is honored alongside the
#   per-server APPLYGATE_API_KEYS (key sets unioned, constant-time compares).
#   Comma-separated keys within either var are the rotation mechanism —
#   append the new key, move clients over, drop the old one, no downtime.

APPLYGATE_API_KEYS_ENV = "APPLYGATE_API_KEYS"

AUTH_ENV_NAMES = (mcp_auth.UNIVERSAL_API_KEYS_ENV, APPLYGATE_API_KEYS_ENV)


def _configured_api_keys() -> list:
    return mcp_auth.configured_keys(AUTH_ENV_NAMES)


def _build_http_app():
    """Starlette app for the streamable-http transport.

    CRITICAL: the MCP session manager needs its lifespan to run (it starts
    the task group that serves /mcp). Mounting only the routes — without
    ``lifespan=http_app.router.lifespan_context`` — yields a server whose
    probes pass but where EVERY /mcp request fails with "RuntimeError:
    Task group is not initialized" (a real fleet bug; see prometheus-mcp).

    The strictly READ-ONLY web console (webui.py: /, /ui, /api/*) is mounted
    ahead of the MCP routes when APPLYGATE_WEBUI_ENABLED is on (default; the
    chart's ``webui.enabled`` values key wires this env). It exposes NO
    apply/delete endpoints — not even gated ones — so the cluster's entire
    write surface stays behind the MCP tools' confirm gates.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from webui import build_ui_routes, webui_enabled

    async def health(_request):
        # namespaces_enabled=false is loud on purpose: probes must pass, but
        # a replica reporting an empty allowlist is refusing every write.
        return JSONResponse(
            {
                "status": "ok",
                "server": "applygate-mcp",
                "namespaces_enabled": bool(_env_list("APPLYGATE_ALLOWED_NAMESPACES")),
                "field_manager": FIELD_MANAGER,
            }
        )

    async def metrics(_request):
        # Import-guarded exposition: where prometheus-client is absent (the
        # fleet unit-test venv, slim images) the route answers with an honest
        # explanation rather than a crash; tool counters are no-ops there.
        try:
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        except ImportError:
            from starlette.responses import PlainTextResponse

            return PlainTextResponse(
                "# applygate-mcp /metrics: prometheus-client is not installed in this "
                "environment (import-guarded fallback). Install it to enable exposition; "
                "the tool-call counters stay no-ops without it and nothing else is affected.\n",
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )
        from starlette.responses import Response

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # MCP 2.0 is natively stateless: no initialize handshake, no
    # Mcp-Session-Id, so any replica serves any request; json_response keeps
    # plain-HTTP clients on single responses.
    http_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=_mcp_transport_security,
    )
    routes = [
        Route("/health", health),
        Route("/healthz", health),
        # HPE-branded READ-ONLY console (/ and /ui + /api/*): plan preview
        # (always dry-run), resource status, audit tail, effective policy.
        # Strictly no apply/delete endpoints — mutations stay behind the MCP
        # tools' confirm gates. webui.enabled=false strips these; /mcp and
        # the health endpoints are unaffected.
        *(build_ui_routes() if webui_enabled() else []),
        # /metrics is ADDITIVE and OFF by default (chart values.metrics.enabled
        # → APPLYGATE_METRICS_ENABLED): the default render and route table are
        # byte-identical to the pre-metrics baseline. (Parens are load-bearing:
        # a starred ternary binds `*` to the TRUE branch only.)
        *(([Route("/metrics", metrics)]) if metrics_enabled() else []),
        *http_app.routes,
    ]
    # The API-key middleware wraps the assembled app (protected=lambda
    # self-exempts every non-/mcp path, so the console and probes are
    # unaffected) — built in here, not in main(), so ANY consumer of
    # _build_http_app gets the authenticated app. OUTSIDE it sits the
    # caller-capture middleware (fleet pattern: K8S-MCP's _Caller): it
    # resolves WHO is calling into a contextvar the hash-chained audit
    # trail attributes entries with — never gating, never logging the key.
    return _CallerAuditMiddleware(
        mcp_auth.ApiKeyAuthMiddleware(
            Starlette(routes=routes, lifespan=http_app.router.lifespan_context),
            env_names=AUTH_ENV_NAMES,
            protected=lambda p: p.startswith("/mcp"),
        )
    )


def main():
    parser = argparse.ArgumentParser(description="applygate-mcp — governed Kubernetes write-path MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9102)
    args = parser.parse_args()

    print("applygate-mcp initialized (governed Kubernetes write path):", file=sys.stderr)
    print(f"  field_manager: {FIELD_MANAGER}", file=sys.stderr)
    print(f"  audit file: {os.environ.get('APPLYGATE_AUDIT_FILE') or DEFAULT_AUDIT_FILE}", file=sys.stderr)
    print(
        f"  plan binding (D11): {_plan_binding_mode()} via {UNPLANNED_APPLY_ENV}"
        f"{' (DEFAULT)' if not (os.environ.get(UNPLANNED_APPLY_ENV) or '').strip() else ''}",
        file=sys.stderr,
    )
    print(f"  metrics: {'on (/metrics)' if metrics_enabled() else 'off'} via {METRICS_ENV}", file=sys.stderr)
    print(
        f"  kinds allowlist: {', '.join(_env_list('APPLYGATE_ALLOWED_KINDS')) or DEFAULT_ALLOWED_KINDS}",
        file=sys.stderr,
    )
    if not _env_list("APPLYGATE_ALLOWED_NAMESPACES"):
        # LOUD: default-deny is correct, but a silent empty allowlist looks
        # exactly like a broken server. Scream once at startup.
        print("=" * 72, file=sys.stderr)
        print(
            "WARNING: APPLYGATE_ALLOWED_NAMESPACES is unset/empty — NO namespaces are",
            file=sys.stderr,
        )
        print(
            "enabled: every write (plan/apply/delete) will be REFUSED (default-deny).",
            file=sys.stderr,
        )
        print("=" * 72, file=sys.stderr)
    else:
        print(f"  namespaces allowed: {', '.join(_env_list('APPLYGATE_ALLOWED_NAMESPACES'))}", file=sys.stderr)
    blocked = _env_list("APPLYGATE_BLOCKED_NAMESPACES")
    if blocked:
        print(f"  namespaces blocked: {', '.join(blocked)}", file=sys.stderr)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # LOUD: an unauthenticated write path is fine only on a laptop. The
    # middleware passes /mcp through when no keys are configured, so a
    # silent empty key list looks exactly like an authenticated server.
    mcp_auth.warn_if_open("applygate-mcp", AUTH_ENV_NAMES)

    app = _build_http_app()
    print(f"applygate-mcp streamable-http endpoint: http://{args.host}:{args.port}/mcp", file=sys.stderr)
    from webui import webui_enabled

    if webui_enabled():
        print(
            "  web ui: read-only console at / (plan previews are ALWAYS dry-run; no apply/delete endpoints)",
            file=sys.stderr,
        )
    else:
        print("  web ui: disabled (APPLYGATE_WEBUI_ENABLED)", file=sys.stderr)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
