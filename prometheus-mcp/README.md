# prometheus-mcp

prometheus-mcp is a read-only **Prometheus observation** MCP (Model Context
Protocol) server: it evaluates instant and range PromQL queries, discovers
series and label values, lists currently firing/pending alerts, and exposes
alerting/recording rules with their exact expressions — all against the
in-cluster Prometheus over plain read-only HTTP, with LLM-safe response
shaping (shape-preserving downsampling, 4-significant-digit rounding, series
and label-value caps) so a busy cluster never floods a model's context. It
serves MCP 2.0 (stateless streamable-HTTP at `/mcp`) behind the PCAI Istio
gateway, plus an HPE-branded web console (dashboard, GPU/NVLink view, query
editor, alerts & rules) at `/`.

**What problem(s) it solves**

- Ops agents see state (pods, events, logs) but not **behavior over time**:
  this server answers "since when, how fast, and why" — the natural pairing
  with a Kubernetes MCP ("what happened") for evidence-backed triage instead
  of guesses.
- Raw Prometheus API responses overwhelm a model context: `prom_query_range`
  downsamples to a bounded number of evenly-spaced points per series (the
  shape — ramp/spike/sawtooth — survives), and series/label-value counts are
  capped with truncation footers.
- Building queries blind is slow: `prom_series` + `prom_label_values`
  discover what labels exist before an exact query is written.
- "Is anything wrong?" needs one call: `prom_alerts` lists firing/pending
  alerts with severity, labels, and annotations; `prom_rules` shows the exact
  condition and threshold behind each alert.
- Humans need the same data without writing PromQL: the web console at `/`
  provides a dashboard, a per-GPU/NVLink fleet view (DCGM), a query editor
  with charts, and an alerts & rules browser — over the same client and caps
  as the tools.
- GPU fleet visibility: the GPU tab groups each node's GPUs into NVLink
  islands, detected from the hardware by an optional DaemonSet (or pinned
  explicitly via `gpuNvlinkDomains`).

## Tools

All tools are read-only and talk to one Prometheus instance.

| Tool | Purpose |
|---|---|
| `prom_query` | Instant PromQL at a point in time (now / `now-6h` / unix / RFC3339) — current values: rates, error %, memory, queue depth. |
| `prom_query_range` | Range query — trends, spikes, leaks, sawtooth-vs-plateau; downsampled, series-capped. |
| `prom_series` | Which series exist for a selector with their label sets — discovery before querying. |
| `prom_label_values` | Values of one label (e.g. list pods/namespaces reporting a metric), optionally restricted by a selector. |
| `prom_alerts` | Currently firing/pending alerts with severity, labels, annotations, age. |
| `prom_rules` | Alerting/recording rules with expressions, filterable by state and name. |
| `query_save` | Save a query (+ optional run params) under a name (Wave-5). |
| `query_list` | List saved queries — name, params, expression; no Prometheus traffic. |
| `query_delete` | Remove one saved query by name. |
| `query_saved` | RUN a saved query through the same instant/range paths as `prom_query`/`prom_query_range` (clamps, caps, validation all apply). |

Both query tools take an optional `include_hints` flag (default `true`) —
see "Query hints" below.

## Architecture

A single Python service (Starlette, MCP 2.0 stateless, JSON responses) that
talks to one backend: the **Prometheus HTTP API** (default: the
kube-prometheus-stack service in the `prometheus` namespace — set
`prometheusUrl` if your cluster differs). It needs **no Kubernetes RBAC at
all** — plain read-only HTTP with the pod's identity. The optional
NVLink-topology DaemonSet (GPU nodes only) runs `nvidia-smi topo -m` at node
boot and pushes the detected islands to the pushgateway for the GPU tab; it
fails soft — a node without a working driver mount never breaks the page.
HTTP surface: `/mcp` (MCP streamable-HTTP), `/health` + `/healthz`, the web
console at `/` + `/api/*` (always served — this chart has no webui toggle),
and an optional gateway-level auth gate (`ezua.authorizationPolicy`,
off by default).

## Deploy on PCAI (HPE Private Cloud AI)

Import the packaged chart once into PCAI, then edit the chart's values in the
PCAI **Helm Values** editor and apply — you never run `helm install` or
`kubectl apply` for the deployment itself. Every `helm --set a.b=c`
corresponds 1:1 to a values key. `${DOMAIN_NAME}` in ezua values is resolved
by PCAI's deployment pipeline before helm runs — keep the placeholder. Note
`ezua.domainName` is informational only: the gateway host comes solely from
`ezua.virtualService.endpoint`.

**Required values**:

```yaml
prometheusUrl: http://kubeprom-prometheus.prometheus.svc.cluster.local:9090
#                SITE: the in-cluster Prometheus service — the
#                kube-prometheus-stack default; adjust the release/namespace
#                if your cluster names it differently
ezua:
  enabled: true                      # SITE: expose through the PCAI Istio gateway
  domainName: ${DOMAIN_NAME}          # informational — no template reads it
  virtualService:
    endpoint: prometheus-mcp.${DOMAIN_NAME}   # SITE: /mcp -> MCP server; / -> web console
    istioGateway: istio-system/ezaf-gateway
    timeout: 660s                    # long: wide range queries can run for minutes
```

**Optional values**: `gpuNvlinkDomains` (pin the GPU-tab NVLink grouping),
`nvlinkAutodetect.*` (the DaemonSet detector — disable if you pin the
grouping or want no resident pods), `image.*` (this chart pins
`pullPolicy: Always` for a reason — see `helm/values.yaml`), `resources`,
`ezua.authorizationPolicy.*` (gateway-level oauth2-proxy enforcement, off by
default). Complete paste-ready documents:
[helm/values-examples/values.g2.yaml](helm/values-examples/values.g2.yaml)
and
[helm/values-examples/values.hosted-trial.yaml](helm/values-examples/values.hosted-trial.yaml).

### Mandatory endpoint when the gateway is on

`ezua.virtualService.endpoint` is **required whenever `ezua.enabled: true`**
(this chart's default): `templates/virtualservice.yaml` and — when the
authorization gate is on — `templates/authorizationpolicy.yaml` call Helm's
`required` on it, so an empty endpoint aborts the render before anything is
created — `Valid .Values.ezua.virtualService.endpoint is required !` /
`… is required when ezua is enabled !`. It must be unique per release on the
shared ezaf-gateway, and it is the *only* gateway host: `ezua.domainName` is
informational (no template reads it). With `ezua.enabled: false` nothing is
rendered from it — in-cluster Service access only.

### Saved-query persistence (`persistence.*` — default OFF)

`query_save`/`query_list`/`query_delete`/`query_saved` keep their JSON store
in memory unless `persistence.enabled: true` wires `PROMETHEUS_SAVED_QUERIES_PATH`
to `<mountPath>/<savedQueriesFile>` on a dedicated PVC
(`<deployment.name>-data`) — every pod update/reschedule otherwise silently
forgets every saved query. Sub-keys:

| Key | Default | Effect |
| --- | --- | --- |
| `persistence.mountPath` | `/data` | Where the store volume mounts in the container. |
| `persistence.savedQueriesFile` | `saved-queries.json` | Store file **inside** the mount, pinned explicitly so a moved `mountPath` cannot silently orphan an existing store. |
| `persistence.size` | `1Gi` | PVC storage request. |
| `persistence.storageClass` | `""` | Empty = cluster default StorageClass. |
| `persistence.accessModes` | `[ReadWriteOnce]` | `replicaCount > 1` with a RWO class strands extra replicas Pending — flip to a RWX class (gl4f-filesystem RWX on G2) or keep 1 replica (the fleet default). |

### Standard Kubernetes knobs

Defaults fit the fleet baseline; overridable per deployment.

| Key | Default | Effect |
| --- | --- | --- |
| `deployment.appName` | `prometheus-mcp` | Label/selector + container name on Deployment, Service, VirtualService — not the release name (`deployment.name` is, and names the PVC). Leave at the default; mismatched selectors break the Service/VS wiring. |
| `image.tag` | `v0.5.2` | Kept in lockstep with the pushed image tags; the release tooling bumps it. Pin a site override only deliberately — a stale tag is how "old MCP" pods happen. |
| `resources.requests.cpu` | `100m` | CPU request (limits: memory `512Mi`; requests.memory `128Mi`, no CPU limit). |

## Connect an MCP client

Any MCP client that speaks streamable-HTTP connects to `/mcp` (stateless —
no session header needed); humans use the web console at `/`:

```json
{
  "mcpServers": {
    "prometheus-mcp": {
      "url": "https://prometheus-mcp.<your-domain>/mcp"
    }
  }
}
```

Clients that want the transport spelled out accept `"type": "http"`
(Claude Code / Claude Desktop) or `"transport": "streamable-http"` (DSH
profile, opencode). In-cluster consumers can use the service DNS instead:
`http://prometheus-mcp-service.<namespace>.svc.cluster.local:9095/mcp`. If
`ezua.authorizationPolicy.enabled=true`, external callers must present a
valid PCAI token on every call.

## Self-metrics (GET /metrics — default OFF)

The MCP server exports ITS OWN request counters (not the upstream
Prometheus's series) at `GET /metrics`: one counter family,
`prometheus_mcp_tool_requests_total{tool,outcome}` — per-tool call counts
with outcome ok|error (a tool's "Error: ..." string or exception both count
as error). No PromQL text, label names, or error strings are exported.
Chart-gated default-OFF: `metrics.enabled: false` (the default) renders no
env and no ServiceMonitor and the server serves no `/metrics` route — the
default pod is unchanged. Set `metrics.enabled: true` (values) to enable;
`/metrics` rides the same port as `/mcp`, and the ServiceMonitor template
(templates/servicemonitor.yaml) scrapes it via the Prometheus Operator.

## Performance defaults (Wave-4, decision D14)

Two ratified default changes protect the upstream Prometheus (and the
model's context) from pathological queries — each with an env escape hatch
that restores today's behavior exactly:

**Range-query step clamp** — `PROMETHEUS_MIN_STEP_SECONDS` (default **15**).
A caller-supplied `step` below the floor is clamped UP to it before the
request reaches the upstream server (`step=1s` over a 24h range would
otherwise pull ~86k points per series). The clamp never lowers a step, and
it is honest: the `prom_query_range` tool result carries a notice line —
`step clamped to 15s (requested 1s) — PROMETHEUS_MIN_STEP_SECONDS` — and the
JSON API (`POST /api/query_range`) returns the same text in a `step_notice`
field (the effective resolution stays in `step`). Steps at or above the
floor pass through byte-unchanged. `PROMETHEUS_MIN_STEP_SECONDS=0` disables
clamping entirely; unset/non-numeric values fall back to the default. The
clamp applies to every range path: the MCP tool, the query editor, and the
dashboard trends.

**`/api/overview` payload cache** — `PROMETHEUS_OVERVIEW_CACHE_TTL`
(default **20** seconds; `0` disables). The dashboard aggregate runs ~14
upstream queries per refresh (30-60s payload on a busy cluster); a refresh
within the TTL is served instantly from the previous computation and is
marked honestly — `cached: true` + `cache_age_seconds` (fresh responses say
`cached: false`, age `0`). The cache key is the request's actual parameter
set, failures are never memoized (an overview whose every block errored is
recomputed next refresh), and concurrent identical overviews share a single
computation (single-flight) instead of stampeding Prometheus. The cache is
per-process (each replica computes its own); `0` restores the pre-D14
recompute-every-refresh behavior exactly.

**Label-name validation** — no env. The client validates the label NAME
against the same rule the web console always applied
(`[a-zA-Z_][a-zA-Z0-9_]*`) before interpolating it into the
`/api/v1/label/<name>/values` URL path; invalid names are rejected
client-side with a clear error (`Error: invalid label name 'a/b': …`)
instead of reaching the server. Valid label names are unaffected.

`PROMETHEUS_METRICS_ENABLED` (Wave 3) remains chart-gated default-off — see
the self-metrics section above.

## Saved queries, query hints, dashboard deep links (Wave-5, additive)

Three additive features; the default experience is unchanged when they are
unused.

### Saved queries (`query_save` / `query_list` / `query_delete` / `query_saved`)

A small name→{query, params} store:

- **Where it lives** — `PROMETHEUS_SAVED_QUERIES_PATH` (server env). Unset
  (default) → the store is **in-memory only for the session** and every
  tool result says so ("in-memory only for this session — set
  PROMETHEUS_SAVED_QUERIES_PATH to persist"). Set → a **durable JSON file**
  at that path. The store is per-process (each replica keeps its own; the
  file is per-replica state, not a shared multi-writer database).
- **Writes are atomic** — every mutation writes a temp file in the same
  directory, fsyncs, then `os.replace`s it over the target. A failed write
  (disk full, …) rolls the store back and leaves the previous file
  byte-intact; no partial file can ever appear at the path.
- **Names are sanitized** — whitespace collapses, anything outside
  letters/digits/space/`.`/`_`/`-` becomes `_`, length caps at 64. The
  stored (sanitized) name is reported back, and the original spelling is
  kept as an alias so `query_saved`/`query_delete` accept either.
- **Saving over an existing name overwrites** it (an update, not a
  duplicate); the store caps at 100 entries with a clear error instead of
  silent eviction.
- **`params`** — optional run defaults recorded with the query, using the
  exact arguments the query tools already take: `mode`
  (`instant`|`range`), `time` (instant) or `start`/`end`/`step` (range).
  `query_saved(name, params?)` merges per-call overrides over the saved
  defaults and runs the query through the **same code path** as
  `prom_query`/`prom_query_range` — the D14 step clamp, the series/point
  caps and all validation apply unchanged. Queries are validated when they
  RUN, not when saved.

### Query hints (advisory, capped, suppressible)

`prom_query` and `prom_query_range` append a short `Hints (advisory)` block
when the query text matches an obvious pattern — static analysis of the
expression only, no extra HTTP, no result inspection:

- `[cardinality]` — a label matcher whose regex matches every value
  (`{pod=~".*"}`): narrow the selector to bound the series count.
- `[counter]` — a `_total`/`_count` metric used without `rate()`/`increase()`
  or any over-time function: raw counters only ever rise.
- `[regex]` — a matcher starting with an unbounded wildcard
  (`{pod=~".*myapp.*"}`): anchoring keeps matching cheap on
  high-cardinality labels.
- `[range-vector]` — a bare `metric[5m]` selector with no over-time
  function: an instant query wants `rate(metric[5m])` (or the selector
  without the bracket).

At most **3 hints** are returned per result, quoted label values can never
trip them (`errors{kind="user_total"}` is not a counter), and queries that
match nothing are byte-identical with hints on or off. Pass
`include_hints=false` to suppress the block entirely.

### Dashboard deep links (web console)

The query editor's state is shareable: the expression + time range ride in
the URL **fragment** (`#q=…&range=…`) — never the query string — so a
shared link re-sends nothing to the server and never lands in access logs:

- `#q=up&range=now` — instant at `now`
- `#q=<urlencoded PromQL>&range=now-6h..now/2m` — range from `now-6h` to
  `now` with step `2m` (omit the `/step` for auto resolution)

The fragment is decoded on page load (switching to the Query tab and
running the query), refreshed after every successful run
(`history.replaceState` — no reload), and copied by the **🔗 Link** button
next to 💾 Save. Decoded values only ever land in input `.value`
assignments (never `innerHTML`) — the console's escape-clean rendering
rule applies to the fragment too.

These browser features are independent of the MCP-side saved queries (the
console's "Saved queries" panel remains per-browser localStorage; the MCP
store above is per-server).

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Values walkthrough (required vs optional), Prometheus target, GPU/NVLink detector, ezua/Istio + oauth2-proxy auth gate, upgrading |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | MCP handshake + first tool test, optional operator kubectl checks, troubleshooting |
| [helm/values-examples/README.md](helm/values-examples/README.md) | What the example values files are, how to use them (PCAI editor or `helm -f`) |
