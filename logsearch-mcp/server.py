"""
LogSearch MCP Server

An MCP 2.0 server for read-only, namespace-scoped Kubernetes pod-log SEARCH —
the *logs* half of cluster observability that Prometheus cannot answer
(Prometheus sees metrics: rates, trends, alerts; this server sees what the
application actually printed: tracebacks, panics, OOM kills, crash loops).
Built for agent harnesses: fan-out pod log search with regex filtering, time
bounds, per-line provenance, and hard caps so a noisy namespace can never
blow up a context window. READ-ONLY by design — every tool is readOnlyHint,
and the RBAC it needs is pods get/list + pods/log get, nothing else.

Tools (all read-only, all return a JSON string):
  list_log_sources  pods + containers in one namespace, with restart counts
                    and age — the discovery call before searching
  get_pod_logs      raw log fetch for ONE pod (tail/since/previous-container)
  search_logs       regex search across every pod in a namespace, merged
                    chronologically with "pod/container: " provenance
  count_matches     per-pod match counts for one pattern, sorted descending

Configuration (environment variables, read lazily per call — flip policy or
caps in tests without reimporting):
  LOGSEARCH_ALLOWED_NAMESPACES  comma-separated fnmatch globs; EMPTY = ALL
                                namespaces allowed
  LOGSEARCH_BLOCKED_NAMESPACES  comma-separated fnmatch globs; ALWAYS wins
                                over the allowed list
  LOGSEARCH_MAX_PODS            max pods per fan-out search (default 50)
  LOGSEARCH_MAX_LINES_PER_POD   max tail lines fetched per pod (default 1000)
  LOGSEARCH_MAX_TOTAL_LINES     default cap on merged search matches
                                (default 300)
  LOGSEARCH_WEBUI_ENABLED       serve the web UI + /api/* routes on the
                                streamable-http transport (default true)
"""

import argparse
import ast
import asyncio
import fnmatch
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------------------
# Configuration — lazy env reads (a test flips env vars, not module globals)
# ---------------------------------------------------------------------------

ENV_ALLOWED = "LOGSEARCH_ALLOWED_NAMESPACES"
ENV_BLOCKED = "LOGSEARCH_BLOCKED_NAMESPACES"
ENV_WEBUI_ENABLED = "LOGSEARCH_WEBUI_ENABLED"

DEFAULT_MAX_PODS = 50
DEFAULT_MAX_LINES_PER_POD = 1000
DEFAULT_MAX_TOTAL_LINES = 300

# Hard ceiling on any single log body handed to a caller, regardless of
# tail_lines: one pod can emit megabyte lines, and the MCP response is the
# agent's context window.
_MAX_OUTPUT_CHARS = 100_000

# Kubernetes log timestamps are RFC3339 with NANOSECOND precision
# (2026-09-08T00:00:00.123456789Z); match the timestamp prefix wherever it
# sits in the line. Naive (offset-less) timestamps are tolerated by the
# parser and sorted as UTC.
_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _env_csv(name: str) -> list[str]:
    """Parse a comma-separated env list into clean glob patterns."""
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _max_pods() -> int:
    return _env_int("LOGSEARCH_MAX_PODS", DEFAULT_MAX_PODS)


def _max_lines_per_pod() -> int:
    return _env_int("LOGSEARCH_MAX_LINES_PER_POD", DEFAULT_MAX_LINES_PER_POD)


def _default_max_total_lines() -> int:
    return _env_int("LOGSEARCH_MAX_TOTAL_LINES", DEFAULT_MAX_TOTAL_LINES)


def _webui_enabled() -> bool:
    """Whether the HTTP transport should also serve the web UI + /api/* JSON
    routes (webui.py). Read lazily like every other knob so a test can flip
    it without reimporting; unset/true-ish = enabled, false-ish = the plain
    /health + /mcp surface."""
    raw = os.getenv(ENV_WEBUI_ENABLED, "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _namespace_allowed(ns: str) -> bool:
    """Pure namespace-policy predicate: blocked ALWAYS wins, an empty
    allowed-list means everything is allowed, otherwise fnmatch glob match.

    fnmatchcase (not fnmatch) so matching is identical on every platform —
    namespace names are DNS labels and always lowercase.
    """
    for pattern in _env_csv(ENV_BLOCKED):
        if fnmatch.fnmatchcase(ns, pattern):
            return False
    allowed = _env_csv(ENV_ALLOWED)
    if not allowed:
        return True
    return any(fnmatch.fnmatchcase(ns, pattern) for pattern in allowed)


def _ns_denied_error(ns: str) -> str:
    """Self-describing denial: names the env vars an operator must change."""
    return (
        f"Error: namespace {ns!r} is denied by this server's namespace policy "
        f"({_ENV_POLICY_HINT})."
    )


_ENV_POLICY_HINT = (
    f"allowed={ENV_ALLOWED}, blocked={ENV_BLOCKED}; blocked always wins, "
    "empty allowed-list means all namespaces"
)


class LogSearchError(Exception):
    """Self-describing failure surfaced to the caller as an 'Error:' string —
    never a traceback (pod gone, namespace gone, API down, bad regex...)."""


# ---------------------------------------------------------------------------
# Kubernetes seams — the ONLY places that touch the kubernetes client.
# Module-level functions so tests monkeypatch them directly (the test venv
# has NO kubernetes package; the import must stay lazy, inside the functions).
# ---------------------------------------------------------------------------

_core_v1_api = None


def _get_core_v1_api():
    """Lazy kubernetes import + client init, cached for the process life."""
    global _core_v1_api
    if _core_v1_api is None:
        from kubernetes import client, config  # heavy dep; absent in the test venv

        try:
            config.load_incluster_config()  # in-cluster: service account
        except config.ConfigException:
            config.load_kube_config()  # local dev / port-forwarded kubeconfig
        _core_v1_api = client.CoreV1Api()
    return _core_v1_api


def _list_pods(namespace: str, label_selector: str = "") -> list[dict]:
    """List pods in one namespace as plain dicts:
    {name, containers: [str], restarts: int, started: str}.

    restarts is the SUM over app containers (a crashed sidecar restart loop
    is triage-relevant, not noise). started is the pod start time (RFC3339)
    or '' when the API did not report one.
    """
    from kubernetes.client.rest import ApiException

    api = _get_core_v1_api()
    try:
        resp = api.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector or ""
        )
    except ApiException as e:
        status = getattr(e, "status", None)
        if status == 404:
            raise LogSearchError(
                f"namespace {namespace!r} not found (404) — check the spelling "
                "or list namespaces with a cluster-wide tool."
            ) from e
        raise LogSearchError(
            f"Kubernetes API error listing pods in namespace {namespace!r} "
            f"(status {status}): {getattr(e, 'reason', '') or e}"
        ) from e
    except Exception as e:  # config load failure, connection refused, ...
        raise LogSearchError(
            f"Kubernetes cluster unreachable while listing pods in namespace "
            f"{namespace!r}: {type(e).__name__}: {e}"
        ) from e

    pods = []
    for pod in resp.items or []:
        spec = pod.spec
        status = pod.status
        containers = [c.name for c in (spec.containers or [])] if spec else []
        restarts = 0
        if status and status.container_statuses:
            restarts = sum(int(cs.restart_count or 0) for cs in status.container_statuses)
        started = ""
        if status and status.start_time:
            started = status.start_time.isoformat().replace("+00:00", "Z")
        pods.append(
            {"name": pod.metadata.name, "containers": containers, "restarts": restarts, "started": started}
        )
    return pods


def _decode_log_payload(raw) -> str:
    """Normalize a pod-log API payload to a real UTF-8 string with newlines.

    Two client quirks are handled (both found live in v0.1.0/v0.1.1):
    - raw BYTES payloads (preload=False .data) are decoded defensively;
    - the client's DEFAULT preload path returns the repr of the bytes — a
      str like "b'...\\n...'" with literal backslash-n — recovered via
      ast.literal_eval so per-line search and timestamp parsing work.
    """
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.startswith("b'") and raw.endswith("'"):
        try:
            recovered = ast.literal_eval(raw)
            if isinstance(recovered, bytes):
                return recovered.decode("utf-8", errors="replace")
        except (ValueError, SyntaxError):
            pass  # a log that genuinely reads like a bytes literal — keep as-is
    return raw if isinstance(raw, str) else str(raw)


def _read_log(
    namespace: str,
    pod: str,
    container: str,
    tail_lines: int,
    since_seconds: int | None = None,
    timestamps: bool = True,
    previous: bool = False,
) -> str:
    """Read one container's log (tail-bounded). timestamps=True prepends the
    RFC3339 timestamp every search relies on for chronological ordering;
    previous=True reads the PREVIOUS (crashed/restarted) container — the
    triage path for CrashLoopBackOff, where the current container may have
    nothing but 'back-off restarting'."""
    from kubernetes.client.rest import ApiException

    api = _get_core_v1_api()
    kwargs: dict = {"tail_lines": tail_lines, "timestamps": timestamps, "previous": previous}
    if container:
        kwargs["container"] = container
    if since_seconds:
        kwargs["since_seconds"] = int(since_seconds)
    try:
        # _preload_content=False: the client's default preload path returns
        # the log as the REPR of its bytes — a str like "b'...\\n...'" with
        # literal backslash-n and zero real newlines (verified 2026-09-11
        # against a controlled API: the whole log collapsed to one line).
        # Raw bytes via .data are honest; decode them ourselves.
        resp = api.read_namespaced_pod_log(
            name=pod, namespace=namespace, _preload_content=False, **kwargs
        )
        return _decode_log_payload(resp.data)
    except ApiException as e:
        status = getattr(e, "status", None)
        if status == 404:
            raise LogSearchError(
                f"pod {pod!r} not found in namespace {namespace!r} (404) — it may "
                "have been rescheduled mid-search; use list_log_sources to refresh."
            ) from e
        if status == 400:
            raise LogSearchError(
                f"log request for pod {pod!r} in namespace {namespace!r} rejected "
                "(400): a multi-container pod needs an explicit container name — "
                "use list_log_sources to see the container list."
            ) from e
        raise LogSearchError(
            f"Kubernetes API error reading log of pod {pod!r} in namespace "
            f"{namespace!r} (status {status}): {getattr(e, 'reason', '') or e}"
        ) from e
    except Exception as e:
        raise LogSearchError(
            f"Kubernetes cluster unreachable while reading logs of pod {pod!r} "
            f"in namespace {namespace!r}: {type(e).__name__}: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

mcp = MCPServer("logsearch-mcp")

print("LogSearch MCP Server initialized:", file=sys.stderr)
print(
    f"  namespace policy: allowed={os.getenv(ENV_ALLOWED, '') or '<all>'} "
    f"blocked={os.getenv(ENV_BLOCKED, '') or '<none>'} (re-read per call)",
    file=sys.stderr,
)
print(
    f"  caps: max_pods={_max_pods()} max_lines_per_pod={_max_lines_per_pod()} "
    f"max_total_lines={_default_max_total_lines()}",
    file=sys.stderr,
)

_mcp_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _err(context: str, exc: Exception) -> str:
    """One error shape for every tool: self-describing for expected failures
    (LogSearchError), a stderr traceback plus type summary for the rest."""
    if isinstance(exc, LogSearchError):
        return f"Error: {exc}"
    traceback.print_exc(file=sys.stderr)
    return f"Error: {context} failed: {type(exc).__name__}: {exc}"


def _compile_regex(pattern: str, what: str = "pattern", flags: int = 0) -> re.Pattern:
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        # Re-raise as LogSearchError so the CALLER sees the bad pattern and
        # why it is bad, instead of a traceback.
        raise LogSearchError(f"invalid regex {what} {pattern!r} ({e})") from e


def _since_seconds_from_minutes(since_minutes: float | None) -> int | None:
    if since_minutes is None:
        return None
    if since_minutes <= 0:
        raise LogSearchError("since_minutes must be > 0 (e.g. 30 = last half hour).")
    return max(1, int(round(since_minutes * 60)))


def _pick_container(pod: dict, container: str) -> str | None:
    """Resolve which container of a pod to read: the requested one (only when
    the pod actually has it) or the pod's first container — the Kubernetes
    default for an unspecified container. None = nothing to read."""
    containers = pod.get("containers") or []
    if container:
        return container if container in containers else None
    return containers[0] if containers else None


def _rfc3339_sort_key(line: str) -> tuple:
    """Sort key over the RFC3339 timestamp embedded in a (provenance-prefixed)
    log line: timestamped lines first, oldest → newest; lines without a
    parseable timestamp keep their relative order at the END."""
    match = _RFC3339_RE.search(line)
    if not match:
        return (1, 0.0)
    try:
        epoch = _rfc3339_to_epoch(match.group(0))
        return (0, epoch)
    except ValueError:
        return (1, 0.0)


def _rfc3339_to_epoch(ts: str) -> float:
    # Normalize the two things fromisoformat cannot take everywhere: the 'Z'
    # suffix and >6-digit fractional seconds (k8s emits nanoseconds).
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", ts.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _human_age(started: str) -> str:
    """Pod age as a compact human string, from the RFC3339 start time."""
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        secs = max(0, int((datetime.now(timezone.utc) - start).total_seconds()))
    except ValueError:
        return "unknown"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Tools — every tool: policy check → offload blocking k8s calls via
# asyncio.to_thread → JSON string. All read-only.
# ---------------------------------------------------------------------------


@mcp.tool(
    title="List Log Sources",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def list_log_sources(namespace: str, label_selector: str = "") -> str:
    """List pods+containers in one namespace with restart counts and age —
    the discovery call before searching (which pods exist, which containers
    to name in search_logs/get_pod_logs). Respects the namespace policy.

    Args:
        namespace: Kubernetes namespace to inspect.
        label_selector: Optional label selector, e.g. 'app=myapp'.
    """
    if not _namespace_allowed(namespace):
        return _ns_denied_error(namespace)
    try:
        pods = await asyncio.to_thread(_list_pods, namespace, label_selector)
    except Exception as e:
        return _err("pod listing", e)
    cap = _max_pods()
    pod_cap_applied = len(pods) > cap
    shown = sorted(pods, key=lambda p: p["name"])[:cap]
    for pod in shown:
        pod["age"] = _human_age(pod.get("started", ""))
    return json.dumps(
        {
            "namespace": namespace,
            "label_selector": label_selector,
            "pod_count": len(pods),
            "pod_cap_applied": pod_cap_applied,
            "pods": shown,
        }
    )


@mcp.tool(
    title="Get Pod Logs",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def get_pod_logs(
    namespace: str,
    pod: str,
    container: str = "",
    tail_lines: int = 500,
    since_seconds: int | None = None,
    previous: bool = False,
) -> str:
    """Fetch the log of ONE pod's container (tail-bounded, newest last).
    previous=true reads the PREVIOUS (crashed) container — the first move
    when a pod is in CrashLoopBackOff and the current container has nothing
    to say. Output is length-capped.

    Args:
        namespace: Kubernetes namespace of the pod.
        pod: Pod name (discover with list_log_sources).
        container: Container name; empty = the pod's first (default) container.
        tail_lines: How many trailing lines to fetch (server-capped by
            LOGSEARCH_MAX_LINES_PER_POD).
        since_seconds: Optional: only lines newer than this many seconds.
        previous: Read the previous (crashed/restarted) container instead of
            the running one.
    """
    if not _namespace_allowed(namespace):
        return _ns_denied_error(namespace)
    if tail_lines <= 0:
        return "Error: tail_lines must be > 0."
    if since_seconds is not None and since_seconds <= 0:
        return "Error: since_seconds must be > 0 when given."
    effective_tail = min(tail_lines, _max_lines_per_pod())
    try:
        target_container = container
        if not target_container:
            # Resolve the pod's default container up front so multi-container
            # pods fail into a helpful message instead of an API 400.
            pods = await asyncio.to_thread(_list_pods, namespace, "")
            match = next((p for p in pods if p["name"] == pod), None)
            if match is None:
                return (
                    f"Error: pod {pod!r} not found in namespace {namespace!r} — "
                    "use list_log_sources to discover pods."
                )
            target_container = _pick_container(match, "")
            if target_container is None:
                return f"Error: pod {pod!r} reports no containers to read."
        log_text = await asyncio.to_thread(
            _read_log,
            namespace,
            pod,
            target_container,
            effective_tail,
            since_seconds,
            True,
            previous,
        )
    except Exception as e:
        return _err("pod log fetch", e)
    # Char-cap AFTER the fetch: the tail cap bounds line count, this bounds
    # the response even when individual lines are enormous.
    truncated = False
    if len(log_text) > _MAX_OUTPUT_CHARS:
        cut = log_text.rfind("\n", 0, _MAX_OUTPUT_CHARS)
        log_text = log_text[: cut if cut > 0 else _MAX_OUTPUT_CHARS]
        truncated = True
    lines = log_text.splitlines()
    return json.dumps(
        {
            "namespace": namespace,
            "pod": pod,
            "container": target_container,
            "previous": previous,
            "tail_lines": effective_tail,
            "lines_returned": len(lines),
            "truncated": truncated,
            "log": log_text,
        }
    )


@mcp.tool(
    title="Search Pod Logs",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def search_logs(
    namespace: str,
    pattern: str,
    label_selector: str = "",
    pod_regex: str = "",
    since_minutes: float | None = None,
    tail_lines: int = 200,
    case_insensitive: bool = True,
    container: str = "",
    max_total_lines: int = 300,
) -> str:
    """Fan-out regex search over the pods of one namespace: fetch the tail of
    each pod (timestamps on), keep matching lines, prefix each with
    'podname/container: ' provenance, merge, and sort chronologically by the
    embedded RFC3339 timestamp (untimestamped lines last). Capped at
    max_total_lines — when the cap bites, the MOST RECENT matches are kept
    and 'truncated' is true. Empty matches are a normal empty result, not an
    error.

    Args:
        namespace: Kubernetes namespace to search.
        pattern: Python regex to match against log lines, e.g. 'Traceback|ERROR'.
        label_selector: Optional pod label selector, e.g. 'app=myapp'.
        pod_regex: Optional regex narrowing WHICH pods to search by name.
        since_minutes: Optional: only fetch logs newer than this many minutes.
        tail_lines: Lines fetched per pod (server-capped by
            LOGSEARCH_MAX_LINES_PER_POD).
        case_insensitive: Match pattern case-insensitively (default true).
        container: Restrict to one container name; empty = each pod's first
            (default) container.
        max_total_lines: Cap on merged matches returned (default 300).
    """
    if not _namespace_allowed(namespace):
        return _ns_denied_error(namespace)
    try:
        rx = _compile_regex(pattern, flags=re.IGNORECASE if case_insensitive else 0)
        pod_rx = _compile_regex(pod_regex, what="pod_regex") if pod_regex else None
        since_seconds = _since_seconds_from_minutes(since_minutes)
    except LogSearchError as e:
        return f"Error: {e}"
    if tail_lines <= 0:
        return "Error: tail_lines must be > 0."
    if max_total_lines <= 0:
        return "Error: max_total_lines must be > 0."
    effective_tail = min(tail_lines, _max_lines_per_pod())

    try:
        pods = await asyncio.to_thread(_list_pods, namespace, label_selector)
    except Exception as e:
        return _err("pod listing", e)

    cap = _max_pods()
    pod_cap_applied = len(pods) > cap
    pods = pods[:cap]
    if pod_rx is not None:
        pods = [p for p in pods if pod_rx.search(p["name"])]

    matches: list[str] = []
    errors: list[str] = []
    pods_searched = 0
    pods_with_matches = 0
    for pod in pods:
        target_container = _pick_container(pod, container)
        if target_container is None:
            continue
        try:
            log_text = await asyncio.to_thread(
                _read_log,
                namespace,
                pod["name"],
                target_container,
                effective_tail,
                since_seconds,
                True,  # timestamps: what chronological merge sort runs on
                False,
            )
        except LogSearchError as e:
            # One dead pod must not sink the search: record, keep going.
            errors.append(f"{pod['name']}: {e}")
            continue
        pods_searched += 1
        prefix = f"{pod['name']}/{target_container}: "
        pod_matches = [prefix + line for line in log_text.splitlines() if rx.search(line)]
        if pod_matches:
            pods_with_matches += 1
            matches.extend(pod_matches)

    matches.sort(key=_rfc3339_sort_key)
    truncated = len(matches) > max_total_lines
    if truncated:
        # Chronological sort + cap: keep the TAIL (most recent) — triage
        # cares about what happened last, not what happened first.
        matches = matches[-max_total_lines:]
    return json.dumps(
        {
            "namespace": namespace,
            "pattern": pattern,
            "matches": matches,
            "match_count": len(matches),
            "pods_searched": pods_searched,
            "pods_with_matches": pods_with_matches,
            "pod_cap_applied": pod_cap_applied,
            "truncated": truncated,
            "errors": errors,
        }
    )


@mcp.tool(
    title="Count Log Matches",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def count_matches(
    namespace: str,
    pattern: str,
    label_selector: str = "",
    since_minutes: float | None = None,
    case_insensitive: bool = True,
) -> str:
    """Per-pod match counts for one pattern across a namespace, sorted
    descending — the 'where is this error coming from?' call before reading
    full logs. Counts run over the last LOGSEARCH_MAX_LINES_PER_POD lines of
    each pod's default container.

    Args:
        namespace: Kubernetes namespace to count in.
        pattern: Python regex to match against log lines, e.g. 'OutOfMemory'.
        label_selector: Optional pod label selector, e.g. 'app=myapp'.
        since_minutes: Optional: only fetch logs newer than this many minutes.
        case_insensitive: Match pattern case-insensitively (default true).
    """
    if not _namespace_allowed(namespace):
        return _ns_denied_error(namespace)
    try:
        rx = _compile_regex(pattern, flags=re.IGNORECASE if case_insensitive else 0)
        since_seconds = _since_seconds_from_minutes(since_minutes)
    except LogSearchError as e:
        return f"Error: {e}"

    try:
        pods = await asyncio.to_thread(_list_pods, namespace, label_selector)
    except Exception as e:
        return _err("pod listing", e)

    cap = _max_pods()
    pod_cap_applied = len(pods) > cap
    pods = pods[:cap]

    counts: dict[str, int] = {}
    errors: list[str] = []
    pods_searched = 0
    for pod in pods:
        target_container = _pick_container(pod, "")
        if target_container is None:
            continue
        try:
            log_text = await asyncio.to_thread(
                _read_log,
                namespace,
                pod["name"],
                target_container,
                _max_lines_per_pod(),
                since_seconds,
                True,
                False,
            )
        except LogSearchError as e:
            errors.append(f"{pod['name']}: {e}")
            continue
        pods_searched += 1
        counts[pod["name"]] = sum(1 for line in log_text.splitlines() if rx.search(line))

    # Sorted descending by count (pod name breaks ties deterministically);
    # JSON preserves insertion order, so clients see the ranking directly.
    ranked = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return json.dumps(
        {
            "namespace": namespace,
            "pattern": pattern,
            "counts": ranked,
            "pods_searched": pods_searched,
            "pods_with_matches": sum(1 for v in ranked.values() if v > 0),
            "total_matches": sum(ranked.values()),
            "pod_cap_applied": pod_cap_applied,
            "errors": errors,
        }
    )


def _build_http_app():
    """Starlette app for the streamable-http transport.

    CRITICAL: the MCP session manager needs its lifespan to run (it starts
    the task group that serves /mcp requests) — even though MCP 2.0 is
    natively STATELESS (no initialize handshake, no Mcp-Session-Id, any
    replica serves any request). Mounting only the routes without
    ``lifespan=http_app.router.lifespan_context`` yields a server whose
    probes pass but where EVERY /mcp request 500s with "Task group is not
    initialized" — a real fleet bug; do not "simplify" this away.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(_request):
        return JSONResponse({"status": "ok", "server": "logsearch-mcp"})

    # json_response=True keeps plain-HTTP clients on single responses;
    # stateless_http=True drops session affinity entirely.
    http_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=_mcp_transport_security,
    )
    routes: list = [
        Route("/health", health),
        Route("/healthz", health),
    ]
    if _webui_enabled():
        # HPE-branded web UI + read-only JSON API at / and /api/* — a human
        # front-end over the SAME seams/policy/caps that back the MCP tools
        # (webui.py calls the tool coroutines; it has no extra powers).
        # Gated by LOGSEARCH_WEBUI_ENABLED (helm: webui.enabled), default on.
        from webui import build_ui_routes

        routes.extend(build_ui_routes())
    routes.extend(http_app.routes)
    return Starlette(
        routes=routes,
        lifespan=http_app.router.lifespan_context,
    )


def main():
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    parser = argparse.ArgumentParser(description="LogSearch MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    app = _build_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )
    print(f"LogSearch MCP streamable-http endpoint: http://{args.host}:{args.port}/mcp")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
