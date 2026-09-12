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

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Values walkthrough (required vs optional), Prometheus target, GPU/NVLink detector, ezua/Istio + oauth2-proxy auth gate, upgrading |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | MCP handshake + first tool test, optional operator kubectl checks, troubleshooting |
| [helm/values-examples/README.md](helm/values-examples/README.md) | What the example values files are, how to use them (PCAI editor or `helm -f`) |
