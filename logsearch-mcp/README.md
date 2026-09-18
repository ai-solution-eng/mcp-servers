# logsearch-mcp

Read-only, **namespace-scoped Kubernetes pod-log SEARCH** over MCP — the
*logs* half of cluster observability. Prometheus (the fleet's other
observability MCP) sees metrics: rates, trends, quantiles, alert state. This
server sees what the applications actually printed: tracebacks, panics, OOM
kills, crash loops. Built for agent harnesses: fan-out pod log search with
regex filtering, time bounds, per-line provenance, and hard caps so a noisy
namespace can never blow up an agent's context window.

**READ-ONLY by design.** Every tool except `export_matches` declares
`readOnlyHint=True`; the only RBAC it needs is `pods` get/list +
`pods/log` get. No secrets, no exec, no write verbs, anywhere.
`export_matches` is the one honest exception (`readOnlyHint=False`): it
writes ONLY the export file into the operator-configured
`LOGSEARCH_EXPORT_ROOT` directory (opt-in — unset by default, the tool
refuses; see "Exports" below). The cluster side stays read-only.

## Tools

All tools take a `namespace` first, enforce the namespace policy (below), and
return a JSON string. All are read-only against the cluster.

| Tool | Contract |
| --- | --- |
| `list_log_sources(namespace, label_selector="")` | Pods + containers in one namespace with restart counts and age — the discovery call before searching. |
| `get_pod_logs(namespace, pod, container="", tail_lines=500, since_seconds=None, previous=False)` | Raw tail-bounded log fetch for ONE pod; `previous=true` reads the PREVIOUS (crashed) container — the first move on CrashLoopBackOff. |
| `search_logs(namespace, pattern, label_selector="", pod_regex="", since_minutes=None, tail_lines=200, case_insensitive=True, container="", max_total_lines=300)` | Fan-out regex search: pods are fetched in PARALLEL (bounded semaphore, `LOGSEARCH_FETCH_CONCURRENCY`); tail of each pod (timestamps on); keep matching lines with `podname/container: ` provenance; merge chronologically. The `max_total_lines` budget is enforced DURING the fan-out — once it is full the search stops pulling further pods (`pods_skipped_budget` counts them, `truncated: true`). Each returned line is capped at `LOGSEARCH_MAX_LINE_CHARS` characters with a `...[truncated N chars]` marker. Returns `{matches, pods_searched, pods_with_matches, truncated, pods_skipped_budget, ...}`. |
| `count_matches(namespace, pattern, label_selector="", since_minutes=None, case_insensitive=True)` | Per-pod match counts over each pod's tail, `{pod: count}` sorted descending — the "where is this error coming from?" call before reading full logs. Pod fetches run in parallel (same bounded semaphore). |
| `export_matches(namespace, pattern, dest_name, ...same params as search_logs)` | **OPT-IN (Wave-5 F3)** — runs the EXACT `search_logs` pipeline and writes the matched lines to `<LOGSEARCH_EXPORT_ROOT>/<dest_name>`: for result sets too big for a context window, the agent reads the FILE back instead. With `LOGSEARCH_EXPORT_ROOT` unset the tool refuses with setup instructions and writes nothing. `dest_name` must be a bare file name (no separators/`..`); the write is bounded by the same caps as the search and the file gets a small `#`-prefixed header. See "Exports". |

Errors are self-describing strings (`Error: ...`), never tracebacks: a denied
namespace names the policy env vars, a dead pod is reported per-pod while the
rest of the fan-out completes, an unreachable cluster says so. Empty results
are empty results, not errors.

## Configuration (environment variables, read lazily per call)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOGSEARCH_ALLOWED_NAMESPACES` | *(empty)* | Comma-separated fnmatch globs of allowed namespaces. **EMPTY = DENY ALL namespaces** (default-deny, fleet decision D8) — unless `LOGSEARCH_EMPTY_ALLOWS_ALL=1`. |
| `LOGSEARCH_EMPTY_ALLOWS_ALL` | `false` | **D8 escape hatch**: `1` (or `true`/`yes`/`on`) restores the pre-D8 open default where an empty allowlist means every namespace is searchable. |
| `LOGSEARCH_BLOCKED_NAMESPACES` | *(empty)* | Comma-separated fnmatch globs; **always wins** over the allowed list. |
| `LOGSEARCH_MAX_PODS` | `50` | Max pods touched by one fan-out search/count. |
| `LOGSEARCH_MAX_LINES_PER_POD` | `1000` | Max tail lines fetched per pod (also the tail used by `count_matches`). |
| `LOGSEARCH_MAX_TOTAL_LINES` | `300` | Default cap on merged `search_logs` matches — enforced DURING the fan-out (the pull stops when the budget is full). |
| `LOGSEARCH_MAX_LINE_CHARS` | `2000` | Per-line char cap on `search_logs` output; overlong lines are cut with an explicit `...[truncated N chars]` marker. `0` disables. |
| `LOGSEARCH_FETCH_CONCURRENCY` | `8` | Width of the parallel pod-fetch fan-out (asyncio.gather under a bounded semaphore); `1` = sequential. |
| `LOGSEARCH_MAX_REGEX_CHARS` | `512` | Max user-regex length before the ReDoS screen refuses it; `0` disables the length cap (the shape screen stays on). |
| `LOGSEARCH_WEBUI_ENABLED` | `true` | Serve the web console (`/`, `/ui`, `/api/*`) on the streamable-http transport; `false` = MCP-only surface. |
| `LOGSEARCH_EXPORT_ROOT` | *(unset)* | **Opt-in directory** `export_matches` may write into (`<root>/<dest_name>`). UNSET by default = the tool refuses with a self-describing setup message and writes nothing. Set it to an absolute directory on a writable, agent-readable volume — fleet convention: a path on the workbench/shared PVC so workbench workspaces can read exports back (see "Exports"). Chart: `logsearch.exportRoot` (rendered only when set). |
| `LOGSEARCH_METRICS_ENABLED` | `false` | Serve `GET /metrics` — Prometheus self-metrics: `logsearch_mcp_tool_requests_total{tool,outcome}` (per-tool call counts, ok/error incl. namespace-policy denials; no namespace/pod names, patterns, or log content are exported). Chart-gated default-OFF (`metrics.enabled: false` renders no env and no ServiceMonitor); when on, `/metrics` is key-free like the probes. See `helm/values.yaml` (which also documents the fleet audit-JSONL searchability convention — this server is the SEARCH side of it). |

Matching is `fnmatch`-style and case-sensitive (`prod-*` matches
`prod-eu-1`, not `prod`; namespace names are lowercase DNS labels).

## Regex safety (ReDoS guard)

User regexes are screened at compile time, BEFORE they ever touch a log line:
a pattern whose backtracking can explode — the classic nested-quantifier
shapes (`(a+)+`, `(.*)*`, `(\w+\s)*`, `(a|aa)+`) — would otherwise hold the
GIL and freeze the whole server (every worker, every request) while `re`
backtracks. The screen rejects such patterns in microseconds with a clean
`unsafe regex ... rejected: ...` error that explains the rewrite (quantify
plain character classes, not groups that already contain quantifiers; or make
the inner quantifier atomic/possessive: `(?>a+)+`, `a++`). Patterns longer
than `LOGSEARCH_MAX_REGEX_CHARS` are refused outright. Realistic log patterns
are unaffected (`ERROR|Traceback`, `\d+(?:\.\d+)*`, `(?:\d{1,3}\.){3}`,
`(?:[a-f0-9]{2}){8}` all compile as-is). If the optional `re2` package is
installed in the serving image it is preferred for matching (linear-time
engine, belt-and-braces); the screen runs either way and nothing installs it.

## Exports (export_matches — Wave-5 F3, opt-in)

`export_matches(namespace, pattern, dest_name, ...)` answers one need: a
fan-out search whose results would blow the calling agent's context window.
It runs the **EXACT `search_logs` pipeline** — same namespace policy (D8),
same ReDoS screen, same pod/line caps, byte-identical matched lines and
counts; there is no second search path to bypass — and writes the matched
lines to `<LOGSEARCH_EXPORT_ROOT>/<dest_name>`:

```
# logsearch export_matches
# timestamp: 2026-09-13T22:00:00Z
# namespace: payments-prod
# pattern: Traceback|ERROR
# matches: 42 (truncated: false)
# pods_searched: 12  pods_with_matches: 4  pods_skipped_budget: 0  pod_cap_applied: false
# errors: none

payments-api-7/main: 2026-09-13T21:59:58Z ERROR ...
```

- **Opt-in**: `LOGSEARCH_EXPORT_ROOT` unset (the default) → the tool refuses
  with a self-describing setup message and writes nothing.  Suggested value
  (also in `helm/values.yaml`): a directory on the workbench/shared PVC —
  e.g. `LOGSEARCH_EXPORT_ROOT=/data/exports` on the logsearch pod plus
  `WORKBENCH_SHARED_PATHS=/data/exports` on the workbench pod — so an agent
  reads the export back through its workbench tools.  The directory is
  created on first export if missing.
- **Path safety**: `dest_name` must be a bare file name — no `/` or `\`
  separators, no `..` anywhere, no leading dot, no whitespace/control
  characters (clear error otherwise; the export always lands directly under
  the root).  A hostile pattern cannot forge header lines (fields are
  flattened to one line each).
- **Bounded writes**: the file holds at most `max_total_lines` matches, each
  already char-capped by the search's `LOGSEARCH_MAX_LINE_CHARS` — the same
  caps the MCP response would carry, nothing more.  Writes are atomic
  (temp file + rename): a reader never sees a partial export, and re-exporting
  to the same name REPLACES it (never appends).
- **Failure posture**: a denied namespace, a refused regex, or bad arguments
  return the very same `Error: ...` string `search_logs` would — and write
  NOTHING.  The tool's response carries counts and the file path, never the
  matches themselves (the file is the artifact).  It is the fleet's one
  honest `readOnlyHint=False` tool here: its only write is that export file.

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

The chart ships a read-only grant bound to a dedicated ServiceAccount
(gated on `rbac.create=true`). Scope is chosen by `rbac.clusterWide`:

```
pods       get, list    # discovery: names, containers, restarts, age
pods/log   get          # log reads (the only verb pods/log supports)
```

- `rbac.clusterWide: false` (default) — namespaced `Role` + `RoleBinding`,
  release namespace only: the release's blast radius is exactly one
  namespace.
- `rbac.clusterWide: true` — the SAME two rules as a `ClusterRole` +
  `ClusterRoleBinding`: cluster-wide reads (lab/trusted clusters; the
  namespace policy stays the agent-facing gate).

Explicitly never granted in either mode: secrets, configmaps, `pods/exec`,
`pods/attach`, or any write verb.

## Trust model

- The server is read-only end to end; it cannot mutate cluster state and
  cannot execute anything in a container (no exec path exists).
- **Pod logs routinely contain sensitive strings** — tokens in stack traces,
  PII, internal hostnames — and matched lines are returned verbatim to the
  calling agent. The namespace policy
  (`LOGSEARCH_ALLOWED_NAMESPACES` / `LOGSEARCH_BLOCKED_NAMESPACES`) is the
  control that bounds what a harness can reach, and it is **default-deny**
  (fleet decision D8): an unconfigured server (empty allowlist, escape hatch
  off) answers NOTHING. Set an explicit allow-list for anything beyond a lab
  cluster, or set `LOGSEARCH_EMPTY_ALLOWS_ALL=1` to opt back into the open
  default, and use the block-list to punch holes back out of a broad
  allow-list. Caps (`MAX_PODS` / `MAX_LINES_PER_POD` / `MAX_TOTAL_LINES` /
  `MAX_LINE_CHARS`) bound how much of it leaves the cluster per call.
- Kubernetes access uses the pod's own service account in-cluster
  (`load_incluster_config`, falling back to `load_kube_config` locally).

### API-key auth — MANDATORY (fleet pattern)

`/mcp` requires an API key (`X-API-Key` or `Authorization: Bearer`; the
read-only console and `/health` probes stay public). **The chart never
creates the key Secret — you MUST pre-deploy it in the target namespace
before `helm install`, or the pod sits in `CreateContainerConfigError`:**

```bash
kubectl -n <ns> create secret generic logsearch-mcp-apikey \
  --from-literal="api-keys=$(openssl rand -hex 32)"
```

Keys are a comma-separated list (`api-keys=new,old`) — that is the rotation
mechanism: append the new key, move clients over, drop the old; the server
re-reads the env per request, so no restart is needed. The fleet-universal
`MCP_API_KEYS` env is honored too (either var works).

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
