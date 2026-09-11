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
  1. Namespace policy  — DEFAULT-DENY: APPLYGATE_ALLOWED_NAMESPACES unset/
                         empty means NOTHING is writable; BLOCKED always
                         wins; fnmatch globs supported.
  2. Kind allowlist    — APPLYGATE_ALLOWED_KINDS, namespaced kinds ONLY;
                         cluster-scoped kinds and Secret are refused even
                         if someone allowlists them.
  3. Manifest hygiene  — multi-doc YAML, each doc needs apiVersion/kind/
                         metadata.name, doc namespace must match the tool
                         parameter, caps on docs (8) and bytes (256 KiB).
  4. Confirm gates     — real mutation requires confirm_apply=True /
                         confirm_delete=True.
  5. Audit trail       — every apply/delete/plan (and refusal) appended as
                         JSONL to APPLYGATE_AUDIT_FILE (best-effort).

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

Secrets never flow through this server: no reads, no writes, no deletes —
manage them out-of-band (kubectl create secret / external-secrets).
"""

import argparse
import asyncio
import fnmatch
import json
import os
import sys
import traceback
from datetime import datetime, timezone

import yaml
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

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
            return False, f"namespace {namespace!r} matches APPLYGATE_BLOCKED_NAMESPACES pattern {pattern!r} — the blocklist always wins"
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


def _parse_manifest(text: str) -> list:
    """Parse a multi-doc YAML manifest into validated document dicts.

    Raises _ManifestError (with the offending doc index in the message) on:
    oversized manifests, unparseable YAML, empty/non-mapping docs, docs
    missing apiVersion/kind/metadata.name, or more than MAX_DOCS docs.
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
        parsed = list(yaml.safe_load_all("\n".join(lines)))
    except yaml.YAMLError as exc:
        raise _ManifestError(f"manifest is not valid YAML: {exc}") from exc
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
# Audit trail (best-effort JSONL — a broken audit sink never blocks or
# silently passes a write: it screams on stderr but the tool result reports
# the truth of the k8s operation).
# ---------------------------------------------------------------------------


def _audit(tool: str, namespace: str, kind: str, name: str, dry_run: bool, outcome: str) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "tool": tool,
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "dry_run": bool(dry_run),
        "outcome": outcome,
    }
    try:
        path = os.environ.get("APPLYGATE_AUDIT_FILE") or DEFAULT_AUDIT_FILE
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        # Best-effort by design — but never quiet.
        print(f"[audit] WARNING: could not append audit entry to {path}: {exc}", file=sys.stderr)


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
    ok, reason = _namespace_allowed(namespace)
    if not ok:
        return _refused("plan_apply", namespace, "", "", True, reason)
    try:
        docs = _parse_manifest(manifest)
    except _ManifestError as exc:
        return _refused("plan_apply", namespace, "", "", True, f"manifest rejected: {exc}")
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
    return json.dumps(
        {
            "ok": all(r["ok"] for r in results),
            "dry_run": True,
            # force is accepted for call-site symmetry but NEVER affects the
            # plan: no flag can turn a plan into a mutation.
            "force": bool(force),
            "note": "plan_apply is ALWAYS a dry-run (dry_run=All) — call apply_manifest with confirm_apply=true to mutate",
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


def _apply_sync(namespace: str, manifest: str, confirm_apply: bool) -> str:
    if not confirm_apply:
        return _refused(
            "apply_manifest", namespace, "", "", False,
            "confirm_apply is False — refusing to mutate. Run plan_apply first, then re-call "
            "apply_manifest with confirm_apply=true.",
        )
    ok, reason = _namespace_allowed(namespace)
    if not ok:
        return _refused("apply_manifest", namespace, "", "", False, reason)
    try:
        docs = _parse_manifest(manifest)
    except _ManifestError as exc:
        return _refused("apply_manifest", namespace, "", "", False, f"manifest rejected: {exc}")
    results = []
    for doc in docs:
        kind, name = doc["kind"], doc["metadata"]["name"]
        mismatch = _doc_namespace_mismatch(doc, namespace)
        if mismatch:
            results.append(_per_doc(kind, doc, False, mismatch))
            _audit("apply_manifest", namespace, kind, name, False, "refused")
            continue
        try:
            _check_kind(kind)
            applied = _ssa_apply(namespace, doc, dry_run=False)
            rv = (applied.get("metadata") or {}).get("resourceVersion", "?")
            results.append(_per_doc(kind, doc, True, f"applied (server-side apply, field_manager={FIELD_MANAGER}, resourceVersion={rv})"))
            _audit("apply_manifest", namespace, kind, name, False, "applied")
        except _Refusal as exc:
            results.append(_per_doc(kind, doc, False, str(exc)))
            _audit("apply_manifest", namespace, kind, name, False, "refused")
        except Exception as exc:
            results.append(_per_doc(kind, doc, False, f"{type(exc).__name__}: {exc}"))
            _audit("apply_manifest", namespace, kind, name, False, "failed")
    return json.dumps(
        {
            "ok": all(r["ok"] for r in results),
            "confirm_apply": True,
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
            "delete_resource", namespace, kind, name, False,
            "confirm_delete is False — refusing to delete. Re-call delete_resource with confirm_delete=true "
            "(and verify the object first with get_resource_status).",
        )
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
    return json.dumps(
        {"ok": True, "deleted": {"kind": kind, "name": name, "namespace": namespace}}, indent=2
    )


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
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def plan_apply(namespace: str, manifest: str, force: bool = False) -> str:
    """Validate a manifest and predict the server-side apply — ALWAYS a
    dry-run (dry_run=All): nothing is created, patched, or deleted. The
    natural first call before apply_manifest; also answers "would this
    manifest be refused, and why".

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
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
async def apply_manifest(namespace: str, manifest: str, confirm_apply: bool = False) -> str:
    """Server-side apply each document of the manifest into the namespace —
    the real, mutating write. REFUSES unless confirm_apply=True; run
    plan_apply first and read its per-doc verdicts. Every document is
    applied and audit-logged individually.

    Args:
        namespace: Target namespace (must be allowlisted; default-deny otherwise).
        manifest: YAML manifest (multi-doc OK; same hygiene rules as plan_apply).
        confirm_apply: Must be true to actually mutate. Anything else refuses loudly.
    """
    try:
        return await asyncio.to_thread(_apply_sync, namespace, manifest, confirm_apply)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return json.dumps({"ok": False, "error": f"apply_manifest failed: {type(e).__name__}: {e}"}, indent=2)


@mcp.tool(
    title="Delete Resource (gated)",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
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
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
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
        *http_app.routes,
    ]
    return Starlette(routes=routes, lifespan=http_app.router.lifespan_context)


def main():
    parser = argparse.ArgumentParser(description="applygate-mcp — governed Kubernetes write-path MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9102)
    args = parser.parse_args()

    print("applygate-mcp initialized (governed Kubernetes write path):", file=sys.stderr)
    print(f"  field_manager: {FIELD_MANAGER}", file=sys.stderr)
    print(f"  audit file: {os.environ.get('APPLYGATE_AUDIT_FILE') or DEFAULT_AUDIT_FILE}", file=sys.stderr)
    print(f"  kinds allowlist: {', '.join(_env_list('APPLYGATE_ALLOWED_KINDS')) or DEFAULT_ALLOWED_KINDS}", file=sys.stderr)
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
