# Features — logsearch-mcp

Every capability this server has, with the design decision behind it. Written
for a reader who has never heard of logsearch-mcp and only knows that PCAI
runs LLM applications. Start here if the README's one-paragraph summary is
not enough.

**MCP** — Model Context Protocol: an endpoint that exposes *tools* (typed
functions) to LLM agents over HTTP, so a model can call them the way it
would call an API. This server is one member of the PCAI MCP fleet; each
fleet server owns one narrow capability. This one owns **pod-log search**.

---

## The niche: logs, not metrics

Cluster observability has two halves. Prometheus (the fleet's other
observability MCP) sees *metrics*: rates, trends, quantiles, alert state —
aggregate behavior over time. This server sees *logs*: what applications
actually printed — tracebacks, panics, OOM kills, crash-loop reasons. The
pairing is deliberate: an agent triaging an incident asks Prometheus "since
when, how fast" and logsearch "what did the pod print when it died".
Neither answers the other's question.

**Philosophy: read-only, namespace-scoped, capped.** The server touches the
most sensitive data on a cluster (pod logs routinely contain tokens in stack
traces, PII, internal hostnames), so every design decision narrows what a
calling agent can reach and how much of it leaves the cluster per call.

---

## Tool catalog

All tools take a `namespace` first, enforce the namespace policy, and return
a JSON string. Every tool except `export_matches` declares
`readOnlyHint=True`; nothing in the server can mutate cluster state or
execute anything in a container (no exec path exists in the code).

| Tool | Contract | Why it is shaped this way |
| --- | --- | --- |
| `list_log_sources(namespace, label_selector="")` | Pods + containers in one namespace with restart counts and age. | The discovery call: agents search blindly without knowing pod names, container names, or which pod is on its 9th restart. |
| `get_pod_logs(namespace, pod, container="", tail_lines=500, since_seconds=None, previous=False)` | Raw tail-bounded log fetch for ONE pod; `previous=true` reads the PREVIOUS (crashed) container. | `previous` is the first move on a CrashLoopBackOff — the current container's log often shows nothing but the crash itself. |
| `search_logs(namespace, pattern, label_selector="", pod_regex="", since_minutes=None, tail_lines=200, case_insensitive=True, container="", max_total_lines=None, context_lines=0)` | Fan-out regex search across the namespace's pods, fetched in PARALLEL under a bounded semaphore (`LOGSEARCH_FETCH_CONCURRENCY`); matching lines carry `podname/container: ` provenance and merge chronologically. | Provenance on every line means the agent never has to guess where a line came from; the chronological merge means a distributed incident reads as one timeline instead of N interleaved pod dumps. |
| `count_matches(namespace, pattern, label_selector="", since_minutes=None, case_insensitive=True, tail_lines=None)` | Per-pod match counts over each pod's effective tail, `{pod: count}` sorted descending. | The "where is this error coming from?" call — one cheap query ranks the offenders before anyone reads full logs. |
| `export_matches(namespace, pattern, dest_name, ...)` | Runs the EXACT `search_logs` pipeline and writes the matched lines to `<LOGSEARCH_EXPORT_ROOT>/<dest_name>` (opt-in — see [Exports](#exports-export_matches--opt-in)). | For result sets too big for a context window: the agent reads the FILE back instead of the tool result. |

Errors are self-describing strings (`Error: ...`), never tracebacks: a
denied namespace names the policy env vars, a dead pod is reported per-pod
while the rest of the fan-out completes, an unreachable cluster says so.
Empty results are empty results, not errors — an agent that cannot
distinguish "no match" from "the server broke" retries blindly.

---

## Context-window discipline: the caps

A noisy namespace can produce gigabytes of log lines per minute. The design
rule: **a single tool call can never blow up the calling agent's context
window, no matter what the cluster is doing.**

| Cap | Default | What it bounds |
| --- | --- | --- |
| `LOGSEARCH_MAX_PODS` | `50` | Pods touched by one fan-out search/count — bounds API-server load. |
| `LOGSEARCH_MAX_LINES_PER_POD` | `1000` | Tail lines fetched per pod (also the tail `count_matches` uses). |
| `LOGSEARCH_MAX_TOTAL_LINES` | `300` | Merged matches per `search_logs`/`export_matches` call. Enforced **DURING** the fan-out: once the budget is full the search stops pulling further pods (`pods_skipped_budget` counts them, `truncated: true`). |
| `LOGSEARCH_MAX_LINE_CHARS` | `2000` | Per-line characters; overlong lines are cut with an explicit `...[truncated N chars]` marker (`0` disables). |
| `LOGSEARCH_FETCH_CONCURRENCY` | `8` | Width of the parallel pod-fetch fan-out (asyncio.gather under a bounded semaphore); `1` = sequential. |

**Philosophy: budget enforcement mid-flight, not after.** Stopping the pull
when the budget fills (instead of fetching everything then truncating)
means a fan-out over 200 pods with a full budget does 20 pod fetches, not
200. The response is honest about it: `truncated`, `pods_searched`,
`pods_skipped_budget` let the agent decide to narrow the query.

**Philosophy: per-call override beats environment.** The env value is the
deployment's default; an explicit per-call `max_total_lines` overrides it —
the agent that knows it wants 20 lines gets 20 lines without an operator
touching values.

---

## Multi-namespace search

`search_logs`/`export_matches` accept SEVERAL namespaces in one call —
comma-separated exact names (`ns-a,ns-b`) or a glob (`team-*`, expanded
against the live namespace list). The policy is evaluated **per resolved
namespace**: a denied namespace is skipped and reported (in the response's
`namespaces` breakdown and `errors`), never silently merged, and each
namespace is searched under its own pod cap and its own slice of the global
`max_total_lines` budget. A single name keeps the exact pre-multi-namespace
response shape.

**Philosophy: a denied namespace is data, not an error.** Skipping with a
report lets a fleet-wide search ("find this traceback anywhere in team-*")
still work when one namespace refuses; silently merging would hide the
boundary the operator drew.

---

## The namespace policy (fleet decision D8 — default-deny)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOGSEARCH_ALLOWED_NAMESPACES` | *(empty)* | Comma-separated fnmatch globs of allowed namespaces. **EMPTY = DENY ALL namespaces** — unless `LOGSEARCH_EMPTY_ALLOWS_ALL=1`. |
| `LOGSEARCH_EMPTY_ALLOWS_ALL` | `false` | The D8 escape hatch: `1`/`true`/`yes`/`on` restores the pre-D8 open default (empty allowlist = everything searchable). Trusted lab clusters only. |
| `LOGSEARCH_BLOCKED_NAMESPACES` | *(empty)* | Comma-separated fnmatch globs; **always wins** over the allowed list — the counterweight that punches holes back out of a broad allow-list. |

Matching is fnmatch-style and case-sensitive (`prod-*` matches
`prod-eu-1`, not `prod`; namespace names are lowercase DNS labels).

**Philosophy: the access control is a value, not a position.** Before D8
(2026-09) an unconfigured server searched everything. Pod logs are the most
leak-prone surface on a cluster, and "the server is only reachable from
inside" is not a control — it is a network accident. D8 inverts the
default: an unconfigured server answers NOTHING. Deployments that want the
old behavior must say so explicitly (`emptyAllowsAll`), which is the point
— the open posture becomes a decision someone makes, not a default someone
forgot.

---

## Regex safety (the ReDoS screen)

User regexes are screened at compile time, BEFORE they ever touch a log
line. A pattern whose backtracking can explode — the classic
nested-quantifier shapes (`(a+)+`, `(.*)*`, `(\w+\s)*`, `(a|aa)+`) —
would otherwise hold the Python GIL and freeze the whole server (every
worker, every request) while `re` backtracks. The screen rejects such
patterns in microseconds with a clean `unsafe regex ... rejected: ...`
error that explains the rewrite (quantify plain character classes, not
groups that already contain quantifiers; or make the inner quantifier
atomic/possessive: `(?>a+)+`, `a++`). Patterns longer than
`LOGSEARCH_MAX_REGEX_CHARS` (default 512) are refused outright; `0`
disables the length cap (the shape screen stays on). Realistic log patterns
are unaffected (`ERROR|Traceback`, `\d+(?:\.\d+)*`, `(?:\d{1,3}\.){3}`,
`(?:[a-f0-9]{2}){8}` all compile as-is). If the optional `re2` package is
installed in the serving image it is preferred for matching (linear-time
engine, belt-and-braces); the screen runs either way and nothing installs
it.

**Philosophy: user input is hostile even when the user is the model.** The
screen costs microseconds per call; a frozen server costs every call. The
error teaches the fix instead of just failing — the agent rewrites the
pattern itself on the next attempt.

---

## Exports (export_matches — opt-in)

`export_matches` answers one need: a fan-out search whose results would
blow the calling agent's context window. It runs the **EXACT
`search_logs` pipeline** — same namespace policy (D8), same ReDoS screen,
same pod/line caps, byte-identical matched lines and counts; there is no
second search path to bypass — and writes the matched lines to
`<LOGSEARCH_EXPORT_ROOT>/<dest_name>`:

    # logsearch export_matches
    # timestamp: 2026-09-13T22:00:00Z
    # namespace: payments-prod
    # pattern: Traceback|ERROR
    # matches: 42 (truncated: false)
    # pods_searched: 12  pods_with_matches: 4  pods_skipped_budget: 0  pod_cap_applied: false
    # errors: none

    payments-api-7/main: 2026-09-13T21:59:58Z ERROR ...

- **Opt-in**: `LOGSEARCH_EXPORT_ROOT` unset (the default) → the tool
  refuses with a self-describing setup message and writes nothing. Fleet
  convention: a path on the workbench/shared PVC — e.g.
  `LOGSEARCH_EXPORT_ROOT=/data/exports` on the logsearch pod plus
  `WORKBENCH_SHARED_PATHS=/data/exports` on the workbench pod — so an
  agent reads the export back through its workbench tools. The directory is
  created on first export if missing.
- **Path safety**: `dest_name` must be a bare file name — no `/` or `\`
  separators, no `..` anywhere, no leading dot, no whitespace/control
  characters (clear error otherwise; the export always lands directly under
  the root). A hostile pattern cannot forge header lines (fields are
  flattened to one line each).
- **Bounded writes**: the file holds at most `max_total_lines` matches,
  each already char-capped by `LOGSEARCH_MAX_LINE_CHARS` — the same caps
  the MCP response would carry, nothing more. Writes are atomic (temp file
  + rename): a reader never sees a partial export, and re-exporting to the
  same name REPLACES it (never appends).
- **Failure posture**: a denied namespace, a refused regex, or bad arguments
  return the very same `Error: ...` string `search_logs` would — and write
  NOTHING. The response carries counts and the file path, never the
  matches themselves (the file is the artifact).

It is the fleet's one honest `readOnlyHint=False` tool here: its only
write is that export file. **Note:** the chart renders the env only when
`logsearch.exportRoot` is set, and the chart ships no volume wiring for it
(`templates/` contains no pvc.yaml and no extraVolumes) — the directory is
whatever the pod's filesystem makes writable at that path. See DEPLOYMENT.md
"Known chart gaps".

---

## Web console

The streamable-http transport also serves a self-contained, HPE-branded log
search console (fleet pattern: no CDN, no build step, corporate-proxy safe):

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
→ 403, bad regex / bad input → 400, cluster-side failure → 502. Gated by
`LOGSEARCH_WEBUI_ENABLED` (chart: `webui.enabled`, default true).

**Philosophy: one enforcement point.** A UI that reimplements the search
drifts from the tools; a UI that calls the tools cannot diverge. Humans get
the same caps and the same policy — and the same denials.

---

## Self-metrics (GET /metrics — default OFF)

`LOGSEARCH_METRICS_ENABLED=true` (chart: `metrics.enabled`) serves
`GET /metrics`: one counter family,
`logsearch_mcp_tool_requests_total{tool,outcome}` — per-tool call counts,
ok/error including namespace-policy denials. No namespace/pod names,
patterns, or log content are exported. Default OFF renders no env and no
ServiceMonitor — the default pod has no `/metrics` route; when on,
`/metrics` is key-free like the probes (the API-key gate scopes to `/mcp`
only). `helm/values.yaml` also documents the fleet audit-JSONL
searchability convention — this server is the SEARCH side of it (workbench
and other fleet servers expose their audit trails on pod stdout;
`search_logs`/`count_matches` make those trails queryable — see
DEPLOYMENT.md "Fleet conventions").

**Philosophy: an unsearchable audit trail is unauditable in practice.** The
fleet keeps audit JSONL on pod stdout so this server can search it; the
counter family deliberately exports outcomes only, so metrics never become
a side channel for log content.

---

## API-key auth (mandatory — fleet pattern)

`/mcp` requires an API key (`X-API-Key` or `Authorization: Bearer`; the
read-only console and `/health` probes stay public). **The chart never
creates the key Secret** — it is pre-deployed out-of-band and referenced by
`apiKey.existingSecret` (default name `logsearch-mcp-apikey`, key
`api-keys`). See DEPLOYMENT.md for the exact prerequisite and failure mode.

Keys are a comma-separated list (`api-keys=new,old`) — that is the rotation
mechanism: append the new key, move clients over, drop the old; the server
re-reads the env per request, so no restart is needed. The fleet-universal
`MCP_API_KEYS` env is honored too (either var works).

**Philosophy: /mcp must never trust network position.** Pod logs are
sensitive enough that "only in-cluster callers can reach it" is not a
control; the key is.

---

## RBAC posture

The chart ships a read-only grant bound to a dedicated ServiceAccount
(gated on `rbac.create=true`). Scope is chosen by `rbac.clusterWide`:

    pods       get, list    # discovery: names, containers, restarts, age
    pods/log   get          # log reads (the only verb pods/log supports)

- `rbac.clusterWide: false` (default) — namespaced `Role` + `RoleBinding`,
  release namespace only: the release's blast radius is exactly one
  namespace.
- `rbac.clusterWide: true` — the SAME two rules as a `ClusterRole` +
  `ClusterRoleBinding`: cluster-wide reads (lab/trusted clusters; the
  namespace policy stays the agent-facing gate).

Explicitly never granted in either mode: secrets, configmaps, `pods/exec`,
`pods/attach`, or any write verb.

Kubernetes access uses the pod's own service account in-cluster
(`load_incluster_config`, falling back to `load_kube_config` when running
locally) — no kubeconfig files ship in the image and no credentials are
configured anywhere.

**Philosophy: RBAC bounds what the SA can read, the namespace policy bounds
what agents may ask for.** They are two different controls at two different
boundaries and both stay deliberately narrow. Cross-namespace reads have two
paths — the chart-native cluster-wide opt-in, or per-namespace bootstrap
Roles an admin applies (DEPLOYMENT.md) — because the release identity
cannot grant itself access elsewhere.

---

## Transport security (DNS-rebinding protection)

The MCP endpoint's Host-header protection comes from the fleet-shared
`mcp_auth.transport_security_from_env`: with BOTH `MCP_HOSTNAME` and
`MCP_EXTRA_ALLOWED_HOSTS` unset (local dev) the SDK's implicit
loopback-only protection applies untouched; with either set, protection is
explicitly ON and the Host allowlist is the pinned public FQDN
(`MCP_HOSTNAME`, chart: `ezua.virtualService.endpoint`) + in-cluster
svc-DNS extras (`MCP_EXTRA_ALLOWED_HOSTS`, chart: `extraAllowedHosts`,
matched verbatim or as `host:*`) + loopback. When transport security is
active the chart AUTO-prepends the release's own service DNS
(`<deployment.name>-service.<ns>.svc.cluster.local:*`) — the LLM-gateway
relay hop's Host header, without which every relay hop gets 421 Misdirected
Request — so sites list only EXTRA hosts.

**Philosophy:** a browser-resident agent session can be tricked into
calling `localhost:port` on the attacker's behalf; pinning the Host header
closes that path without touching legitimate callers.
