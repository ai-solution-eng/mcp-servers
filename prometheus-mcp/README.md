# Prometheus MCP Server

MCP 2.0 server for **read-only** querying of a Prometheus instance — the
*time-series* half of cluster observability. A Kubernetes API server sees
**state** (pods, events, logs, instantaneous `top`); Prometheus sees
**behavior over time** (rates, trends, quantiles, alert state). Together
they complete an ops agent: the K8s MCP answers "what happened", this
server answers "since when, how fast, and why".

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

Range queries are downsampled to a bounded number of evenly-spaced points
per series (the *shape* — ramp/spike/sawtooth — survives), floats are
rounded to 4 significant digits, and the series count is capped per
response with a truncation footer naming the count. Caps are env-tunable
(`PROM_MAX_SERIES`/`PROM_MAX_POINTS`/`PROM_MAX_LABEL_VALUES`). A busy
cluster never floods a model's context.

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROM_URL` | `http://kubeprom-prometheus.prometheus.svc.cluster.local:9090` | Prometheus base URL (kube-prometheus-stack default on G2) |
| `PROM_TIMEOUT` | `30` | HTTP timeout seconds |
| `PROM_MAX_SERIES` | `20` | Max series per response |
| `PROM_MAX_POINTS` | `60` | Max (downsampled) points per range series |
| `PROM_MAX_LABEL_VALUES` | `200` | Max label values per response |
| `PROM_BEARER_TOKEN_ENV` | — | Env-var NAME holding a bearer token (value never in config) |

No RBAC is needed: the in-cluster Prometheus HTTP API is plain read-only
HTTP, and the pod needs no Kubernetes permissions at all.

## Local development

```bash
uv venv --python 3.12 .venv
UV_CACHE_DIR=$PWD/.uv-cache uv pip install -e . pytest
.venv/bin/python -m pytest tests/ -v   # 15 unit tests (fully mocked)

# Live check (needs a route to the Prometheus — run in-cluster or port-forward):
PROM_URL=http://localhost:9090 .venv/bin/python tests/live_check.py

# Locally with stdio (inspector):
PROM_URL=http://localhost:9090 .venv/bin/python server.py --transport stdio
```

## Deployment

Reachable from OUTSIDE the cluster through the PCAI gateway (DSH runs
locally on the operator's laptop, so MCP clients hit the ezaf-gateway, not
cluster DNS — same topology as searxng-mcp/sqlhandler). The MCP path uses a
long VirtualService timeout because wide range queries can run for minutes.
The gateway-level oauth2 gate is supported (`ezua.authorizationPolicy`) but
**off by default**: the rotating 30-min SSO token is a known pain for
machine MCP callers — an accepted lab trade (flip it on per environment if
the trust posture changes).

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
3. **Prometheus MCP**: `prom_query_range` on
   `container_memory_working_set_bytes{pod=…}[6h]` → sawtooth = leak,
   plateau-at-limit = undersized
4. Evidence-backed verdict instead of a guess
