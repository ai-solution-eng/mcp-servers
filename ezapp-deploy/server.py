"""EzApp deploy MCP server for HPE PCAI (MCP SDK v2, protocol 2026-07-28).

Deploys applications to an HPE Private Cloud AI (PCAI / Ezmeral Unified
Analytics) cluster. The server runs IN the target cluster and exposes a
deliberately tiny write surface:

- upload_chart      upload a packaged Helm chart (.tgz, base64) to the
                    in-cluster ChartMuseum (raw POST /api/charts via curl)
- upload_chart_begin/chunk/commit
                    chunked variant for charts too big for one tool call;
                    the agent streams base64 chunks and proves integrity
                    with a locally-computed sha256
- apply_ezappconfig apply the cluster-scoped EzAppConfig CR that tells the
                    EZUA app operator to install the chart
- get_ezappconfig   read one EzAppConfig back (name/status/spec)
- delete_chart      delete one chart version from ChartMuseum — ONLY a
                    version this server uploaded
- delete_ezappconfig delete one EzAppConfig — ONLY one this server applied

For charts too large for MCP tool-call arguments (clients/model caps
truncate base64 blobs around 40 KB), POST the RAW .tgz to the server's
authenticated HTTP endpoint instead:

    curl -H "Authorization: Bearer <API_KEY>" \
         --data-binary @chart.tgz https://<host>/upload[?force=true]

It runs the identical pipeline (sniff, ownership gate, curl to
ChartMuseum, ledger) and answers with JSON. Large EzAppConfig CRs get the
same treatment: POST the YAML to /manifest, then apply the returned
manifest_id with apply_ezappconfig.

ChartMuseum may be fronted by a TLS endpoint whose PCAI CA is not in the
container trust store: set CHARTMUSEUM_TLS_INSECURE=true (skip
verification) or CHARTMUSEUM_CA_BUNDLE=<path> (pin the platform CA) —
never both.

The expected caller is an agent skill that produces (a) a `helm package`
tarball and (b) the EzAppConfig manifest; this server applies them. The
manifest schema is not hardcoded: the CR is validated for shape (kind,
apiVersion, name, required spec fields) and namespace policy, then handed
to `kubectl apply`.

OWNERSHIP LEDGER
----------------
Every successful chart upload and EzAppConfig apply is recorded in a
ConfigMap in this pod's namespace (survives restarts, shared across
replicas). The destructive paths are gated on it:
- force-overwriting a chart version requires that this server uploaded it;
- delete_chart requires that this server uploaded that exact version;
- delete_ezappconfig requires that this server applied that CR;
- apply_ezappconfig refuses to overwrite an existing EzAppConfig that has
  no ledger entry (it may be someone else's app).
A missing/unreadable ledger always means REFUSAL (fail-safe): the server
would rather lose the ability to destroy things than gain it.

Security model
--------------
- WRITE-CAPABLE SERVER: every mutating path is bounded by construction.
  There is NO kubectl escape hatch and NO shell. Both curl and kubectl are
  invoked as argv lists via asyncio.create_subprocess_exec; the only
  user-controlled input that reaches them is validated first.
- apply_ezappconfig accepts ONLY `kind: EzAppConfig` documents (configurable
  kind + apiVersion allowlist) — no Secrets, Roles, or any other object can
  be applied through this server.
- Target-namespace governance: spec.options.namespace is checked against
  optional allow/deny patterns (blocked wins); kube-system / kube-public /
  kube-node-lease are always denied.
- API-key auth on every HTTP request (Authorization: Bearer or X-API-Key,
  constant-time compare). Unset = open endpoint + loud startup warning
  (local development only).
- Uploaded payloads are sniffed before they leave the pod: gzip magic bytes,
  tar structure, and a Chart.yaml with a name/version are required; upload
  bodies and decompressed sizes are capped (zip-bomb guard).
- Pod hardening (chart): non-root, read-only root filesystem, dropped
  capabilities, RuntimeDefault seccomp, /tmp emptyDir. Least-privilege RBAC:
  the ServiceAccount may only create/get/list/patch EzAppConfigs — nothing
  else.

Environment
-----------
EZAPP_MCP_API_KEY                      API key for the MCP endpoint (required
                                       in production; unset = open + warning)
CHARTMUSEUM_URL                        ChartMuseum base URL, e.g.
                                       http://chartmuseum.ez-chartmuseum-ns.svc.cluster.local:8080
CHARTMUSEUM_USERNAME / _PASSWORD       optional basic auth for ChartMuseum
EZAPP_MCP_EZAPPCONFIG_KIND             expected manifest kind (default EzAppConfig)
EZAPP_MCP_EZAPPCONFIG_API_VERSIONS     comma-separated apiVersion allowlist
                                       (default ezconfig.hpe.ezaf.com/v1alpha1;
                                       "*" = accept any)
EZAPP_MCP_EZAPPCONFIG_PLURAL           CRD plural for kubectl (default ezappconfigs)
EZAPP_MCP_MAX_CHART_MB                 upload size cap in MB (default 30)
EZAPP_MCP_ALLOWED_TARGET_NAMESPACES    optional spec.options.namespace whitelist
EZAPP_MCP_BLOCKED_TARGET_NAMESPACES    optional spec.options.namespace blacklist
EZAPP_MCP_LEDGER_CONFIGMAP             ownership-ledger ConfigMap name
                                       (default ezapp-deploy-ledger; created
                                       by the chart)
EZAPP_MCP_LEDGER_NAMESPACE             namespace of the ledger (default: the
                                       pod's service-account namespace)
EZAPP_MCP_UI_ENABLED                   read-only web view of managed apps at
                                       /ui/ (default on; "false" disables it)
EZAPP_MCP_CHUNKED_UPLOAD_ENABLED       chunked upload tools (begin/chunk/
                                       commit); default on, "false" drops
                                       the three tools from the server
EZAPP_MCP_DELETE_ENABLED               delete_chart / delete_ezappconfig;
                                       default on, "false" drops both tools
                                       (decommissioning becomes manual)
MCP_HOSTNAME                           public FQDN for DNS-rebinding protection
"""

import asyncio
import base64
import binascii
import datetime
import fnmatch
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import tarfile
import tempfile
import time
from urllib.parse import parse_qs

import yaml
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# ─── MCP server (MCP SDK v2 / protocol 2026-07-28) ───────────────────────
# Transport options (host, port, stateless_http, ...) are passed to run(),
# not the constructor. The tool list is static and identical for every
# caller, so clients may cache tools/list for 5 minutes.
mcp = MCPServer(
    "ezapp-deploy-server",
    title="EzApp Deploy MCP Server",
    description=(
        "Deploys applications to HPE PCAI (Ezmeral Unified Analytics): "
        "uploads a packaged Helm chart to the in-cluster ChartMuseum via "
        "curl and applies the cluster-scoped EzAppConfig CR that triggers "
        "the EZUA app operator. An ownership ledger limits deletes and "
        "overwrites to objects this server deployed itself."
    ),
    version="0.1.0",
    cache_hints={"tools/list": CacheHint(ttl_ms=300_000, scope="public")},
)


# ─── Configuration ───────────────────────────────────────────────────────

API_KEY_ENV = "EZAPP_MCP_API_KEY"
CHARTMUSEUM_URL_ENV = "CHARTMUSEUM_URL"
CHARTMUSEUM_USERNAME_ENV = "CHARTMUSEUM_USERNAME"
CHARTMUSEUM_PASSWORD_ENV = "CHARTMUSEUM_PASSWORD"
CHARTMUSEUM_TLS_INSECURE_ENV = "CHARTMUSEUM_TLS_INSECURE"
CHARTMUSEUM_CA_BUNDLE_ENV = "CHARTMUSEUM_CA_BUNDLE"
EZAPPCONFIG_KIND_ENV = "EZAPP_MCP_EZAPPCONFIG_KIND"
EZAPPCONFIG_API_VERSIONS_ENV = "EZAPP_MCP_EZAPPCONFIG_API_VERSIONS"
EZAPPCONFIG_PLURAL_ENV = "EZAPP_MCP_EZAPPCONFIG_PLURAL"
MAX_CHART_MB_ENV = "EZAPP_MCP_MAX_CHART_MB"
ALLOWED_TARGETS_ENV = "EZAPP_MCP_ALLOWED_TARGET_NAMESPACES"
BLOCKED_TARGETS_ENV = "EZAPP_MCP_BLOCKED_TARGET_NAMESPACES"
LEDGER_CONFIGMAP_ENV = "EZAPP_MCP_LEDGER_CONFIGMAP"
LEDGER_NAMESPACE_ENV = "EZAPP_MCP_LEDGER_NAMESPACE"
UI_ENABLED_ENV = "EZAPP_MCP_UI_ENABLED"
CHUNKED_UPLOAD_ENABLED_ENV = "EZAPP_MCP_CHUNKED_UPLOAD_ENABLED"

DEFAULT_EZAPPCONFIG_KIND = "EzAppConfig"
DEFAULT_EZAPPCONFIG_API_VERSION = "ezconfig.hpe.ezaf.com/v1alpha1"
DEFAULT_EZAPPCONFIG_PLURAL = "ezappconfigs"
# PCAI's official ChartMuseum deployment (byoa-tutorials reference flow).
DEFAULT_CHARTMUSEUM_URL = "http://chartmuseum.ez-chartmuseum-ns.svc.cluster.local:8080"

DEFAULT_MAX_CHART_MB = 30
# Decompressed-tar ceiling: a 30 MB gzipped chart cannot legitimately expand
# past this; anything larger is treated as a zip bomb and refused.
MAX_TAR_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_CHARS = 1_000_000
MAX_CHARTMUSEUM_BODY_CHARS = 5_000
OUTPUT_TRUNCATE_CHARS = 50_000

KUBECTL_TIMEOUT_SECONDS = 60
CURL_TIMEOUT_SECONDS = 120

# Teardown watch: after an accepted DELETE the CR lingers in Terminating
# while the operator uninstall (finalizer) runs — on the operator's own
# timeline, potentially minutes. The ledger entry is cleared at acceptance
# (the irreversible moment); the background watch only provides
# observability: it polls until the CR is really gone and logs, warning
# loudly if the finalizer appears stuck. Bounded so it can never leak.
_TEARDOWN_POLL_SECONDS = 10.0
_TEARDOWN_MAX_POLLS = 60          # 10 minutes

_teardown_watchers: dict = {}


async def _watch_teardown_completion(name: str, resource: str, started: float):
    """Poll until the terminating CR is really gone; log, never touch state."""
    try:
        for _ in range(_TEARDOWN_MAX_POLLS):
            await asyncio.sleep(_TEARDOWN_POLL_SECONDS)
            rc, out, err = await _run_subprocess(
                ["kubectl", "get", resource, name, "-o", "name"],
                KUBECTL_TIMEOUT_SECONDS,
            )
            if rc != 0 and "NotFound" in err:
                print(
                    f"AUDIT ezappconfig teardown completed name={name} "
                    f"seconds={int(time.monotonic() - started)}",
                    flush=True,
                )
                return
            if rc != 0:
                # Transient API trouble — keep watching.
                first = (err or f"exit {rc}").splitlines()[0]
                print(
                    f"AUDIT ezappconfig teardown check failed name={name} "
                    f"err={first}",
                    flush=True,
                )
        print(
            f"WARNING: ezappconfig teardown still not complete after "
            f"{int(_TEARDOWN_MAX_POLLS * _TEARDOWN_POLL_SECONDS)}s name={name} "
            "— the finalizer may be stuck. The ledger entry was already "
            "cleared at delete time; if the CR never disappears an operator "
            "should inspect the EZUA app operator.",
            flush=True,
        )
    finally:
        _teardown_watchers.pop(name, None)


def _schedule_teardown_watch(name: str, resource: str):
    existing = _teardown_watchers.get(name)
    if existing is not None and not existing.done():
        return
    _teardown_watchers[name] = asyncio.create_task(
        _watch_teardown_completion(name, resource, time.monotonic())
    )

# Hard floor: these namespaces are never valid deploy targets, even with no
# policy configured. The env-var policy adds to (never relaxes) this.
ALWAYS_BLOCKED_TARGETS = ("kube-system", "kube-public", "kube-node-lease")

DELETE_ENABLED_ENV = "EZAPP_MCP_DELETE_ENABLED"
_UPLOADER = "ezapp-deploy-mcp"

_DNS_SUBDOMAIN_RE = re.compile(
    r"[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?"   # DNS-1123 subdomain, <= 253
)
_CHART_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,60}")
_CHART_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}")
_NS_PATTERN_RE = re.compile(r"[a-z0-9*?][a-z0-9*?-]{0,61}")


def _configured_chartmuseum_url() -> str:
    raw = os.environ.get(CHARTMUSEUM_URL_ENV, "").strip()
    if raw:
        return raw.rstrip("/")
    # Not configured: only a sane default when the standard in-cluster
    # ChartMuseum is the intended target; tools surface this in errors too.
    return DEFAULT_CHARTMUSEUM_URL


def _chartmuseum_configured() -> bool:
    return bool(os.environ.get(CHARTMUSEUM_URL_ENV, "").strip())


def _max_chart_bytes() -> int:
    raw = os.environ.get(MAX_CHART_MB_ENV, "").strip()
    try:
        mb = int(raw) if raw else DEFAULT_MAX_CHART_MB
    except ValueError:
        mb = DEFAULT_MAX_CHART_MB
    return max(1, mb) * 1024 * 1024


def _ezappconfig_kind() -> str:
    raw = os.environ.get(EZAPPCONFIG_KIND_ENV, "").strip()
    return raw or DEFAULT_EZAPPCONFIG_KIND


def _ezappconfig_api_versions() -> tuple:
    """apiVersion allowlist; ("*",) means accept any (operator override)."""
    raw = os.environ.get(EZAPPCONFIG_API_VERSIONS_ENV, "").strip()
    if not raw:
        return (DEFAULT_EZAPPCONFIG_API_VERSION,)
    if raw == "*":
        return ("*",)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _ezappconfig_group() -> str:
    """CRD group for kubectl get, derived from the first configured apiVersion."""
    raw = os.environ.get(EZAPPCONFIG_API_VERSIONS_ENV, "").strip()
    first = raw.split(",")[0].strip() if raw and raw != "*" else DEFAULT_EZAPPCONFIG_API_VERSION
    return first.split("/", 1)[0] if "/" in first else first


def _ezappconfig_plural() -> str:
    raw = os.environ.get(EZAPPCONFIG_PLURAL_ENV, "").strip()
    return raw or DEFAULT_EZAPPCONFIG_PLURAL


# ─── Target-namespace policy (spec.options.namespace) ────────────────────

def _parse_ns_patterns(raw: str) -> tuple:
    patterns = []
    for part in raw.split(","):
        pattern = part.strip().lower()
        if not pattern:
            continue
        if not _NS_PATTERN_RE.fullmatch(pattern):
            raise ValueError(
                f"invalid namespace pattern {pattern!r} in {raw!r}: use "
                "lowercase DNS labels with optional * / ? globs"
            )
        patterns.append(pattern)
    return tuple(patterns)


def _target_namespace_policy():
    """(allowed_patterns | None, blocked_patterns | None); None = unset."""
    allowed_raw = os.environ.get(ALLOWED_TARGETS_ENV, "").strip()
    blocked_raw = os.environ.get(BLOCKED_TARGETS_ENV, "").strip()
    return (
        _parse_ns_patterns(allowed_raw) if allowed_raw else None,
        _parse_ns_patterns(blocked_raw) if blocked_raw else None,
    )


def _pattern_hit(patterns, namespace: str) -> bool:
    return any(fnmatch.fnmatchcase(namespace, pattern) for pattern in patterns)


def _policy_hint() -> str:
    allowed, _ = _target_namespace_policy()
    if allowed is None:
        return ""
    shown = ", ".join(sorted(allowed))
    if len(shown) > 200:
        shown = shown[:197] + "..."
    return f" Allowed target namespaces: {shown}."


def target_namespace_violation(namespace: str):
    """Return an error message when `namespace` is not a valid deploy target."""
    ns = (namespace or "").strip().lower()
    if not ns:
        return None
    if not _DNS_SUBDOMAIN_RE.fullmatch(ns) or len(ns) > 253:
        return (
            f"invalid target namespace {namespace!r}: must be a DNS-1123 "
            "subdomain (max 253 chars)"
        )
    if ns in ALWAYS_BLOCKED_TARGETS:
        return f"namespace '{ns}' is never a valid deploy target."
    try:
        allowed, blocked = _target_namespace_policy()
    except ValueError as e:
        return f"server target-namespace policy is misconfigured: {e}"
    if blocked is not None and _pattern_hit(blocked, ns):
        return f"namespace '{ns}' is denied by the target-namespace policy.{_policy_hint()}"
    if allowed is not None and not _pattern_hit(allowed, ns):
        return (
            f"namespace '{ns}' is not covered by the target-namespace policy."
            f"{_policy_hint()}"
        )
    return None


# ─── Subprocess execution (no shell, argv only) ──────────────────────────

# In-cluster kubeconfig for kubectl. Written once at startup to a mkstemp
# file (mode 0600) and only when actually running in-cluster (dev kubeconfig
# is left alone).
_IN_CLUSTER_KUBECONFIG = """
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
    server: https://kubernetes.default.svc.cluster.local
  name: default
contexts:
- context:
    cluster: default
    namespace: default
    user: default
  name: default
current-context: default
users:
- name: default
  user:
    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
"""
_KUBECONFIG_PATH = None


def _create_incluster_kubeconfig():
    if not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return None
    try:
        fd, path = tempfile.mkstemp(prefix="kubeconfig-", suffix=".yaml", dir="/tmp")
        with os.fdopen(fd, "w") as f:
            f.write(_IN_CLUSTER_KUBECONFIG)
        os.chmod(path, 0o600)
        return path
    except OSError as e:
        print(f"Could not create in-cluster kubeconfig: {e}")
        return None


_KUBECONFIG_PATH = _create_incluster_kubeconfig()


def _clean_subprocess_env() -> dict:
    """Env without proxy variables — in-cluster endpoints (API server,
    ChartMuseum ClusterIP) are not reachable through egress proxies."""
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    if _KUBECONFIG_PATH:
        env["KUBECONFIG"] = _KUBECONFIG_PATH
    else:
        env.pop("KUBECONFIG", None)
    return env


async def _run_subprocess(argv: list, timeout: int):
    """Run one argv-list subprocess; returns (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_clean_subprocess_env(),
        )
    except (OSError, ValueError) as e:
        return None, "", f"could not execute {argv[0]}: {e}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        return None, "", f"{argv[0]} timed out after {timeout} seconds"
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode, out, err


def _truncate(text: str, limit: int = OUTPUT_TRUNCATE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text)} chars total)"


# ─── Chart tarball sniffing (gzip/tar/Chart.yaml; no disk extraction) ────

def _inspect_chart_tarball(data: bytes):
    """Validate that `data` is a packaged Helm chart; return (name, version).

    A `helm package` tarball is a gzip stream containing a tar whose entries
    all live under one top-level directory that holds Chart.yaml. Names are
    read in memory only — nothing is extracted to disk.
    Raises ValueError with an actionable message on any violation.
    """
    if len(data) > _max_chart_bytes():
        mb = len(data) / (1024 * 1024)
        cap = _max_chart_bytes() / (1024 * 1024)
        raise ValueError(
            f"chart tarball is {mb:.1f} MB, above the {cap:.0f} MB cap "
            f"(EZAPP_MCP_MAX_CHART_MB)"
        )
    if data[:2] != b"\x1f\x8b":
        raise ValueError(
            "payload is not gzip data — pass the raw bytes of the `helm "
            "package` .tgz, base64-encoded"
        )
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            total = sum(m.size for m in members if m.isfile())
            if total > MAX_TAR_UNCOMPRESSED_BYTES:
                raise ValueError(
                    "chart tarball decompresses beyond the safety cap — "
                    "refusing (possible zip bomb)"
                )
            chart_member = None
            for m in members:
                if not m.isfile():
                    continue
                parts = m.name.split("/")
                if len(parts) == 2 and parts[1] == "Chart.yaml" and parts[0]:
                    chart_member = m
                    break
            if chart_member is None:
                raise ValueError(
                    "tarball does not look like a packaged Helm chart (no "
                    "<chart-name>/Chart.yaml at the archive root); package "
                    "with `helm package <chart-dir>` first"
                )
            fh = tar.extractfile(chart_member)
            if fh is None:
                raise ValueError("could not read Chart.yaml from the tarball")
            try:
                meta = yaml.safe_load(fh.read().decode("utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError) as e:
                raise ValueError(f"Chart.yaml is not valid YAML: {e}") from e
    except tarfile.TarError as e:
        raise ValueError(f"not a readable gzipped tar archive: {e}") from e
    if not isinstance(meta, dict):
        raise ValueError("Chart.yaml does not parse to a mapping")
    name = str(meta.get("name") or "").strip()
    version = str(meta.get("version") or "").strip()
    if not name or not _CHART_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Chart.yaml has an unusable name {name!r} (expected a Helm "
            "chart name: lowercase alphanumerics, '-', '.', '_')"
        )
    if not version or not _CHART_VERSION_RE.fullmatch(version):
        raise ValueError(
            f"Chart.yaml has an unusable version {version!r} (expected a "
            "semver string)"
        )
    return name, version


# ─── EzAppConfig manifest validation ─────────────────────────────────────

def _validate_ezappconfig(manifest_yaml: str) -> dict:
    """Validate one EzAppConfig document; returns the parsed mapping.

    The manifest comes from an agent skill, so the schema is intentionally
    NOT hardcoded beyond the fields the deploy flow depends on (kind,
    apiVersion, metadata.name, spec.name, spec.chartVersion, and the
    spec.options.namespace policy check). Raises ValueError on violations.
    """
    if not manifest_yaml or not manifest_yaml.strip():
        raise ValueError("manifest is empty")
    if len(manifest_yaml) > MAX_MANIFEST_CHARS:
        raise ValueError(
            f"manifest is {len(manifest_yaml)} chars, above the "
            f"{MAX_MANIFEST_CHARS}-char cap"
        )
    try:
        docs = list(yaml.safe_load_all(manifest_yaml))
    except yaml.YAMLError as e:
        raise ValueError(f"manifest is not valid YAML: {e}") from e
    docs = [d for d in docs if d is not None]
    if len(docs) != 1:
        raise ValueError(
            f"expected exactly one YAML document, got {len(docs)} — pass a "
            "single EzAppConfig CR"
        )
    doc = docs[0]
    if not isinstance(doc, dict):
        raise ValueError("manifest must be a YAML mapping")

    kind = str(doc.get("kind") or "").strip()
    expected_kind = _ezappconfig_kind()
    if kind != expected_kind:
        raise ValueError(
            f"only kind: {expected_kind} can be applied by this server "
            f"(got {kind!r})"
        )
    api_version = str(doc.get("apiVersion") or "").strip()
    allowed = _ezappconfig_api_versions()
    if "*" not in allowed and api_version not in allowed:
        raise ValueError(
            f"apiVersion {api_version!r} is not in the configured allowlist "
            f"({', '.join(allowed)}); set "
            f"{EZAPPCONFIG_API_VERSIONS_ENV} if your cluster uses a "
            "different EzAppConfig API version"
        )
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("metadata is required")
    name = str(metadata.get("name") or "").strip()
    if not name or len(name) > 253 or not _DNS_SUBDOMAIN_RE.fullmatch(name):
        raise ValueError(
            f"invalid metadata.name {name!r}: must be a DNS-1123 subdomain"
        )
    if metadata.get("namespace"):
        raise ValueError(
            "EzAppConfig is a cluster-scoped resource — metadata.namespace "
            "must not be set (the install namespace lives in "
            "spec.options.namespace)"
        )
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec is required")
    for field in ("name", "chartVersion"):
        value = str(spec.get(field) or "").strip()
        if not value:
            raise ValueError(f"spec.{field} is required (the EZUA app "
                             "operator pulls the chart by it)")
    spec_name = str(spec.get("name")).strip()
    if len(spec_name) > 253 or not _DNS_SUBDOMAIN_RE.fullmatch(spec_name):
        raise ValueError(
            f"invalid spec.name {spec_name!r}: must be a DNS-1123 subdomain"
        )
    chart_version = str(spec.get("chartVersion")).strip()
    if not _CHART_VERSION_RE.fullmatch(chart_version):
        raise ValueError(f"invalid spec.chartVersion {chart_version!r}")
    options = spec.get("options")
    if options is not None:
        if not isinstance(options, dict):
            raise ValueError("spec.options must be a mapping when present")
        target_ns = str(options.get("namespace") or "").strip().lower()
        if target_ns:
            violation = target_namespace_violation(target_ns)
            if violation:
                raise ValueError(violation)
    return doc


# ─── ChartMuseum helpers ─────────────────────────────────────────────────

def _chartmuseum_curl_auth_argv() -> list:
    user = os.environ.get(CHARTMUSEUM_USERNAME_ENV, "")
    password = os.environ.get(CHARTMUSEUM_PASSWORD_ENV, "")
    if user or password:
        return ["-u", f"{user}:{password}"]
    return []


def _chartmuseum_tls_argv() -> list:
    """TLS options for the outbound curl calls (ChartMuseum may be https).

    PCAI platform CAs are frequently NOT in the container's root trust
    store. Operators choose one of:
      CHARTMUSEUM_TLS_INSECURE=true  -> --insecure (skip verification)
      CHARTMUSEUM_CA_BUNDLE=<path>   -> --cacert <path> (pin the platform CA;
                                        the file must exist in the pod —
                                        mount it, don't bake it into values)
    Setting both is a configuration error (fail loud). Plain http URLs are
    unaffected.
    """
    insecure = os.environ.get(CHARTMUSEUM_TLS_INSECURE_ENV, "").strip().lower() == "true"
    ca_bundle = os.environ.get(CHARTMUSEUM_CA_BUNDLE_ENV, "").strip()
    if insecure and ca_bundle:
        raise ValueError(
            f"set either {CHARTMUSEUM_TLS_INSECURE_ENV} or "
            f"{CHARTMUSEUM_CA_BUNDLE_ENV}, not both"
        )
    if insecure:
        return ["--insecure"]
    if ca_bundle:
        if not os.path.isfile(ca_bundle):
            raise ValueError(
                f"{CHARTMUSEUM_CA_BUNDLE_ENV} points at a missing file: {ca_bundle}"
            )
        return ["--cacert", ca_bundle]
    return []


def _chartmuseum_report(code: int, body: str, action: str, target: str) -> str:
    if code == 200 and action == "delete":
        return f"Deleted {target} from ChartMuseum.\n{body}"
    if code == 404 and action == "delete":
        return f"{target} was not found in ChartMuseum (nothing to delete).\n{body}"
    if 200 <= code < 300:
        return f"ChartMuseum accepted the request for {target} ({code}).\n{body}"
    return (
        f"Error: ChartMuseum returned HTTP {code} for {action} of {target}.\n{body}"
    )


# ─── Ownership ledger (what THIS server has deployed) ────────────────────
#
# A ConfigMap in this pod's namespace (created empty by the chart and kept
# across helm upgrades via helm.sh/resource-policy: keep) records every
# chart upload and every EzAppConfig apply. All destructive paths are gated
# on it: force-overwrite, delete_chart, delete_ezappconfig, and re-applying
# an existing CR require a matching ledger entry — the server cannot touch
# charts or EzAppConfigs it did not create itself. Any ledger problem means
# REFUSAL, never permission (fail-safe).

class LedgerError(Exception):
    """The ownership ledger could not be read or updated."""


def _pod_namespace() -> str:
    override = os.environ.get(LEDGER_NAMESPACE_ENV, "").strip()
    if override:
        return override
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace",
                  encoding="utf-8") as fh:
            ns = fh.read().strip()
            if ns:
                return ns
    except OSError:
        pass
    return "default"


def _ledger_configmap_name() -> str:
    raw = os.environ.get(LEDGER_CONFIGMAP_ENV, "").strip()
    return raw or "ezapp-deploy-ledger"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class _Ledger:
    """Ownership ledger backed by one ConfigMap in this pod's namespace.

    ConfigMap .data layout:
      charts.json        {"<name>/<version>": {"uploaded_at", "filename"}}
      ezappconfigs.json  {"<name>": {"applied_at", "chart", "chart_version",
                                     "target_namespace"}}
    """

    def __init__(self):
        self.name = _ledger_configmap_name()
        self.namespace = _pod_namespace()

    @staticmethod
    def _json_section(raw):
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LedgerError(f"ownership ledger section is not valid JSON: {e}") from e
        return parsed if isinstance(parsed, dict) else {}

    async def load(self):
        """(charts, ezappconfigs) dicts; empty when the ConfigMap is absent."""
        rc, out, err = await _run_subprocess(
            ["kubectl", "get", "configmap", self.name, "-n", self.namespace,
             "-o", "json"],
            KUBECTL_TIMEOUT_SECONDS,
        )
        if rc != 0:
            if "NotFound" in err:
                return {}, {}
            raise LedgerError(
                f"cannot read the ownership ledger: {err or f'kubectl exit {rc}'}"
            )
        try:
            data = json.loads(out).get("data") or {}
        except json.JSONDecodeError as e:
            raise LedgerError(f"ownership ledger is unreadable: {e}") from e
        return (
            self._json_section(data.get("charts.json")),
            self._json_section(data.get("ezappconfigs.json")),
        )

    async def save(self, charts, ezappconfigs):
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                "labels": {"app.kubernetes.io/managed-by": "ezapp-deploy"},
            },
            "data": {
                "version": "1",
                "charts.json": json.dumps(charts, indent=2, sort_keys=True),
                "ezappconfigs.json": json.dumps(ezappconfigs, indent=2, sort_keys=True),
            },
        }
        serialized = json.dumps(body)
        if len(serialized) > 900_000:
            raise LedgerError("ownership ledger has grown too large to write")
        fd, path = tempfile.mkstemp(prefix="ledger-", suffix=".json", dir="/tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(serialized)
            os.chmod(path, 0o600)
            rc, out, err = await _run_subprocess(
                ["kubectl", "patch", "configmap", self.name, "-n", self.namespace,
                 "--type", "merge", "-p", serialized],
                KUBECTL_TIMEOUT_SECONDS,
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if rc != 0:
            hint = ""
            if "NotFound" in err:
                hint = (" — the ledger ConfigMap is missing; recreate it (see "
                        "the chart NOTES) or reinstall the chart")
            raise LedgerError(
                f"cannot update the ownership ledger: {err or f'kubectl exit {rc}'}{hint}"
            )

    async def has_chart(self, chart_name: str, chart_version: str) -> bool:
        charts, _ = await self.load()
        return f"{chart_name}/{chart_version}" in charts

    async def has_ezappconfig(self, name: str) -> bool:
        _, apps = await self.load()
        return name in apps

    async def record_chart(self, chart_name: str, chart_version: str,
                           filename: str = ""):
        charts, apps = await self.load()
        charts[f"{chart_name}/{chart_version}"] = {
            "uploaded_at": _now_iso(),
            "filename": filename or "",
        }
        await self.save(charts, apps)

    async def record_ezappconfig(self, name: str, chart: str,
                                 chart_version: str, target_namespace: str):
        charts, apps = await self.load()
        apps[name] = {
            "applied_at": _now_iso(),
            "chart": chart,
            "chart_version": chart_version,
            "target_namespace": target_namespace or "",
        }
        await self.save(charts, apps)

    async def forget_chart(self, chart_name: str, chart_version: str):
        charts, apps = await self.load()
        charts.pop(f"{chart_name}/{chart_version}", None)
        await self.save(charts, apps)

    async def forget_ezappconfig(self, name: str):
        charts, apps = await self.load()
        apps.pop(name, None)
        await self.save(charts, apps)


_ledger_instance = _Ledger()


def _get_ledger() -> _Ledger:
    return _ledger_instance


# ─── Tools ───────────────────────────────────────────────────────────────

async def _upload_chart_bytes(data: bytes, force: bool, filename: str = "") -> dict:
    """Shared chart-upload pipeline (MCP tool + raw HTTP /upload endpoint).

    Sniffs the tarball, enforces the ownership ledger on force-overwrites,
    POSTs the raw bytes to ChartMuseum via curl, and records success in the
    ledger. Returns {"status", "ok", "http_code", "chart", "version",
    "detail"} — "detail" carries the exact same message text the MCP tool
    returns.
    """
    result = {"status": "error", "ok": False, "http_code": None,
              "chart": None, "version": None, "detail": ""}
    if not _chartmuseum_configured():
        result["detail"] = (
            f"Error: {CHARTMUSEUM_URL_ENV} is not configured on this server "
            f"(default {DEFAULT_CHARTMUSEUM_URL} is in use — set the env var "
            "if your ChartMuseum lives elsewhere)."
        )
        return result
    try:
        tls_argv = _chartmuseum_tls_argv()
    except ValueError as e:
        result["detail"] = f"Error: {e}"
        return result
    try:
        chart_name, chart_version = _inspect_chart_tarball(data)
    except ValueError as e:
        result["status"] = "invalid"
        result["detail"] = f"Error: {e}"
        return result
    result["chart"], result["version"] = chart_name, chart_version

    ledger = _get_ledger()
    if force:
        try:
            managed = await ledger.has_chart(chart_name, chart_version)
        except LedgerError as e:
            result["status"] = "unverifiable"
            result["detail"] = (
                f"Error: refusing to overwrite {chart_name}-{chart_version}: "
                f"ownership cannot be verified ({e})."
            )
            return result
        if not managed:
            result["status"] = "refused"
            result["detail"] = (
                f"Error: refusing to overwrite {chart_name}-{chart_version}: "
                "this chart version was not uploaded by this server (no "
                "ledger entry). Bump the chart version instead — overwrite "
                "rights are limited to charts this server uploaded."
            )
            return result

    size_kb = len(data) / 1024
    print(
        f"AUDIT chart upload decision=allowed chart={chart_name} "
        f"version={chart_version} size_kb={size_kb:.1f} force={force} "
        f"filename={filename or 'n/a'}",
        flush=True,
    )

    url = f"{_configured_chartmuseum_url()}/api/charts"
    if force:
        url += "?force=true"
    fd, path = tempfile.mkstemp(prefix="chart-", suffix=".tgz", dir="/tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(path, 0o600)
        argv = [
            "curl", "-sS", "-m", str(CURL_TIMEOUT_SECONDS),
            *_chartmuseum_curl_auth_argv(),
            *tls_argv,
            "--data-binary", f"@{path}",
            "-w", "%{http_code}",
            url,
        ]
        rc, out, err = await _run_subprocess(argv, CURL_TIMEOUT_SECONDS + 10)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if rc is None:
        result["detail"] = f"Error: {err}"
        return result
    if rc != 0:
        result["detail"] = (
            f"Error: curl exited with code {rc} while uploading "
            f"{chart_name}-{chart_version}.tgz: {err or '(no stderr)'}"
        )
        return result
    body, code_raw = out[:-3], out[-3:]
    try:
        code = int(code_raw)
    except ValueError:
        result["detail"] = (
            f"Error: could not parse ChartMuseum response (no HTTP status "
            f"code at the end of output): {_truncate(out, MAX_CHARTMUSEUM_BODY_CHARS)}"
        )
        return result
    result["http_code"] = code
    body = _truncate(body, MAX_CHARTMUSEUM_BODY_CHARS)
    if code == 201:
        try:
            await ledger.record_chart(chart_name, chart_version, filename)
            ledger_note = "Recorded in the ownership ledger."
        except LedgerError as e:
            ledger_note = (
                f"WARNING: {e} — the chart is NOT in the ownership ledger, so "
                "this server cannot force-overwrite or delete it."
            )
        result["status"], result["ok"] = "uploaded", True
        result["detail"] = (
            f"Uploaded {chart_name}-{chart_version}.tgz to ChartMuseum "
            f"(201 Created). {ledger_note}\n{body}"
        )
        return result
    if code == 409:
        try:
            managed = await ledger.has_chart(chart_name, chart_version)
        except LedgerError:
            managed = False
        hint = (
            "pass force=true to overwrite (this server uploaded it before)"
            if managed else
            "bump the chart version — overwrite is refused for charts this "
            "server did not upload"
        )
        result["status"] = "exists"
        result["detail"] = (
            f"ChartMuseum already has {chart_name}-{chart_version}.tgz "
            f"(409 Conflict): {hint}.\n{body}"
        )
        return result
    result["detail"] = _chartmuseum_report(
        code, body, "upload", f"{chart_name}-{chart_version}.tgz"
    )
    return result


@mcp.tool()
async def upload_chart(chart_tgz_b64: str, force: bool = False, filename: str = "") -> str:
    """Upload a packaged Helm chart (.tgz) to the PCAI in-cluster ChartMuseum.

    Args:
      chart_tgz_b64: base64 of the .tgz bytes produced by `helm package`
        (an optional "data:...;base64," prefix and whitespace are stripped;
        folded/wrapped base64 with newlines is fine).
      force: pass true to overwrite an existing chart version on ChartMuseum
        (POST .../api/charts?force=true). ONLY allowed for chart versions
        this server uploaded itself (ownership ledger) — an existing
        unmanaged chart version is never overwritten.
      filename: optional original filename, used for logging only.

    SIZE LIMIT: MCP tool-call arguments get truncated by client/model caps
    (observed ~40-43 KB) — for anything but small charts, either POST the
    raw .tgz to this server's HTTP endpoint:
      curl -H "Authorization: Bearer <API_KEY>" \\
           --data-binary @chart.tgz https://<host>/upload[?force=true]
    or, from a client with no shell, use the chunked tools
    upload_chart_begin -> upload_chart_chunk -> upload_chart_commit.

    The payload is sniffed first (gzip magic, tar layout, Chart.yaml with a
    name/version) and size-capped. Upload is a raw
    `curl --data-binary @file <CHARTMUSEUM_URL>/api/charts` — the same flow
    as HPE's official byoa-tutorials. 201 = created (recorded in the
    ownership ledger); 409 = that chart version already exists (bump the
    version, or force=true when this server uploaded it before).
    """
    raw = (chart_tgz_b64 or "").strip()
    if raw.startswith("data:"):
        raw = raw.split(";", 1)[-1]
    raw = "".join(raw.split())
    if not raw:
        return "Error: chart_tgz_b64 is empty."
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as e:
        return f"Error: chart_tgz_b64 is not valid base64: {e}"
    if not data:
        return "Error: chart_tgz_b64 decodes to zero bytes."
    result = await _upload_chart_bytes(data, force, filename)
    return result["detail"] or "Error: upload failed for an unknown reason."


# ─── Chunked chart upload (charts too big for one tool call) ─────────────
#
# The single-shot upload_chart makes the MODEL the data path: tool-call
# arguments are LLM output tokens, and clients/models truncate them around
# 40-43 KB. For bigger charts the agent streams the .tgz through three
# tools as fixed-size base64 chunks and proves integrity with a locally
# computed sha256 — a single corrupted chunk fails loud at commit instead
# of reaching ChartMuseum.
#
# Sessions are transient staging held in memory (they never outlive the
# upload and expire after 15 minutes). The chart ships single-replica by
# default; with N replicas behind round-robin, chunks can land on
# different pods and the session will not be found — keep replicaCount 1
# (or use sticky sessions) if you raise it.

UPLOAD_SESSION_TTL_SECONDS = 900
UPLOAD_SESSION_MAX = 5
UPLOAD_RECOMMENDED_CHUNK_BYTES = 24 * 1024   # ~32 KB base64 — under the ~43 KB arg cap

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class _UploadSession:
    def __init__(self, total_size: int, sha256: str, filename: str, chunk_bytes: int):
        self.total_size = total_size
        self.sha256 = sha256
        self.filename = filename
        self.chunk_bytes = chunk_bytes
        self.chunks_total = -(-total_size // chunk_bytes)   # ceil
        self.buffer: list = []
        self.received = 0
        self.next_seq = 0
        self.created = time.monotonic()


_upload_sessions: dict = {}


def _sweep_upload_sessions():
    now = time.monotonic()
    expired = [
        handle for handle, session in _upload_sessions.items()
        if now - session.created > UPLOAD_SESSION_TTL_SECONDS
    ]
    for handle in expired:
        _upload_sessions.pop(handle, None)


def _chunked_upload_enabled() -> bool:
    return os.environ.get(CHUNKED_UPLOAD_ENABLED_ENV, "").strip().lower() != "false"


if _chunked_upload_enabled():

    @mcp.tool()
    async def upload_chart_begin(total_size: int, sha256: str, filename: str = "") -> str:
        """Begin a CHUNKED chart upload (for charts too big for one tool call).

        Why: tool-call arguments are model output tokens, and clients truncate
        them around 40-43 KB — single-shot upload_chart(base64) fails above
        that. Protocol, per chunk:
          1. measure the local .tgz with your shell:
             `stat -c %s chart.tgz` (or `wc -c < chart.tgz`) and
             `sha256sum chart.tgz`;
          2. call this tool -> the JSON response carries `handle` and
             `chunk_bytes`;
          3. split the FILE into `chunk_bytes`-sized BINARY chunks, base64
             each one (whitespace-stripped), and call upload_chart_chunk in
             order (seq = 0,1,2,...). Every chunk except the last is exactly
             `chunk_bytes`; the last is the remainder;
          4. call upload_chart_commit(handle) — the server verifies the
             assembled size AND sha256 before anything reaches ChartMuseum,
             then runs the same pipeline as upload_chart (sniffing, force/
             ownership gate, ledger).
        On any error, restart from step 2 (a fresh begin). Sessions expire
        after 15 minutes of inactivity.
        """
        if not isinstance(total_size, int) or total_size <= 0:
            return ("Error: total_size must be the exact byte size of the .tgz "
                    "(positive integer) — measure it with `wc -c < chart.tgz`.")
        if total_size > _max_chart_bytes():
            return (
                f"Error: total_size {total_size} exceeds the {_max_chart_bytes()} "
                f"byte cap ({MAX_CHART_MB_ENV})."
            )
        sha = (sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(sha):
            return ("Error: sha256 must be the 64-hex-char digest of the FULL "
                    ".tgz file (`sha256sum chart.tgz`).")
        _sweep_upload_sessions()
        if len(_upload_sessions) >= UPLOAD_SESSION_MAX:
            return (
                f"Error: {UPLOAD_SESSION_MAX} uploads are already in progress — "
                "commit them or let them expire first."
            )
        chunk_bytes = UPLOAD_RECOMMENDED_CHUNK_BYTES
        handle = secrets.token_hex(16)
        _upload_sessions[handle] = _UploadSession(total_size, sha, filename.strip(), chunk_bytes)
        return json.dumps({
            "handle": handle,
            "chunk_bytes": chunk_bytes,
            "chunks_total": _upload_sessions[handle].chunks_total,
            "total_size": total_size,
            "sha256": sha,
            "expires_in_seconds": UPLOAD_SESSION_TTL_SECONDS,
        })

    @mcp.tool()
    async def upload_chart_chunk(handle: str, seq: int, chunk_b64: str) -> str:
        """Send ONE chunk of a chunked chart upload (protocol in upload_chart_begin).

        Args:
          handle: from upload_chart_begin.
          seq: zero-based chunk index; must be the next expected one (strict
            sequential order).
          chunk_b64: base64 of this chunk's raw bytes (whitespace-stripped is
            fine). Every chunk except the last must be exactly `chunk_bytes`
            from the begin response; the last is the remainder.
        Returns progress JSON; when `complete` is true, call
        upload_chart_commit.
        """
        handle = (handle or "").strip()
        session = _upload_sessions.get(handle)
        if session is None:
            _sweep_upload_sessions()
            return ("Error: unknown or expired upload handle — restart with "
                    "upload_chart_begin (sessions last 15 minutes).")
        if time.monotonic() - session.created > UPLOAD_SESSION_TTL_SECONDS:
            _upload_sessions.pop(handle, None)
            return "Error: upload session expired — restart with upload_chart_begin."
        if not isinstance(seq, int) or seq != session.next_seq:
            return (f"Error: expected chunk seq {session.next_seq}, got {seq!r} "
                    "(chunks are strict sequential).")
        raw = (chunk_b64 or "").strip()
        if raw.startswith("data:"):
            raw = raw.split(";", 1)[-1]
        raw = "".join(raw.split())
        try:
            chunk = base64.b64decode(raw, validate=True) if raw else b""
        except (binascii.Error, ValueError) as e:
            return f"Error: chunk_b64 is not valid base64: {e}"
        is_last = seq == session.chunks_total - 1
        expected = (
            session.total_size - seq * session.chunk_bytes if is_last
            else session.chunk_bytes
        )
        if len(chunk) != expected:
            return (
                f"Error: chunk {seq} must be exactly {expected} bytes (got "
                f"{len(chunk)}) — fixed-size chunks except the final remainder."
            )
        session.buffer.append(chunk)
        session.received += len(chunk)
        session.next_seq += 1
        return json.dumps({
            "handle": handle,
            "seq": seq,
            "received": session.received,
            "total_size": session.total_size,
            "chunks_done": session.next_seq,
            "chunks_total": session.chunks_total,
            "complete": session.next_seq == session.chunks_total,
        })

    @mcp.tool()
    async def upload_chart_commit(handle: str, force: bool = False) -> str:
        """Finish a chunked chart upload: verify the assembled size and sha256,
        then run the same ChartMuseum pipeline as upload_chart (payload
        sniffing, force/ownership gate, ledger recording).

        Args:
          handle: from upload_chart_begin (all chunks must be sent first).
          force: pass true to overwrite an existing chart version — ONLY
            allowed for versions this server uploaded itself.
        On any integrity mismatch the session is discarded — re-run `sha256sum`
        on the file and restart with upload_chart_begin.
        """
        handle = (handle or "").strip()
        session = _upload_sessions.pop(handle, None)
        if session is None:
            _sweep_upload_sessions()
            return ("Error: unknown or expired upload handle — restart with "
                    "upload_chart_begin.")
        data = b"".join(session.buffer)
        if len(data) != session.total_size:
            return (
                f"Error: assembled {len(data)} bytes but the upload declared "
                f"{session.total_size} — restart with upload_chart_begin."
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != session.sha256:
            return (
                f"Error: sha256 mismatch (expected {session.sha256}, got "
                f"{digest}) — a chunk was corrupted in transit. Verify the file "
                "with `sha256sum` and restart with upload_chart_begin."
            )
        result = await _upload_chart_bytes(data, force, session.filename)
        return result["detail"] or "Error: upload failed for an unknown reason."


# ─── Manifest staging (POST /manifest → apply via manifest_id) ───────────
#
# An EzAppConfig CR can be as large as the chart (inline spec.values,
# base64 logoImage) and hits the same tool-call truncation. The agent
# POSTs the raw YAML to /manifest (same API-key gate), the server
# validates it up front and stages it, and apply_ezappconfig(manifest_id)
# applies it server-side — the CR text never travels through model
# tokens. Staging is in-memory, single-use, 15-minute TTL, 5 max — same
# replicaCount-1 caveat as the chunked uploads.

MANIFEST_STAGING_TTL_SECONDS = 900
MANIFEST_STAGING_MAX = 5

_staged_manifests: dict = {}


def _sweep_staged_manifests():
    now = time.monotonic()
    expired = [
        mid for mid, staged in _staged_manifests.items()
        if now - staged["created"] > MANIFEST_STAGING_TTL_SECONDS
    ]
    for mid in expired:
        _staged_manifests.pop(mid, None)


async def _apply_ezappconfig_text(manifest_yaml: str) -> str:
    """Shared apply pipeline: validate → ownership gate → kubectl → ledger."""
    try:
        doc = _validate_ezappconfig(manifest_yaml)
    except ValueError as e:
        return f"Error: {e}"
    name = doc["metadata"]["name"]
    spec = doc["spec"]
    target_ns = ((spec.get("options") or {}).get("namespace") or "(operator default)")

    # Ownership/existence gate: a fresh CR must not exist; an existing one
    # must carry this server's ledger entry. A CR that is still terminating
    # from a previous delete gets its own message (re-creating the same name
    # is blocked by the API server until the finalizer finishes anyway).
    resource = f"{_ezappconfig_plural()}.{_ezappconfig_group()}"
    rc, out, err = await _run_subprocess(
        ["kubectl", "get", resource, name, "-o", "json"], KUBECTL_TIMEOUT_SECONDS
    )
    if rc == 0:
        try:
            existing = json.loads(out)
        except json.JSONDecodeError:
            existing = {}
        if (existing.get("metadata") or {}).get("deletionTimestamp"):
            return (
                f"Error: EzAppConfig {name} is still terminating from a "
                "previous deletion (finalizer teardown in progress). Poll "
                "get_ezappconfig until NotFound, then re-apply."
            )
        try:
            known = await _get_ledger().has_ezappconfig(name)
        except LedgerError as e:
            return (
                f"Error: refusing to overwrite EzAppConfig {name}: ownership "
                f"cannot be verified ({e})."
            )
        if not known:
            return (
                f"Error: EzAppConfig {name!r} already exists but was not "
                "deployed by this server (no ledger entry) — refusing to "
                "overwrite it. An operator can delete it manually or seed "
                "the ledger if it should be adopted."
            )
    elif "NotFound" not in err:
        return (
            f"Error: could not check for an existing EzAppConfig {name}: "
            f"{err or f'kubectl exit {rc}'}"
        )

    print(
        f"AUDIT ezappconfig apply decision=allowed name={name} "
        f"target_namespace={target_ns}",
        flush=True,
    )
    fd, path = tempfile.mkstemp(prefix="ezappconfig-", suffix=".yaml", dir="/tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(manifest_yaml)
        os.chmod(path, 0o600)
        argv = [
            "kubectl", "apply", "-f", path, "-o", "name",
            "--field-manager", _UPLOADER,
        ]
        rc, out, err = await _run_subprocess(argv, KUBECTL_TIMEOUT_SECONDS)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if rc is None:
        return f"Error: {err}"
    if rc != 0:
        return (
            f"Error: kubectl apply failed for EzAppConfig {name}: "
            f"{err or out or f'(exit code {rc})'}"
        )
    try:
        await _get_ledger().record_ezappconfig(
            name, str(spec["name"]), str(spec["chartVersion"]), str(target_ns)
        )
        ledger_note = "Recorded in the ownership ledger (this server can now delete it)."
    except LedgerError as e:
        ledger_note = (
            f"WARNING: {e} — the EzAppConfig is NOT in the ownership ledger; "
            "this server will refuse to delete or re-apply it."
        )
    return (
        f"{out or f'ezappconfig.ezconfig.hpe.ezaf.com/{name} applied'}\n"
        f"The EZUA app operator now installs '{target_ns}'. Watch it with "
        f"get_ezappconfig(name=\"{name}\") until status.status is ready."
        f"{ledger_note}"
    )


@mcp.tool()
async def apply_ezappconfig(manifest_yaml: str = "", manifest_id: str = "") -> str:
    """Apply the cluster-scoped EzAppConfig CR that deploys the application.

    Args (pass EXACTLY ONE):
      manifest_yaml: the full EzAppConfig CR as YAML (single document).
        Fine for small CRs; MCP clients truncate tool-call arguments around
        40-43 KB, so big CRs (long spec.values, base64 logoImage) get cut
        off mid-payload.
      manifest_id: for LARGE CRs — POST the raw YAML to this server's HTTP
        endpoint instead and pass the returned id here:
          curl -sS -H "Authorization: Bearer <API_KEY>" \\
               --data-binary @ezappconfig.yaml \\
               https://<host>/manifest        -> {"manifest_id": "..."}
        The server validates the CR at staging time and applies it
        server-side. Staged manifests are single-use and expire after
        15 minutes.

    Validation before kubectl (this server applies NOTHING else):
      - kind must be EzAppConfig (EZAPP_MCP_EZAPPCONFIG_KIND) and apiVersion
        must be in the configured allowlist;
      - single YAML document, DNS-1123 metadata.name, no metadata.namespace
        (cluster-scoped);
      - spec.name and spec.chartVersion are required;
      - spec.options.namespace must pass the target-namespace policy
        (kube-system/kube-public/kube-node-lease always denied).
    An existing EzAppConfig WITHOUT a ledger entry (deployed by someone
    else) is refused — this server only overwrites what it deployed first.
    Applied with `kubectl apply -f <file> --field-manager ezapp-deploy-mcp`.
    Check progress afterwards with get_ezappconfig (status.status: ready /
    error / warning / initialized).
    """
    if manifest_id and manifest_yaml:
        return ("Error: pass EITHER manifest_yaml (small CRs) OR manifest_id "
                "(staged via POST /manifest), not both.")
    if manifest_id:
        staged = _staged_manifests.pop((manifest_id or "").strip(), None)
        if staged is None:
            _sweep_staged_manifests()
            return ("Error: unknown or expired manifest_id — POST the CR YAML "
                    "to /manifest again (staged manifests are single-use and "
                    "expire after 15 minutes).")
        if time.monotonic() - staged["created"] > MANIFEST_STAGING_TTL_SECONDS:
            return ("Error: staged manifest expired — POST it to /manifest "
                    "again.")
        manifest_yaml = staged["yaml"]
    elif not manifest_yaml:
        return ("Error: nothing to apply — pass the CR as manifest_yaml, or "
                "POST it to /manifest (large CRs) and pass the returned "
                "manifest_id.")
    return await _apply_ezappconfig_text(manifest_yaml)


# Elision threshold for read-backs: any string value longer than this is
# replaced by a size+hash marker so the status fields can never be pushed
# past the tool-output truncation by a big spec.values / logoImage blob.
_ELIDE_THRESHOLD_CHARS = 1024


def _elide_large_strings(node, include_values: bool):
    """Deep-copy walk replacing long string values with size+sha markers."""
    if isinstance(node, dict):
        return {k: _elide_large_strings(v, include_values) for k, v in node.items()}
    if isinstance(node, list):
        return [_elide_large_strings(v, include_values) for v in node]
    if isinstance(node, str) and len(node) > _ELIDE_THRESHOLD_CHARS:
        if include_values:
            return node
        digest = hashlib.sha256(node.encode("utf-8")).hexdigest()[:16]
        return f"<elided: {len(node)} bytes, sha256={digest}>"
    return node


@mcp.tool()
async def get_ezappconfig(name: str, output: str = "summary",
                          include_values: bool = False) -> str:
    """Read one cluster-scoped EzAppConfig back (spec + status).

    Args:
      name: metadata.name of the EzAppConfig.
      output: "summary" (default — compact JSON with the FULL .status block
        plus the key spec fields; large blobs such as spec.values and
        spec.logoImage are elided to '<elided: N bytes, sha256=...>' so the
        status can never be pushed past the tool-output truncation),
        "yaml" (full document, same elision), or "json" (raw, unelided —
        may truncate for very large values).
      include_values: also include the actual spec.values / spec.logoImage
        content in summary/yaml output (may truncate when huge).

    Useful fields: .status.status (ready | error | warning | initialized),
    .status.retryCnt, .status.failureReason, .spec.install, and the
    platform-generated resources under .status. For a rollout poll the
    default summary is all you need.
    """
    name = (name or "").strip().lower()
    if not name or len(name) > 253 or not _DNS_SUBDOMAIN_RE.fullmatch(name):
        return f"Error: invalid EzAppConfig name {name!r}"
    output = (output or "summary").strip().lower()
    if output not in {"summary", "yaml", "json"}:
        return f"Error: unsupported output {output!r}; use summary, yaml or json"
    resource = f"{_ezappconfig_plural()}.{_ezappconfig_group()}"
    rc, out, err = await _run_subprocess(
        ["kubectl", "get", resource, name, "-o", "json"], KUBECTL_TIMEOUT_SECONDS
    )
    if rc is None:
        return f"Error: {err}"
    if rc != 0:
        return f"Error: kubectl get {resource} {name} failed: {err or '(no stderr)'}"
    try:
        doc = json.loads(out)
    except json.JSONDecodeError as e:
        return f"Error: unreadable API server response: {e}"
    if output == "json":
        return _truncate(json.dumps(doc, indent=2))
    elided = _elide_large_strings(doc, include_values)
    if output == "yaml":
        return _truncate(yaml.safe_dump(elided, sort_keys=False))
    summary = {
        "apiVersion": elided.get("apiVersion"),
        "kind": elided.get("kind"),
        "metadata": {
            k: elided.get("metadata", {}).get(k)
            for k in ("name", "creationTimestamp", "generation", "resourceVersion")
        },
        "status": elided.get("status") or {"status": "unknown"},
        "spec": elided.get("spec") or {},
    }
    return _truncate(json.dumps(summary, indent=2))


def _delete_enabled() -> bool:
    return os.environ.get(DELETE_ENABLED_ENV, "").strip().lower() != "false"


# Registration gate: with EZAPP_MCP_DELETE_ENABLED=false the two delete
# tools below remain defined but are NOT registered on the MCP server —
# unregistered tools are invisible and uncallable for clients. Default on.
_maybe_delete = mcp.tool() if _delete_enabled() else (lambda fn: fn)


@_maybe_delete
async def delete_chart(chart_name: str, chart_version: str) -> str:
    """Delete one chart version from ChartMuseum (curl -X DELETE) — ONLY one
    this server uploaded.

    Args:
      chart_name: Helm chart name (as packaged in Chart.yaml).
      chart_version: the exact chart version to remove.

    The ownership ledger must contain this chart/version (recorded when
    this server uploaded it); anything else is refused. Use case:
    re-uploading a FIXED chart under the same version (ChartMuseum refuses
    duplicates with 409). Deleting a version that a running EzAppConfig
    still references will break its re-install/upgrade path — prefer
    bumping the version.
    """
    chart_name = (chart_name or "").strip()
    chart_version = (chart_version or "").strip()
    if not _CHART_NAME_RE.fullmatch(chart_name):
        return f"Error: invalid chart name {chart_name!r}"
    if not _CHART_VERSION_RE.fullmatch(chart_version):
        return f"Error: invalid chart version {chart_version!r}"
    try:
        managed = await _get_ledger().has_chart(chart_name, chart_version)
    except LedgerError as e:
        return (
            f"Error: refusing to delete {chart_name}-{chart_version}: "
            f"ownership cannot be verified ({e})."
        )
    if not managed:
        return (
            f"Error: refusing to delete {chart_name}-{chart_version}: this "
            "chart version was not uploaded by this server (no ledger "
            "entry). Deletion is limited to charts this server uploaded."
        )
    print(
        f"AUDIT chart delete decision=allowed chart={chart_name} "
        f"version={chart_version} managed=true",
        flush=True,
    )
    try:
        tls_argv = _chartmuseum_tls_argv()
    except ValueError as e:
        return f"Error: {e}"
    url = f"{_configured_chartmuseum_url()}/api/charts/{chart_name}/{chart_version}"
    argv = [
        "curl", "-sS", "-m", str(CURL_TIMEOUT_SECONDS),
        *_chartmuseum_curl_auth_argv(),
        *tls_argv,
        "-X", "DELETE",
        "-w", "%{http_code}",
        url,
    ]
    rc, out, err = await _run_subprocess(argv, CURL_TIMEOUT_SECONDS + 10)
    if rc is None:
        return f"Error: {err}"
    if rc != 0:
        return f"Error: curl exited with code {rc} while deleting {chart_name}-{chart_version}: {err or '(no stderr)'}"
    body, code_raw = out[:-3], out[-3:]
    try:
        code = int(code_raw)
    except ValueError:
        return (
            f"Error: could not parse ChartMuseum response (no HTTP status "
            f"code at the end of output): {_truncate(out, MAX_CHARTMUSEUM_BODY_CHARS)}"
        )
    body = _truncate(body, MAX_CHARTMUSEUM_BODY_CHARS)
    if code in (200, 404):
        try:
            await _get_ledger().forget_chart(chart_name, chart_version)
            ledger_note = "Ledger entry removed."
        except LedgerError as e:
            ledger_note = (
                f"WARNING: {e} — a stale ledger entry remains (future deletes "
                "of it will 404 harmlessly)."
            )
        if code == 200:
            return (
                f"Deleted {chart_name}-{chart_version} from ChartMuseum. "
                f"{ledger_note}\n{body}"
            )
        return (
            f"{chart_name}-{chart_version} was not found in ChartMuseum "
            f"(already gone). {ledger_note}\n{body}"
        )
    return _chartmuseum_report(code, body, "delete", f"{chart_name}-{chart_version}")


@_maybe_delete
async def delete_ezappconfig(name: str) -> str:
    """Delete a cluster-scoped EzAppConfig — ONLY one this server deployed.

    Args:
      name: metadata.name of the EzAppConfig.

    The ownership ledger must contain the name (recorded when this server
    applied it); anything else is refused — the server cannot decommission
    applications it did not deploy itself. Inspect the app first with
    get_ezappconfig (status.status: ready | error | warning | initialized).

    Semantics: this is a single call. The DELETE is issued with --wait=false;
    on acceptance the ledger entry is removed immediately (the irreversible
    moment) and a bounded background watch logs when the operator teardown
    (finalizer) actually completes — warning loudly if it appears stuck.
    Confirm completion yourself with get_ezappconfig (NotFound). Note a
    terminating CR with the same name blocks a re-apply until it disappears.
    """
    name = (name or "").strip().lower()
    if not name or len(name) > 253 or not _DNS_SUBDOMAIN_RE.fullmatch(name):
        return f"Error: invalid EzAppConfig name {name!r}"
    try:
        known = await _get_ledger().has_ezappconfig(name)
    except LedgerError as e:
        return (
            f"Error: refusing to delete EzAppConfig {name}: ownership "
            f"cannot be verified ({e})."
        )
    if not known:
        return (
            f"Error: refusing to delete EzAppConfig {name!r}: it was not "
            "deployed by this server (no ledger entry). Deletion is limited "
            "to objects this server applied."
        )
    print(f"AUDIT ezappconfig delete decision=allowed name={name}", flush=True)
    resource = f"{_ezappconfig_plural()}.{_ezappconfig_group()}"
    rc, out, err = await _run_subprocess(
        ["kubectl", "delete", resource, name, "-o", "name", "--wait=false"],
        KUBECTL_TIMEOUT_SECONDS,
    )
    if rc is None:
        return f"Error: {err}"
    if rc == 0:
        # Acceptance is the irreversible moment: the CR will be torn down by
        # the operator (finalizer). Clear the ledger entry now — single call,
        # no agent-side reconciliation — and watch in the background.
        try:
            await _get_ledger().forget_ezappconfig(name)
            ledger_note = "Ledger entry removed."
        except LedgerError as e:
            ledger_note = f"WARNING: {e} — a stale ledger entry remains."
        _schedule_teardown_watch(name, resource)
        return (
            f"{out or f'EzAppConfig {name} deletion accepted'}. {ledger_note} "
            "The operator teardown (finalizer) runs asynchronously — confirm "
            "with get_ezappconfig (NotFound when fully gone). A re-apply of "
            "the same name is refused while the old CR is still terminating."
        )
    if "NotFound" in err:
        try:
            await _get_ledger().forget_ezappconfig(name)
            ledger_note = "Stale ledger entry removed."
        except LedgerError as e:
            ledger_note = f"WARNING: {e} — a stale ledger entry remains."
        return f"EzAppConfig {name} was already gone (NotFound). {ledger_note}"
    return (
        f"Error: kubectl delete failed for EzAppConfig {name}: "
        f"{err or f'(exit code {rc})'}"
    )


# ─── HTTP API-key auth (transport layer) ─────────────────────────────────

_UNAUTHORIZED_BODY = b'{"error": "unauthorized: missing or invalid API key"}'


def _header_value(scope, lowercase_name: bytes) -> "str | None":
    for name, value in scope.get("headers", []):
        if name.lower() == lowercase_name:
            return value.decode("latin-1")
    return None


def _presented_api_keys(scope) -> list:
    """Candidate API keys from raw ASGI request headers (all lowercase names).

    Accepts `Authorization: Bearer <key>` and `X-API-Key: <key>`; both are
    collected so clients can use whichever header their MCP client exposes.
    """
    candidates = []
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                candidates.append(token.strip())
        elif name.lower() == b"x-api-key":
            candidates.append(value.decode("latin-1").strip())
    return candidates


def _json_asgi_response(send, status: int, body: bytes, extra_headers=()):
    """Minimal ASGI JSON response used by middleware short-circuits."""
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    return [
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    ]


class _ApiKeyAuthMiddleware:
    """Pure-ASGI middleware authenticating every HTTP request.

    EZAPP_MCP_API_KEY set: every request must present it (Authorization:
    Bearer or X-API-Key); comparison is constant-time (hmac.compare_digest).
    Unset: the endpoint is open (local development mode); `__main__` logs a
    loud warning at startup. This is a WRITE-capable server — never deploy
    it without a key.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        shared = os.environ.get(API_KEY_ENV, "").strip()
        if not shared:
            await self.app(scope, receive, send)
            return
        for candidate in _presented_api_keys(scope):
            if hmac.compare_digest(
                candidate.encode("utf-8"), shared.encode("utf-8")
            ):
                await self.app(scope, receive, send)
                return
        for message in _json_asgi_response(
            send, 401, _UNAUTHORIZED_BODY, [(b"www-authenticate", b"Bearer")]
        ):
            await send(message)


# ─── Read-only web UI (/ui/ — managed applications) ──────────────────────
#
# A static single-page shell served from ./ui next to the MCP endpoint. The
# shell itself is inert (no cluster data): the browser fetches /api/managed
# — behind the SAME API-key middleware as /mcp — which merges the ownership
# ledger with live EzAppConfig status. Everything renders as text; the key
# lives in sessionStorage only. Disable with EZAPP_MCP_UI_ENABLED=false.

_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
_UI_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
}
_UI_SECURITY_HEADERS = [
    (b"content-security-policy",
     b"default-src 'none'; style-src 'self'; script-src 'self'; "
     b"connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
     b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


def _ui_enabled() -> bool:
    return os.environ.get(UI_ENABLED_ENV, "").strip().lower() != "false"


async def _managed_snapshot() -> dict:
    """Ledger + live status for every EzAppConfig this server deployed."""
    ledger = _get_ledger()
    charts, apps = await ledger.load()
    resource = f"{_ezappconfig_plural()}.{_ezappconfig_group()}"

    async def fetch(name: str):
        rc, out, err = await _run_subprocess(
            ["kubectl", "get", resource, name, "-o", "json"],
            KUBECTL_TIMEOUT_SECONDS,
        )
        if rc != 0:
            if "NotFound" in err:
                return name, None, "not found (deleted out-of-band)"
            first_line = (err or f"kubectl exit {rc}").splitlines()[0]
            return name, None, f"lookup failed: {first_line}"
        try:
            doc = json.loads(out)
        except json.JSONDecodeError:
            return name, None, "lookup failed: unreadable status"
        spec = doc.get("spec") or {}
        status_obj = doc.get("status") or {}
        return name, {
            "chart": str(spec.get("name") or ""),
            "chart_version": str(spec.get("chartVersion") or ""),
            "target_namespace": str((spec.get("options") or {}).get("namespace") or ""),
            "install": spec.get("install"),
            "status": str(status_obj.get("status") or "unknown"),
            "failure_reason": str(status_obj.get("failureReason") or ""),
        }, None

    fetched = await asyncio.gather(*(fetch(n) for n in sorted(apps)))
    app_rows = []
    for name, live, error in fetched:
        entry = apps.get(name) or {}
        row = {
            "name": name,
            "applied_at": entry.get("applied_at"),
            "ledger_chart": entry.get("chart"),
            "ledger_chart_version": entry.get("chart_version"),
            "ledger_target_namespace": entry.get("target_namespace"),
        }
        if live is not None:
            row["live"] = live
        else:
            row["error"] = error
        app_rows.append(row)

    chart_rows = []
    for key in sorted(charts):
        chart_name, _, chart_version = key.partition("/")
        entry = charts.get(key) or {}
        chart_rows.append({
            "chart": chart_name,
            "version": chart_version,
            "uploaded_at": entry.get("uploaded_at"),
            "filename": entry.get("filename"),
        })

    return {
        "generated_at": _now_iso(),
        "ledger_configmap": ledger.name,
        "apps": app_rows,
        "charts": chart_rows,
    }


async def _managed_api(scope, receive, send):
    """GET /api/managed — the UI's one data call (API-key gated upstream)."""
    if not _ui_enabled():
        for message in _json_asgi_response(send, 404, b'{"error": "not found"}'):
            await send(message)
        return
    try:
        body = json.dumps(await _managed_snapshot(), indent=2).encode("utf-8")
    except LedgerError as e:
        body = json.dumps({"error": str(e)}).encode("utf-8")
        for message in _json_asgi_response(
            send, 503, body, [(b"cache-control", b"no-store")]
        ):
            await send(message)
        return
    for message in _json_asgi_response(
        send, 200, body, [(b"cache-control", b"no-store")]
    ):
        await send(message)


class _UiApp:
    """ASGI app serving the static shell under /ui (and / → /ui/)."""

    def __init__(self, root: str):
        self.root = os.path.realpath(root)

    async def _not_found(self, send):
        for message in _json_asgi_response(send, 404, b'{"error": "not found"}'):
            await send(message)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "/")
        if not _ui_enabled():
            await self._not_found(send)
            return
        if path in ("", "/"):
            for message in _json_asgi_response(
                send, 302, b"", [(b"location", b"/ui/"), (b"content-length", b"0")]
            ):
                await send(message)
            return
        if path != "/ui" and not path.startswith("/ui/"):
            await self._not_found(send)
            return
        rel = path[len("/ui"):].lstrip("/") or "index.html"
        candidate = os.path.realpath(os.path.join(self.root, rel))
        # Path-traversal guard: the resolved file must stay inside the UI
        # directory, no matter what the URL says.
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            await self._not_found(send)
            return
        if not os.path.isfile(candidate):
            await self._not_found(send)
            return
        try:
            with open(candidate, "rb") as f:
                body = f.read()
        except OSError:
            await self._not_found(send)
            return
        ext = os.path.splitext(candidate)[1].lower()
        ctype = _UI_CONTENT_TYPES.get(ext, "application/octet-stream").encode("ascii")
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [
                (b"content-type", ctype),
                (b"cache-control", b"no-cache"),
                (b"content-length", str(len(body)).encode("ascii")),
                *_UI_SECURITY_HEADERS,
            ],
        })
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    """The HTTP request body exceeded the chart size cap."""


class _BodyReadError(Exception):
    """The HTTP request body could not be fully read."""


async def _read_body_capped(receive, cap: int) -> bytes:
    """Read the full ASGI request body, refusing anything past `cap`."""
    chunks, size = [], 0
    while True:
        message = await receive()
        mtype = message.get("type", "")
        if mtype == "http.disconnect":
            raise _BodyReadError("client disconnected before the body arrived")
        if mtype != "http.request":
            continue
        chunk = message.get("body") or b""
        size += len(chunk)
        if size > cap:
            raise _BodyTooLarge(
                f"request body exceeds the {cap} byte cap "
                f"({MAX_CHART_MB_ENV}={_max_chart_bytes() // (1024 * 1024)})"
            )
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


async def _upload_http(scope, receive, send):
    """POST /upload — raw chart upload for payloads too big for MCP
    tool-call arguments. Same API-key middleware, same sniffing, size cap,
    ownership ledger and ChartMuseum pipeline as upload_chart; answers
    JSON so agents can curl it directly:
      curl -H "Authorization: Bearer <KEY>" --data-binary @chart.tgz \\
           https://<host>/upload[?force=true]
    """
    if (scope.get("method") or "").upper() != "POST":
        for message in _json_asgi_response(
            send, 405,
            b'{"error": "POST the raw .tgz body here", "method": "POST"}',
            [(b"allow", b"POST")],
        ):
            await send(message)
        return
    query = scope.get("query_string") or b""
    params = parse_qs(query.decode("latin-1"))
    force = params.get("force", ["false"])[0].strip().lower() == "true"
    try:
        data = await _read_body_capped(receive, _max_chart_bytes())
    except _BodyTooLarge as e:
        for message in _json_asgi_response(
            send, 413, json.dumps({"error": str(e)}).encode("utf-8"),
            [(b"cache-control", b"no-store")],
        ):
            await send(message)
        return
    except _BodyReadError as e:
        for message in _json_asgi_response(
            send, 400, json.dumps({"error": str(e)}).encode("utf-8"),
            [(b"cache-control", b"no-store")],
        ):
            await send(message)
        return
    if not data:
        for message in _json_asgi_response(
            send, 400, b'{"error": "empty body - POST the raw .tgz bytes"}',
            [(b"cache-control", b"no-store")],
        ):
            await send(message)
        return
    result = await _upload_chart_bytes(data, force, filename="http-upload")
    status_to_code = {
        "uploaded": 200, "exists": 409, "refused": 403,
        "unverifiable": 503, "invalid": 400, "error": 502,
    }
    payload = {k: result[k] for k in ("status", "chart", "version", "http_code", "detail")}
    for message in _json_asgi_response(
        send, status_to_code.get(result["status"], 502),
        json.dumps(payload).encode("utf-8"), [(b"cache-control", b"no-store")],
    ):
        await send(message)


async def _manifest_http(scope, receive, send):
    """POST /manifest — stage an EzAppConfig CR for server-side apply.

    For CRs too big for MCP tool-call arguments. Same API-key middleware;
    the body is validated up front (kind/apiVersion/name/namespace policy)
    so the agent learns about problems immediately, then staged in memory
    (single-use, 15-minute TTL). apply_ezappconfig(manifest_id=...) applies it.
      curl -sS -H "Authorization: Bearer <KEY>" --data-binary @cr.yaml \\
           https://<host>/manifest        -> {"manifest_id": "..."}
    """
    if (scope.get("method") or "").upper() != "POST":
        for message in _json_asgi_response(
            send, 405,
            b'{"error": "POST the raw EzAppConfig YAML here", "method": "POST"}',
            [(b"allow", b"POST")],
        ):
            await send(message)
        return
    try:
        data = await _read_body_capped(receive, MAX_MANIFEST_CHARS)
    except _BodyTooLarge as e:
        for message in _json_asgi_response(
            send, 413, json.dumps({"error": str(e)}).encode("utf-8"),
            [(b"cache-control", b"no-store")],
        ):
            await send(message)
        return
    except _BodyReadError as e:
        for message in _json_asgi_response(
            send, 400, json.dumps({"error": str(e)}).encode("utf-8"),
            [(b"cache-control", b"no-store")],
        ):
            await send(message)
        return
    try:
        manifest_yaml = data.decode("utf-8")
    except UnicodeDecodeError as e:
        for message in _json_asgi_response(
            send, 400,
            json.dumps({"error": f"body is not UTF-8 text: {e}"}).encode("utf-8"),
            [(b"cache-control", b"no-store")],
        ):
            await send(message)
        return
    try:
        doc = _validate_ezappconfig(manifest_yaml)
    except ValueError as e:
        for message in _json_asgi_response(
            send, 400,
            json.dumps({"error": f"invalid EzAppConfig: {e}"}).encode("utf-8"),
            [(b"cache-control", b"no-store")],
        ):
            await send(message)
        return
    _sweep_staged_manifests()
    if len(_staged_manifests) >= MANIFEST_STAGING_MAX:
        for message in _json_asgi_response(
            send, 503,
            json.dumps({"error": f"{MANIFEST_STAGING_MAX} manifests already "
                        "staged — apply them or let them expire first"}).encode("utf-8"),
            [(b"cache-control", b"no-store")],
        ):
            await send(message)
        return
    manifest_id = secrets.token_hex(16)
    _staged_manifests[manifest_id] = {
        "yaml": manifest_yaml,
        "name": doc["metadata"]["name"],
        "created": time.monotonic(),
    }
    print(
        f"AUDIT manifest staged id={manifest_id} name={doc['metadata']['name']} "
        f"bytes={len(data)}",
        flush=True,
    )
    body = {
        "manifest_id": manifest_id,
        "name": doc["metadata"]["name"],
        "expires_in_seconds": MANIFEST_STAGING_TTL_SECONDS,
        "next": "apply_ezappconfig(manifest_id=...)",
    }
    for message in _json_asgi_response(
        send, 200, json.dumps(body, indent=2).encode("utf-8"),
        [(b"cache-control", b"no-store")],
    ):
        await send(message)


class _ApiOrMcpApp:
    """Dispatches the authenticated chain: /api/managed → data API,
    /upload → raw chart upload, /manifest → EzAppConfig staging,
    everything else → MCP."""

    def __init__(self, mcp_app):
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/api/managed":
                await _managed_api(scope, receive, send)
                return
            if path == "/upload":
                await _upload_http(scope, receive, send)
                return
            if path == "/manifest":
                await _manifest_http(scope, receive, send)
                return
        await self.mcp_app(scope, receive, send)


class _UiRouterApp:
    """Routes /ui/* (and /) to the static shell, everything else to the
    API-key-authenticated chain. The shell carries no data, so serving it
    cannot bypass or weaken auth on /mcp or /api/managed."""

    def __init__(self, authed_app, ui_app):
        self.authed_app = authed_app
        self.ui_app = ui_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path in ("", "/", "/ui") or path.startswith("/ui/"):
                await self.ui_app(scope, receive, send)
                return
        await self.authed_app(scope, receive, send)


# ─── Entrypoint ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    if not os.environ.get(API_KEY_ENV, "").strip():
        print(
            "WARNING: EZAPP_MCP_API_KEY is not set — the MCP endpoint is "
            "UNAUTHENTICATED. This server DEPLOYS applications; configure a "
            "key (in-cluster: from a Secret) before exposing it.",
            flush=True,
        )
    if not _chartmuseum_configured():
        print(
            f"NOTE: {CHARTMUSEUM_URL_ENV} is not set; falling back to "
            f"{DEFAULT_CHARTMUSEUM_URL} (PCAI's in-cluster ChartMuseum).",
            flush=True,
        )
    print(
        "Web UI: read-only managed-apps view at /ui/ "
        + ("enabled" if _ui_enabled() else "DISABLED")
        + " (data endpoint /api/managed is API-key gated).",
        flush=True,
    )
    # Host-header (DNS-rebinding) protection: when MCP_HOSTNAME is set, keep
    # the SDK's protection ON but allowlist the real public hostname. The
    # SDK's implicit default (loopback-only) rejects every gateway-fronted
    # request. Without MCP_HOSTNAME (local dev), the SDK's implicit loopback
    # protection applies untouched.
    mcp_hostname = os.environ.get("MCP_HOSTNAME", "").strip()
    transport_security = None
    if mcp_hostname:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[mcp_hostname, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[f"https://{mcp_hostname}"],
        )
    # MCP 2.0 / protocol 2026-07-28 is stateless: no initialize handshake,
    # no Mcp-Session-Id header. stateless_http=True keeps 2025-era clients
    # served without a shared session store, so this can sit behind a plain
    # round-robin load balancer.
    mcp_app = mcp.streamable_http_app(
        stateless_http=True, transport_security=transport_security,
    )
    uvicorn.run(
        _UiRouterApp(
            _ApiKeyAuthMiddleware(_ApiOrMcpApp(mcp_app)),
            _UiApp(_UI_DIR),
        ),
        host="0.0.0.0",
        port=9090,
        log_level="info",
    )
