# Prometheus MCP Server

MCP 2.0 server for **read-only** querying of a Prometheus instance — the *time-series* half of cluster observability. A Kubernetes API server sees **state** (pods, events, logs, instantaneous `top`); Prometheus sees **behavior over time** (rates, trends, quantiles, alert state). Together they complete an ops agent: the K8s MCP answers "what happened", this server answers "since when, how fast, and
why".

## Tools (all read-only)

| Tool | Purpose |
| --- | --- |
| `prom_query` | Instant PromQL at a point in time (now / `now-6h` / unix / RFC3339) |
| `prom_query_range` | Range query — trends, spikes, leaks, sawtooth-vs-plateau |
| `prom_series` | Which series exist for a selector (label discovery before querying) |
| `prom_label_values` | Values of one label (list pods/namespaces reporting a metric) |
| `prom_alerts` | Currently firing/pending alerts, with labels + annotations |
| `prom_rules` | Alerting/recording rules with their expressions, filterable by state |

## LLM-safe shaping

Range queries are downsampled to a bounded number of evenly-spaced points per series (the *shape* — ramp/spike/sawtooth — survives), floats are rounded to 4 significant digits, and the series count is capped per response with a truncation footer naming the count. Caps are env-tunable (`PROM_MAX_SERIES`/`PROM_MAX_POINTS`/`PROM_MAX_LABEL_VALUES`). A busy cluster never floods a model's context.

## Web UI (HPE-branded)

The streamable-http server also serves a self-contained, no-build web UI — the same HPE branding as the SQLhandler explorer (green element mark, MetricHPE wordmark, light/dark theme with no-flash init and `localStorage` persistence). It is a **read-only** human front-end over the SAME `PrometheusClient` + caps that back the MCP tools, available at `/` (and `/ui`) — through the PCAI gateway too,
since the VirtualService already routes `/` to the service.

| Tab | What it shows |
| --- | --- |
| **Dashboard** | The nice stats: firing/pending alert counts, up targets, node CPU/memory **+ GPU util/memory** %, running pods, top-pods-by-CPU/memory, **top GPU workloads by framebuffer**, cluster CPU + memory trend lines (last 3h), active alerts preview. Optional 30s auto-refresh. |
| **GPU** | NVIDIA fleet view (DCGM exporter): per-GPU utilization/framebuffer/temp/power/NVLink traffic, grouped into the **per-node NVLink domains**, per-domain workload attribution, GPU trend lines, and the full 16-GPU table. Tables are sortable by any column; rows of one deployment band together (alternating lighter/darker) so workloads read as blocks. |
| **Query** | PromQL editor with Instant/Range modes (`now-6h`-style relative times, auto step), SVG line chart + table for range results, CSV copy, preset library in the sidebar (incl. a GPU category), saved queries + history (browser-local). |
| **Alerts & Rules** | Full alert table (state/severity badges, ages, labels, summaries) with filters, plus the rule browser showing each rule's exact expression — searchable, filterable by state. |
| **MCP Tools** | The tool catalog: what each of the six tools is good for, when an agent reaches for it, arguments, tips, clickable example queries, and the K8s-MCP triage-pairing workflow. |

Every dashboard card fails soft: a metric that doesn't exist on a given cluster shows an error inline instead of breaking the page, and the JSON API mirrors the MCP-side caps so a busy cluster can't flood the browser either.

### NVLink grouping: detected, not hardcoded

The GPU tab groups each node's GPUs into NVLink islands. Precedence:

1. **Explicit override** — `gpuNvlinkDomains` (values) / `PROM_UI_GPU_NVLINK_DOMAINS` (env)
2. **Auto-detected** — the bundled `nvlink-topology` DaemonSet (enabled by default) runs `nvidia-smi topo -m` at **GPU-node boot**, computes the connected components of the NVLink graph, and pushes `nvidia_gpu_nvlink_domain{gpu,domain,peers,hostname}` to the pushgateway. NVLink topology is hardware-fixed, so detection is effectively once per node; the container re-verifies daily as belt-and-braces and otherwise idles at ~0 CPU (one tiny Running pod per GPU node is expected — DaemonSet pods only allow `restartPolicy: Always`, so the container supervises its single-shot script).
3. **Built-in default** — two 4-GPU islands per 8-GPU H200 NVL node.

A node whose `nvlink-topology` pod keeps logging failures (missing driver mount, admission policy) can never break the dashboard — the UI silently keeps the next grouping in the chain.

### JSON API (all read-only)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Server status + Prometheus URL + caps |
| `GET /api/overview` | Dashboard aggregate (cards, top-N, trends, alerts) |
| `GET /api/gpu` | GPU aggregate (DCGM): per-GPU stats, NVLink domains, node + cluster summaries — fails soft without DCGM |
| `GET /api/alerts` | Alerts, shaped + counted (`n_total` accurate, items capped) |
| `GET /api/rules?state=&search=` | Rules with expressions, filtered |
| `POST /api/query` | Instant query `{"query", "time"?}` |
| `POST /api/query_range` | Range query `{"query", "start", "end", "step"?}` |
| `GET /api/series?match=` | Series/label discovery for a selector |
| `GET /api/label_values?label=&match=` | Values of one label |

Prometheus upstream errors map to HTTP 502 with the error text; malformed requests to 400. The UI asset (`ui/index.html`) is copied into `/app/ui` by the Dockerfile; `webui.py` also honors a `PROM_UI_HTML` override.

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROM_URL` | `http://kubeprom-prometheus.prometheus.svc.cluster.local:9090` | Prometheus base URL (kube-prometheus-stack default on G2) |
| `PROM_TIMEOUT` | `30` | HTTP timeout seconds |
| `PROM_MAX_SERIES` | `20` | Max series per response |
| `PROM_MAX_POINTS` | `60` | Max (downsampled) points per range series |
| `PROM_MAX_LABEL_VALUES` | `200` | Max label values per response |
| `PROM_BEARER_TOKEN_ENV` | — | Env-var NAME holding a bearer token (value never in config) |
| `PROM_UI_GPU_NVLINK_DOMAINS` | `[[0,1,2,3],[4,5,6,7]]` (built-in default) | GPU-tab NVLink island grouping — JSON list of GPU-index groups, applied per node (Helm knob: `gpuNvlinkDomains`). Beats auto-detection; beats the built-in default |

No RBAC is needed: the in-cluster Prometheus HTTP API is plain read-only HTTP, and the pod needs no Kubernetes permissions at all.

## Local development

```bash
uv venv --python 3.12 .venv
UV_CACHE_DIR=$PWD/.uv-cache uv pip install -e . pytest
.venv/bin/python -m pytest tests/ -v   # 29 unit tests (fully mocked)

# Live check (needs a route to the Prometheus — run in-cluster or port-forward):
PROM_URL=http://localhost:9090 .venv/bin/python tests/live_check.py

# Locally with stdio (inspector):
PROM_URL=http://localhost:9090 .venv/bin/python server.py --transport stdio

# Locally with the web UI:
PROM_URL=http://localhost:9090 .venv/bin/python server.py \
    --transport streamable-http --port 9095
# -> http://localhost:9095/
```

## Deployment

Reachable from OUTSIDE the cluster through the PCAI gateway (DSH runs locally on the operator's laptop, so MCP clients hit the ezaf-gateway, not cluster DNS — same topology as searxng-mcp/sqlhandler). The MCP path uses a long VirtualService timeout because wide range queries can run for minutes. The gateway-level oauth2 gate is supported (`ezua.authorizationPolicy`) but **off by default**: the
rotating 30-min SSO token is a known pain for machine MCP callers — an accepted lab trade (flip it on per environment if the trust posture changes).

```bash
docker buildx build -t ghcr.io/ai-solution-eng/prometheus-mcp:v0.1.0 . --push
helm upgrade --install prometheus-mcp helm/ -n prometheus-mcp \
    --create-namespace -f helm/local/values.se-g2.yaml
# verify: https://prometheus-mcp.<cluster-domain>/mcp answers an initialize
# handshake; in-cluster clients may use the service DNS instead.
```

### MCP client registration (in the DSH profile)

Through the gateway (local DSH — primary):

```yaml
- id: mcp-prometheus
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    transport: streamable-http
    serverName: prometheus
    url: https://prometheus-mcp.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp
```

In-cluster consumers (bypasses the gateway entirely):

```yaml
    url: http://prometheus-mcp-service.prometheus-mcp.svc.cluster.local:9095/mcp
```

## Triage pairing (why this + the K8s MCP)

1. K8s MCP: `describe pod` → `lastState: OOMKilled`, restart count
2. K8s MCP: `logs --previous` → what it was doing
3. **Prometheus MCP**: `prom_query_range` on `container_memory_working_set_bytes{pod=…}[6h]` → sawtooth = leak, plateau-at-limit = undersized
4. Evidence-backed verdict instead of a guess
