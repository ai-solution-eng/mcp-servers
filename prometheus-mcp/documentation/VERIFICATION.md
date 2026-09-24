# Verification — prometheus-mcp

After a PCAI apply (or a `helm template` render + install), verify in this
order: reachability → MCP surface → a safe tool call → the auth posture. All
commands are read-only against the cluster.

## 1. Reachability

```bash
# Through the PCAI gateway (the path MCP clients use):
curl -s https://prometheus-mcp.<your-domain>/health
# -> {"status":"ok","prometheus":"http://kubeprom-prometheus.prometheus..."}

# In-cluster (bypasses the gateway):
curl -s http://prometheus-mcp-service.<namespace>.svc.cluster.local:9095/health
```

`"prometheus"` in the response echoes the configured target — if that URL is
wrong, every tool call will fail with a connection error (see
Troubleshooting).

## 2. MCP handshake

This server is MCP 2.0 stateless: there is no initialize handshake and no
`Mcp-Session-Id` header — any replica serves any request, and a plain
JSON-RPC `tools/list` works immediately:

```bash
curl -s https://prometheus-mcp.<your-domain>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect a result naming `prom_alerts`, `prom_label_values`, `prom_query`,
`prom_query_range`, `prom_rules`, `prom_series`. If a client registers the
server as `url: https://prometheus-mcp.<your-domain>/mcp` and lists those six
tools, the connection is good. (If `ezua.authorizationPolicy` is enabled,
add `-H "Authorization: Bearer <token>"` — anonymous calls then get 403 at
the gateway.)

## 3. One tool test

`prom_query` on `up` is the safest first call — it reads the Prometheus
self-scrape target:

```bash
curl -s https://prometheus-mcp.<your-domain>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0", "id": 2,
    "method": "tools/call",
    "params": {"name": "prom_query", "arguments": {"query": "up"}}
  }'
```

Expect a shaped table of `up{...} = 1` lines (one per scrape target). If that
returns `Error: ... connection refused/timeout`, the server cannot reach
`prometheusUrl` — the MCP surface itself is fine. Follow up with a range
query to see the downsampling: `prom_query_range` on
`container_memory_working_set_bytes{container!=""}` (last hour) returns a
bounded, evenly-spaced series — the shape survives, the point count does not
explode. The human path: open the console at
`https://prometheus-mcp.<your-domain>/` — the Dashboard tab should populate
(alert counts, targets, node CPU/memory, GPU stats where DCGM exists), and
the Query tab runs the same expressions with an SVG chart.

## 4. Operator checks (optional)

Kubectl-level confirmation that the release is healthy — read-only verbs:

```bash
kubectl get deploy,po,svc -n <namespace> -l app=prometheus-mcp
kubectl get daemonset -n <namespace> -l app=prometheus-mcp   # nvlink detector (when enabled)
kubectl logs deploy/prometheus-mcp -n <namespace> --tail=50
# Startup banner prints PROM_URL and the caps (max_series / max_points).
kubectl get authorizationpolicy -n istio-system   # when the auth gate is enabled
```

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Every tool returns `Error: ... connection refused` / timeout | `prometheusUrl` points at a service that does not exist on this cluster | Set the real in-cluster Prometheus URL in the Helm Values editor, re-apply |
| Route silently vanishes from the gateway (404 at the edge); blank endpoint aborts the render (`Valid .Values.ezua.virtualService.endpoint is required !`) | `${DOMAIN_NAME}` left unsubstituted by the PCAI build (the placeholder registers a host that matches nothing), endpoint blanked, or `ezua.enabled=false` | Write the literal cluster domain in `ezua.virtualService.endpoint` (and, informationally, `ezua.domainName`); enable ezua. PCAI resolves the placeholder on current builds — keep it there when pasting the hosted-trial example |
| Tools return data, console 404s at `/` | Console is always served — a 404 means the root route is missing | Confirm the VirtualService root route exists (`/` → service 9095) |
| 403 at the gateway on `/mcp` | `ezua.authorizationPolicy.enabled=true` | Present a valid PCAI token (`Authorization: Bearer ...`), or disable the gate |
| GPU tab empty or "no GPU data" | No DCGM exporter on the cluster, or no GPU nodes | Expected on CPU-only clusters; the rest of the console keeps working |
| NVLink domains show the built-in 4+4 default | Detector disabled or failing on that node (driver tree missing) | Check the detector pod logs; or pin `gpuNvlinkDomains` explicitly |
| Responses look truncated (`… N more`) | Series/label-value caps (20 / 200 by design) | Narrow the PromQL selector rather than raising caps |
| Range query slow or 504 | Window/step too wide for the timeout | Narrow the time range; the 660s VS timeout exists for wide windows |
