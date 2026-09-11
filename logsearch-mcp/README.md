# logsearch-mcp

Read-only, **namespace-scoped Kubernetes pod-log SEARCH** over MCP — the
*logs* half of cluster observability. Prometheus (the fleet's other
observability MCP) sees metrics: rates, trends, quantiles, alert state. This
server sees what the applications actually printed: tracebacks, panics, OOM
kills, crash loops. Built for agent harnesses: fan-out pod log search with
regex filtering, time bounds, per-line provenance, and hard caps so a noisy
namespace can never blow up an agent's context window.

**READ-ONLY by design.** Every tool declares `readOnlyHint=True`; the only
RBAC it needs is `pods` get/list + `pods/log` get. No secrets, no exec, no
write verbs, anywhere.

## Tools

All tools take a `namespace` first, enforce the namespace policy (below), and
return a JSON string. All are read-only.

| Tool | Contract |
| --- | --- |
| `list_log_sources(namespace, label_selector="")` | Pods + containers in one namespace with restart counts and age — the discovery call before searching. |
| `get_pod_logs(namespace, pod, container="", tail_lines=500, since_seconds=None, previous=False)` | Raw tail-bounded log fetch for ONE pod; `previous=true` reads the PREVIOUS (crashed) container — the first move on CrashLoopBackOff. |
| `search_logs(namespace, pattern, label_selector="", pod_regex="", since_minutes=None, tail_lines=200, case_insensitive=True, container="", max_total_lines=300)` | Fan-out regex search: tail of each pod (timestamps on), keep matching lines with `podname/container: ` provenance, merge chronologically, cap at `max_total_lines` (keeps the MOST RECENT matches when it bites, `truncated: true`). Returns `{matches, pods_searched, pods_with_matches, truncated, ...}`. |
| `count_matches(namespace, pattern, label_selector="", since_minutes=None, case_insensitive=True)` | Per-pod match counts over each pod's tail, `{pod: count}` sorted descending — the "where is this error coming from?" call before reading full logs. |

Errors are self-describing strings (`Error: ...`), never tracebacks: a denied
namespace names the policy env vars, a dead pod is reported per-pod while the
rest of the fan-out completes, an unreachable cluster says so. Empty results
are empty results, not errors.

## Configuration (environment variables, read lazily per call)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOGSEARCH_ALLOWED_NAMESPACES` | *(empty)* | Comma-separated fnmatch globs of allowed namespaces. **EMPTY = ALL namespaces allowed.** |
| `LOGSEARCH_BLOCKED_NAMESPACES` | *(empty)* | Comma-separated fnmatch globs; **always wins** over the allowed list. |
| `LOGSEARCH_MAX_PODS` | `50` | Max pods touched by one fan-out search/count. |
| `LOGSEARCH_MAX_LINES_PER_POD` | `1000` | Max tail lines fetched per pod (also the tail used by `count_matches`). |
| `LOGSEARCH_MAX_TOTAL_LINES` | `300` | Default cap on merged `search_logs` matches. |
| `LOGSEARCH_WEBUI_ENABLED` | `true` | Serve the web console (`/`, `/ui`, `/api/*`) on the streamable-http transport; `false` = MCP-only surface. |

Matching is `fnmatch`-style and case-sensitive (`prod-*` matches
`prod-eu-1`, not `prod`; namespace names are lowercase DNS labels).

## Web console

The streamable-http transport also serves a self-contained, HPE-branded log
search console (fleet pattern: prometheus-mcp's web UI — no CDN, no build
step, corporate-proxy safe):

| Path | Backed by |
| --- | --- |
| `/` (also `/ui`) | the single-file console (`ui/index.html`): search / counts / sources / MCP-tools tabs |
| `/api/status` | the effective namespace policy + caps (the same env vars the MCP error strings name) |
| `/api/sources?namespace=&label_selector=` | `list_log_sources` |
| `/api/search` (POST) | `search_logs` — matches with `pod/container: ` provenance, chronological, `truncated` flag |
| `/api/count` (POST) | `count_matches` — per-pod counts, descending |

The console is a human front-end with **no extra powers**: the endpoints
call the MCP tool coroutines themselves, so the namespace policy, the
pod/line caps, and the regex handling are byte-for-byte the server's own.
Errors arrive as the tools' clean strings, mapped to HTTP: denied namespace
→ 403, bad regex / bad input → 400, cluster-side failure → 502.
Gate it with `LOGSEARCH_WEBUI_ENABLED` (helm: `webui.enabled`).

## RBAC requirements

The chart ships a namespaced `Role` + `RoleBinding` (gated on
`rbac.create=true`) bound to a dedicated ServiceAccount:

```
pods       get, list    # discovery: names, containers, restarts, age
pods/log   get          # log reads (the only verb pods/log supports)
```

Release namespace only — no ClusterRole. Explicitly never granted: secrets,
configmaps, `pods/exec`, `pods/attach`, or any write verb.

## Trust model

- The server is read-only end to end; it cannot mutate cluster state and
  cannot execute anything in a container (no exec path exists).
- **Pod logs routinely contain sensitive strings** — tokens in stack traces,
  PII, internal hostnames — and matched lines are returned verbatim to the
  calling agent. The namespace policy
  (`LOGSEARCH_ALLOWED_NAMESPACES` / `LOGSEARCH_BLOCKED_NAMESPACES`) is the
  control that bounds what a harness can reach: set an explicit allow-list
  for anything beyond a lab cluster, and use the block-list to punch holes
  back out of a broad allow-list. Caps (`MAX_PODS` / `MAX_LINES_PER_POD` /
  `MAX_TOTAL_LINES`) bound how much of it leaves the cluster per call.
- Kubernetes access uses the pod's own service account in-cluster
  (`load_incluster_config`, falling back to `load_kube_config` locally).

## Run

```bash
# stdio (default — for local harness use)
logsearch-mcp

# streamable-http (the in-cluster deployment mode; stateless MCP 2.0)
logsearch-mcp --transport streamable-http --host 0.0.0.0 --port 9101
```

HTTP mode serves `/mcp` (MCP streamable-http, JSON responses, stateless — no
session affinity, any replica serves any request) plus `/health`, `/healthz`,
and — unless `LOGSEARCH_WEBUI_ENABLED=false` — the web console at `/` and its
`/api/*` endpoints. The session-manager lifespan wiring in `_build_http_app()`
is load-bearing: without it every `/mcp` request 500s with "Task group is not
initialized" even though the probes pass.

## Test

```bash
cd /home/andrew/Code/HPE/mcp_servers/logsearch_mcp && \
/home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/ -v
```

The unit-test venv has `mcp` but **no `kubernetes` package**: tests
monkeypatch the seam functions `server._list_pods` / `server._read_log`
directly (the kubernetes import stays lazy inside them), so the whole tool
surface is exercised without a cluster.

## Helm deploy

```bash
helm lint helm/
helm template test helm/ -f helm/local/values.example.yaml   # example render
helm upgrade --install logsearch-mcp helm/ -n <namespace> -f helm/local/values.<site>.yaml
```

Fleet conventions in the chart: Owner header + `imagePullSecrets` in
`values.yaml`; `hpe_proxies`/`proxy.*` (default false — this server's only
peer, the k8s API, is in-cluster and stays covered by the NO_PROXY
cluster-local entries); no `caCert` wiring (the in-cluster API uses the
service-account CA — there is no MITM egress to trust); a vendor-label
Kyverno policy gated on `ezua.enabled`.

Per-site values live in `helm/local/` (never committed, never packaged).

## Release / chores

`./automation.sh <version>` — bumps chart + package versions
(`bump_version.sh`), builds and pushes
`ghcr.io/ai-solution-eng/logsearch-mcp:v<version>` (buildx `--push`),
packages the chart (`helm-p`), prunes stale chart archives
(`prune_charts.py`). The tree is mirrored to
`pcai-solutions/mcp-servers/logsearch-mcp/` by the hardlinker
(`hardlinker.py --config hardlink_config.json --run`; config ships with
`dry_run: true` — mirrors are created only on an explicit `--run`).
