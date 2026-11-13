# Features — prometheus-mcp

Every capability this server has, with the design decision behind it.
Written for a reader who has never heard of prometheus-mcp and only knows
that PCAI runs LLM applications. Start here if the README's one-paragraph
summary is not enough.

**MCP** — Model Context Protocol: an endpoint that exposes *tools* (typed
functions) to LLM agents over HTTP, so a model can call them the way it
would call an API. This server is one member of the PCAI MCP fleet; this
one owns **Prometheus observation**: evaluating PromQL queries, discovering
series and labels, listing alerts and their rules.

**Prometheus** — the standard Kubernetes metrics database; the
kube-prometheus-stack that PCAI clusters already run. **PromQL** — its
query language (`rate(http_errors_total[5m])`).

---

## The niche: behavior over time

Ops agents see state (pods, events, logs — the K8S-MCP and logsearch
halves of the fleet) but not **behavior over time**. This server answers
"since when, how fast, and why": error rates climbing, memory leaking in a
sawtooth, the queue depth that preceded the crash. It is the natural
pairing with a Kubernetes MCP ("what happened") and the log server ("what
did it print") for evidence-backed triage instead of guesses.

**Architecture consequence: no Kubernetes RBAC at all.** The server talks
to one backend — the Prometheus HTTP API — over plain read-only HTTP with
the pod's identity. It needs no `pods`/`deployments` verbs, no secrets, no
exec. The blast radius of a compromised MCP client is exactly what
Prometheus will answer read-only.

---

## Tool catalog

All tools are read-only and talk to one Prometheus instance.

| Tool | Purpose | Why it is shaped this way |
|---|---|---|
| `prom_query` | Instant PromQL at a point in time (now / `now-6h` / unix / RFC3339) — current values: rates, error %, memory, queue depth. | The "what is true right now" call. |
| `prom_query_range` | Range query — trends, spikes, leaks, sawtooth-vs-plateau; downsampled, series-capped. | The "how did it get here" call; see [response shaping](#llm-safe-response-shaping). |
| `prom_series` | Which series exist for a selector with their label sets. | Discovery before querying — building queries blind wastes a round trip per mistake. |
| `prom_label_values` | Values of one label (e.g. list pods/namespaces reporting a metric), optionally restricted by a selector. | The other half of discovery: which labels can the query actually filter on. |
| `prom_alerts` | Currently firing/pending alerts with severity, labels, annotations, age. | "Is anything wrong?" in one call — the harness's sweep entry point. |
| `prom_rules` | Alerting/recording rules with expressions, filterable by state and name. Capped at `max_rules` (default 50) with a `…[truncated N more rules]` footer. | A kube-prometheus-stack ships hundreds of rules; rendering every expr unbounded floods the context — narrow with `state`/`search` instead of paging. |
| `query_save` | Save a query (+ optional run params) under a name. | See [Saved queries](#saved-queries). |
| `query_list` | List saved queries — name, params, expression; no Prometheus traffic. | Cheap inventory. |
| `query_delete` | Remove one saved query by name. | Lifecycle. |
| `query_saved` | RUN a saved query through the same instant/range paths as `prom_query`/`prom_query_range`. | Clamps, caps, validation all apply — a saved query gets no side door. Queries are validated when they RUN, not when saved. |

Both query tools take an optional `include_hints` flag (default `true`) —
see [Query hints](#query-hints-advisory-capped-suppressible).

---

## LLM-safe response shaping

Raw Prometheus API responses overwhelm a model context — a busy cluster
returns tens of thousands of points for one range query. The client shapes
every response before the model sees it:

- **Shape-preserving downsampling** — `prom_query_range` reduces each
  series to a bounded number of evenly-spaced points. The *shape* —
  ramp, spike, sawtooth, plateau — survives; the raw sample count does
  not.
- **4-significant-digit rounding** — `0.123456789` becomes `0.1235`; a
  model reasoning about an error rate does not need the 9th digit.
- **Series and label-value caps** — `prom_series`/`prom_label_values`
  truncate with explicit footers, so a high-cardinality selector costs a
  bounded response, not a silent flood.

**Philosophy: the model's context window is the scarcest resource in the
loop.** Every byte of response must earn its place; truncation is always
announced (a footer), never silent.

---

## Performance defaults (fleet decision D14)

Two ratified default changes protect the upstream Prometheus (and the
model) from pathological queries — each with an env escape hatch that
restores the pre-D14 behavior exactly:

**Range-query step clamp** — `PROMETHEUS_MIN_STEP_SECONDS` (default **15**).
A caller-supplied `step` below the floor is clamped UP before the request
reaches the upstream server (`step=1s` over a 24h range would otherwise
pull ~86k points per series). The clamp never lowers a step, and it is
honest: the `prom_query_range` result carries a notice line — `step clamped
to 15s (requested 1s) — PROMETHEUS_MIN_STEP_SECONDS` — and the JSON API
(`POST /api/query_range`) returns the same text in a `step_notice` field
(the effective resolution stays in `step`). Steps at or above the floor
pass through byte-unchanged. `0` disables clamping entirely; the clamp
applies to every range path: the MCP tool, the query editor, and the
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
per-process (each replica computes its own).

**Label-name validation** — no env. The client validates the label NAME
against the console's own rule (`[a-zA-Z_][a-zA-Z0-9_]*`) before
interpolating it into the `/api/v1/label/<name>/values` URL path; invalid
names are rejected client-side with a clear error (`Error: invalid label
name 'a/b': …`) instead of reaching the server.

**Philosophy: protect the upstream, and never lie about it.** Both
mechanisms report themselves in-band; a model reading a cached or clamped
result knows it without reading docs.

---

## Query hints (advisory, capped, suppressible)

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

At most **3 hints** per result; quoted label values can never trip them
(`errors{kind="user_total"}` is not a counter); queries that match nothing
are byte-identical with hints on or off. `include_hints=false` suppresses
the block entirely.

**Philosophy: teach PromQL at the point of mistake.** Agents learn the
language from the tool that grades their homework — one hint block per
result, never a lecture.

---

## Saved queries

`query_save`/`query_list`/`query_delete`/`query_saved` keep a small
name→{query, params} store:

- **Where it lives** — `PROMETHEUS_SAVED_QUERIES_PATH` (chart:
  `persistence.*`). Unset (default) → in-memory only for the session, and
  every tool result says so; set → a durable JSON file at that path on the
  PVC.
- **Per-replica honesty** — the store is per-process. A multi-replica
  deployment fragments stores; `query_save` results carry a `Storage:`
  line plus a warning naming `PROMETHEUS_SAVED_QUERIES_SHARED` (chart
  `persistence.shared: true` — a declaration that the deployment really
  shares the store, silencing the warning; it is NOT concurrency control —
  there is no cross-process locking, a multi-writer JSON store would lose
  updates silently).
- **Writes are atomic** — every mutation writes a temp file in the same
  directory, fsyncs, then `os.replace`s it over the target. A failed write
  (disk full, …) rolls back and leaves the previous file byte-intact.
- **Names are sanitized** — whitespace collapses, anything outside
  letters/digits/space/`.`/`_`/`-` becomes `_`, length caps at 64. The
  stored (sanitized) name is reported back and the original spelling is
  kept as an alias, so `query_saved`/`query_delete` accept either.
- **Saving over an existing name overwrites** (an update, not a duplicate);
  the store caps at 100 entries with a clear error instead of silent
  eviction.
- **`params`** — optional run defaults recorded with the query (`mode`,
  `time` or `start`/`end`/`step`); `query_saved(name, params?)` merges
  per-call overrides over the saved defaults and runs through the same
  code path — the D14 step clamp, the series/point caps and all validation
  apply unchanged.

---

## Web console

The read-only, HPE-branded console at `/` is **always served** (this chart
has deliberately no webui toggle — every dashboard card fails soft, and the
API mirrors the MCP-side caps):

- **Dashboard** — the at-a-glance aggregate (the `/api/overview` payload,
  cached per D14).
- **GPU / NVLink view** — each node's GPUs grouped into NVLink islands,
  detected from the hardware by an optional DaemonSet or pinned via
  `gpuNvlinkDomains`. **NVLink** — NVIDIA's high-bandwidth GPU-to-GPU
  interconnect; which GPUs share an island determines tensor-parallel
  placement.
- **Nodes capacity view** — each node's CPU cores and memory as three bars
  — requested / limited / active — against that node's allocatable track,
  plus per-node 3h trends and a sortable table. GPU nodes are selected by
  default (auto-detected from the nvidia GPU allocatable); `nodeFilter`
  (values) or the browser filter input changes the selection (`all` =
  every node).
- **Query editor** with charts, shareable **deep links**: the expression +
  time range ride in the URL *fragment* (`#q=…&range=…`) — never the query
  string — so a shared link re-sends nothing to the server and never lands
  in access logs. Decoded values only ever land in input `.value`
  assignments (never `innerHTML`) — the console's escape-clean rendering
  rule applies to the fragment too.
- **Alerts & rules browser** — over the same client and caps as the tools.

**Philosophy: humans and agents see the same data through the same
shaping.** The console API is the tool code; there is no second path that
could return unclamped data.

---

## NVLink auto-detect DaemonSet (optional)

The GPU tab's island grouping can be auto-detected: a two-container
DaemonSet (`nvlinkAutodetect.*`, enabled by default) runs on GPU nodes
only, executes `nvidia-smi topo -m` at node boot using the host-installed
driver, and pushes the detected islands to the pushgateway for the GPU tab
to read. It **fails soft** — a node without a working driver mount never
breaks the page; the tab falls back to the built-in default grouping (two
4-GPU islands per 8-GPU node) or the pinned `gpuNvlinkDomains` override.
`nvlinkAutodetect.pushInterval` (default 20s) MUST stay under the
pushgateway's `--metric.timetolive=30s` or the metric evaporates.

**Philosophy: hardware truth beats configuration.** Pinned groupings rot
when hardware moves; the detector re-derives them at every node boot, and
the failure mode of detection failing is a degraded default, not a broken
page.

---

## Self-metrics (GET /metrics — default OFF)

`metrics.enabled: true` (values) serves `GET /metrics` on the MCP port:
one counter family, `prometheus_mcp_tool_requests_total{tool,outcome}` —
per-tool call counts with outcome ok|error (a tool's `Error: ...` string or
exception both count as error). No PromQL text, label names, or error
strings are exported. Default OFF renders no env and no ServiceMonitor;
when on, `/metrics` is key-free (the API-key gate scopes to `/mcp` only),
and the ServiceMonitor template scrapes it via the Prometheus Operator.

---

## Auth posture (optional by design — fleet decision 2026-09)

`/mcp` requires an API key **only when** `PROMETHEUS_API_KEYS` (or the
fleet-universal `MCP_API_KEYS` — either var works, key sets are unioned) is
configured. Default values leave both unset = the server runs **open**,
with a loud startup warning naming the env vars. This is a ratified fleet
decision: this is a read-only observation surface fronted by the SSO-gated
edge gateway, so the operator decides per environment. The gate scopes to
the `/mcp` prefix ONLY: `/health`, `/healthz`, the web console, its
`/api/*` routes, and the opt-in `/metrics` stay key-free — kubelet probes
and the ServiceMonitor scrape never need a key.

For gateway-level enforcement instead, `ezua.authorizationPolicy.enabled:
true` renders an Istio `AuthorizationPolicy` (action CUSTOM) delegating
this host to the PCAI **oauth2-proxy** extension — external callers then
need a valid PCAI token. OFF by default: the rotating 30-min SSO token is a
known pain for machine MCP callers (an accepted lab trade — flip it on per
environment if the trust posture changes).

**Migrating from the no-key posture:** nothing to do to keep current
behavior — with no Secret referenced, the server keeps running open with
the warning. To tighten, pre-deploy the Secret and point `apiKey.existingSecret`
at it (DEPLOYMENT.md §2.1); update in-cluster callers' configs in the same
change. Saved-query storage has no auth layer of its own — the gate is
transport-level on `/mcp` only.

---

## Transport security (DNS-rebinding protection)

With the ezua endpoint pinned (`MCP_HOSTNAME`) the SDK's Host-header
protection is explicitly ON for that FQDN + loopback; `MCP_EXTRA_ALLOWED_HOSTS`
(chart `extraAllowedHosts`) adds in-cluster service-DNS hosts for callers
like an MCP relay. The chart AUTO-prepends the release's own service DNS
(`<deployment.name>-service.<ns>.svc.cluster.local:*`) as the FIRST entry
whenever transport security is active — the gateway's relay hop arrives
with exactly that Host header, and without the entry every relay hop gets
421 Misdirected Request. With neither env set (local dev) the SDK's
implicit loopback-only protection applies unchanged.
