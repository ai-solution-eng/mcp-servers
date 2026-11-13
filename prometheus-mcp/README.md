# prometheus-mcp

Read-only **Prometheus observation** over MCP — chart `prometheus-mcp`
0.7.1 for HPE Private Cloud AI (PCAI / Ezmeral Unified Analytics). It turns
the cluster's existing Prometheus into a metrics brain for LLM agents, with
a human web console on the side.

## What it is

prometheus-mcp is an **MCP server** — a Model Context Protocol endpoint
that exposes tools to LLM agents — that evaluates instant and range PromQL
queries, discovers series and label values, lists currently firing/pending
alerts, and exposes alerting/recording rules with their exact expressions —
all against the in-cluster Prometheus over plain read-only HTTP, with
LLM-safe response shaping (shape-preserving downsampling, 4-significant-
digit rounding, series and label-value caps) so a busy cluster never floods
a model's context. It serves MCP 2.0 (stateless streamable-HTTP at `/mcp`)
behind the PCAI Istio gateway, plus an HPE-branded web console (dashboard,
GPU/NVLink view, query editor, alerts & rules) at `/`.

## The problem it solves

- **Ops agents see state but not behavior over time.** Pods, events, logs
  say what happened; this server answers "since when, how fast, and why" —
  the natural pairing with a Kubernetes MCP for evidence-backed triage
  instead of guesses.
- **Raw Prometheus API responses overwhelm a model context.**
  `prom_query_range` downsamples to a bounded number of evenly-spaced
  points per series (the shape — ramp/spike/sawtooth — survives), rounds to
  4 significant digits, and caps series/label-value counts with truncation
  footers.
- **Building queries blind is slow.** `prom_series` + `prom_label_values`
  discover what labels exist before an exact query is written, and the
  query tools attach advisory hints (`[counter]`, `[cardinality]`, …) that
  teach PromQL at the point of mistake.
- **"Is anything wrong?" needs one call.** `prom_alerts` lists
  firing/pending alerts with severity, labels, annotations;
  `prom_rules` shows the exact condition and threshold behind each alert.
- **Humans need the same data without writing PromQL.** The web console at
  `/` provides a dashboard, a per-GPU/NVLink fleet view (DCGM), a per-node
  CPU & memory capacity view, a query editor with charts, and an alerts &
  rules browser — over the same client and caps as the tools.
- **GPU fleet visibility.** The GPU tab groups each node's GPUs into NVLink
  islands, detected from the hardware by an optional DaemonSet (or pinned
  explicitly via `gpuNvlinkDomains`).

## How it fits in PCAI

Deployed as chart `prometheus-mcp` in its own namespace, exposed through
the PCAI Istio gateway (`ezaf-gateway`) next to the rest of the MCP fleet:

    browser / agents ──► ezaf-gateway (Istio) ──► prometheus-mcp :9095
                                                     │  plain read-only HTTP
                                                     ▼
                                  kube-prometheus-stack Prometheus :9090

- **Depends on:** a reachable Prometheus HTTP API (the kube-prometheus-stack
  default) — and **no Kubernetes RBAC at all** (plain HTTP with the pod's
  identity); the Istio gateway for external exposure.
- **Depends on it:** agent harnesses (the LLM gateway relays MCP calls) and
  humans via the console. The optional NVLink detector DaemonSet feeds the
  GPU tab through the pushgateway.
- **Siblings:** logsearch-mcp is the logs half of the same story; a
  Kubernetes MCP covers pod/event state. Any MCP client that speaks
  streamable-HTTP connects to `/mcp` (stateless — no session header):

```json
{
  "mcpServers": {
    "prometheus-mcp": { "url": "https://prometheus-mcp.<your-domain>/mcp" }
  }
}
```

Clients that want the transport spelled out accept `"type": "http"` (Claude
Code / Claude Desktop) or `"transport": "streamable-http"` (DSH profile,
opencode). In-cluster consumers can use the service DNS:
`http://prometheus-mcp-service.<namespace>.svc.cluster.local:9095/mcp`. If
`ezua.authorizationPolicy.enabled=true`, external callers must present a
valid PCAI token on every call.

## Capabilities, by theme

**The tool surface** — ten read-only tools, one Prometheus: `prom_query`,
`prom_query_range`, `prom_series`, `prom_label_values`, `prom_alerts`,
`prom_rules`, plus the saved-query family `query_save` / `query_list` /
`query_delete` / `query_saved`. Saved queries run through the same code
path — clamps, caps, validation all apply; queries are validated when they
RUN, not when saved. → [FEATURES.md#tool-catalog](documentation/FEATURES.md#tool-catalog)

**LLM-safe response shaping** — downsampling that preserves shape, 4-digit
rounding, capped series/label values with announced truncation. The model's
context window is the scarcest resource in the loop; every byte earns its
place and truncation is never silent. →
[FEATURES.md#llm-safe-response-shaping](documentation/FEATURES.md#llm-safe-response-shaping)

**Upstream protection (fleet decision D14)** — the range-query step clamp
(`PROMETHEUS_MIN_STEP_SECONDS`, default 15, honest notice in-band) stops
`step=1s`-style point floods; the `/api/overview` cache (TTL 20 s,
single-flight, `cached`-honesty fields) keeps the dashboard from
stampeding Prometheus; client-side label-name validation rejects bad paths
before they reach the server. →
[FEATURES.md#performance-defaults-fleet-decision-d14](documentation/FEATURES.md#performance-defaults-fleet-decision-d14)

**Query hints** — up to 3 advisory hints per query result, from static
analysis only: `[cardinality]`, `[counter]`, `[regex]`, `[range-vector]`.
Teach PromQL at the point of mistake; suppressible with
`include_hints=false`. →
[FEATURES.md#query-hints-advisory-capped-suppressible](documentation/FEATURES.md#query-hints-advisory-capped-suppressible)

**Saved queries with per-replica honesty** — durable only when wired to a
PVC (`persistence.*`); otherwise in-memory with results that say so. Atomic
writes, sanitized names, 100-entry cap, a `shared` declaration that is
honest about being a declaration, not locking. →
[FEATURES.md#saved-queries](documentation/FEATURES.md#saved-queries)

**The web console** — always served (no toggle by design), fails soft card
by card, GPU/NVLink islands from hardware detection, node capacity bars
against allocatable, query-editor deep links that ride the URL fragment
(never the query string, so shared links leak nothing). →
[FEATURES.md#web-console](documentation/FEATURES.md#web-console)

**NVLink auto-detect DaemonSet** — `nvidia-smi topo -m` at GPU-node boot,
pushed through the pushgateway, fails soft to the built-in grouping; the
push interval must stay under the pushgateway's 30 s TTL. →
[FEATURES.md#nvlink-auto-detect-daemonset-optional](documentation/FEATURES.md#nvlink-auto-detect-daemonset-optional)

**Auth posture, optional by design** — the API-key gate on `/mcp` exists
only when you reference a Secret (fleet decision 2026-09: a read-only
observation surface behind the SSO-gated edge may run open, with a loud
startup warning); gateway-level SSO enforcement via `ezua.authorizationPolicy`
is the other lever. →
[FEATURES.md#auth-posture-optional-by-design--fleet-decision-2026-09](documentation/FEATURES.md#auth-posture-optional-by-design--fleet-decision-2026-09)

## Deploy on PCAI (one screen)

1. **Prerequisites:** confirm the in-cluster Prometheus service DNS (§2.3
   of DEPLOYMENT.md — reachability proven from the cluster, not the
   laptop); decide the auth posture. No mandatory Secret.
2. **Import the chart once** into the PCAI catalog.
3. **Create the deployment** from the catalog entry and open the Helm
   Values editor.
4. **Paste values** — start from `helm/values-examples/values.g2.yaml` or
   `values.hosted-trial.yaml` (full documents), set `prometheusUrl`,
   adjust `# SITE:` lines, keep `${DOMAIN_NAME}` as-is.
5. **Apply and verify:** `/healthz` answers; `prom_query` for `up` returns
   data; the console dashboard renders; MCP handshake per
   [documentation/VERIFICATION.md](documentation/VERIFICATION.md).

Full procedure — prerequisites tables, values, upgrade, troubleshooting:
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md).

## Prerequisites at a glance

| Prerequisite | Purpose | What breaks without it |
|---|---|---|
| Reachable Prometheus HTTP API (`prometheusUrl`) | The thing being observed — defines the deployment | Every tool call errors; prove reachability from the cluster |
| Istio gateway + `ezua.virtualService.endpoint` (values; `ezua.enabled` defaults true) | The product path for external MCP clients + console | Render aborts on empty endpoint; unresolved `${DOMAIN_NAME}` matches nothing |
| API-key Secret (OPTIONAL — only if you want the `/mcp` gate) | Key auth for machine callers beyond the lab posture | Nothing, by design — the server runs open with a loud warning; a referenced-but-missing Secret is `CreateContainerConfigError` |
| prometheus-operator CRDs (only with `metrics.enabled: true`) | ServiceMonitor scraping of self-metrics | Render fails on the unknown kind |
| GPU nodes with driver mounts (only with NVLink auto-detect) | Hardware-truth island detection for the GPU tab | Fails soft — built-in grouping or your `gpuNvlinkDomains` pin |
| StorageClass (only with `persistence.enabled: true`) | Durable saved-query store | Store stays in-memory; saved queries vanish on pod reschedule |
| `hpe-ezua` vendor labels (Kyverno policy, chart-rendered ungated) | PCAI platform recognition | Cosmetic; platform tooling misses the labels |

## Day-2

- **Upgrade** — import the newer chart, re-apply your values document
  (DEPLOYMENT.md §5); `pullPolicy: Always` picks up rebuilt images even at
  the same tag.
- **Health** — `GET /health`, `/healthz`; the console dashboard itself is a
  health check (every card fails soft).
- **Saved-query durability** — verify the store file exists on the PVC
  after enabling `persistence.*`.
- **Rotate the API key** (when the gate is on) — append to
  `api-keys=new,old` in the Secret; env re-reads per request.
- **Uninstall** — delete the deployment from the PCAI UI; the saved-query
  PVC survives until explicitly deleted.
- **Troubleshooting** — symptom → cause → fix table:
  [DEPLOYMENT.md §6](documentation/DEPLOYMENT.md).

## Documentation map

| File | The question it answers |
|---|---|
| [documentation/FEATURES.md](documentation/FEATURES.md) | What can it do, and why is each thing designed that way? |
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | How do I deploy, upgrade, and debug it the PCAI way — values, prerequisites, troubleshooting? |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | How do I prove the deployment works (MCP handshake, first tool test)? |
| [helm/values-examples/README.md](helm/values-examples/README.md) | Which paste-ready values file matches my target, and what is in it? |
| [helm/values.yaml](helm/values.yaml) | Every chart key with its default and inline rationale |

## Developing locally

**Not the deployment path.** The server is a single Python service
(Starlette, MCP 2.0 stateless, JSON responses) that talks to one backend:
the Prometheus HTTP API. Run it from the package tree and point it at any
reachable Prometheus; tests run fully offline (transport mocked). Site
operators rarely rebuild images, but if you do, read DEPLOYMENT.md §7
first — the W7 packaging fix exists because a rebuild once shipped without
`mcp_metrics.py` and new pods crash-looped while old pods looked fine.
