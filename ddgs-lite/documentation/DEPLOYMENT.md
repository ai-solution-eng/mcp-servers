# DEPLOYMENT — ddgs-lite on PCAI

Deployment on PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) is values-driven: import the packaged `ddgs-lite` chart into the PCAI catalog once, create the deployment from it, and set every knob in the chart's **Helm Values** editor (or via the PCAI API). PCAI resolves `${DOMAIN_NAME}` in the `ezua` values before rendering. There is deliberately no `helm install`/`kubectl apply` runbook here — that is not how PCAI deployments happen.

## Values walkthrough (from `helm/values.yaml`)

### Required

| Key | Why it is required |
|---|---|
| `ezua.domainName` | Cluster domain; use `${DOMAIN_NAME}` — PCAI substitutes it. |
| `ezua.virtualService.endpoint` | Full public hostname, e.g. `ddgs-lite.${DOMAIN_NAME}`. Must be unique per release on the shared gateway. The VirtualService fails to render without it. |

Minimal required-values document:

```yaml
ezua:
  enabled: true
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "ddgs-lite.${DOMAIN_NAME}"
    istioGateway: "istio-system/ezaf-gateway"
    timeout: 660s
  authorizationPolicy:
    namespace: "istio-system"
    providerName: "oauth2-proxy"
```

### Optional (defaults are sensible)

| Key | Default | Meaning |
|---|---|---|
| `deployment.name` / `appName` / `replicaCount` | `ddgs-lite` / 1 | Workload naming and scale. |
| `image.repository` / `tag` / `pullPolicy` | `ghcr.io/ai-solution-eng/ddgs-mcp` / `v1.0.1` / `IfNotPresent` | Pin `tag` to the image matching the chart version. |
| `service.port` / `targetPort` | 9090 / 9090 | In-cluster MCP port (streamable-http + SSE at `/mcp`). |
| `resources` | 200m/200Mi → 1 CPU/512Mi | Single small container; no tuning usually needed. |
| `env` | `DDG_REGION: ""` | `DDG_REGION` sets the default region/locale for searches (e.g. `us-en`, `de-de`, `wt-wt`); clients can still pass `region` per call. |
| `proxy.{http,https,noProxy}` | `{}` (all off) | Per-key egress proxy wiring — each key is injected only when non-empty: `http`/`https` → `HTTP(S)_PROXY` env, `noProxy` → `NO_PROXY`. **On corporate-proxy clusters without direct internet egress, set `http`/`https` (and usually `noProxy`)** so `search`/`fetch_content` reach the engines. On direct-egress systems leave `proxy: {}`. |
| `ezua.enabled` | `true` | `false` skips the VirtualService (in-cluster-only exposure). |

Underlying detail: the container honors the standard `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` env vars — each `proxy.<key>` fills them only when that key is non-empty.

### Migrating from hpe_proxies

The `hpe_proxies` flag was removed (chart ≥ 1.1.0) — the per-key `proxy:` dict replaced it. Set `proxy.http` / `proxy.https` / `proxy.noProxy` directly; each key is active only when non-empty, and `proxy: {}` is fully off. Former `hpe_proxies: true` sites must now write the explicit proxy block (the chart no longer carries built-in HPE proxy defaults in shipped values).

## ezua / Istio wiring

When `ezua.enabled: true` the chart renders:

- **VirtualService** `<name>-vs` on gateway `ezua.virtualService.istioGateway` (default `istio-system/ezaf-gateway`), host = `ezua.virtualService.endpoint`, all paths routed to `<name>-service.<namespace>.svc.cluster.local:9090` with `timeout: 660s` (long enough for multi-tool agent turns).
- **Kyverno pre-install ClusterPolicy** stamping `hpe-ezua/type: vendor-service` and `hpe-ezua/app: ddgs-lite` labels on the workload — the marker PCAI uses to recognize vendor services.

The values also carry an `ezua.authorizationPolicy` block (`namespace: istio-system`, `providerName: oauth2-proxy`) documenting the PCAI gateway-gate convention. Note this chart ships **no** AuthorizationPolicy template itself — gateway-side authentication for the endpoint is managed at the PCAI/gateway level; the server performs no client auth of its own.

## Upgrading

Edit values, re-apply:

1. Change the values in the PCAI **Helm Values** editor (or re-submit the deployment via the PCAI API with a new values document) — e.g. a new `image.tag`.
2. Apply. PCAI re-renders and rolls the Deployment; the VirtualService is updated in place.
3. Confirm the new pod is ready and re-run the [VERIFICATION](VERIFICATION.md) tool test.

No migration steps exist — the server is stateless with no persistent volumes.

## See also

- [`../helm/values-examples/`](../helm/values-examples/README.md) — paste-ready full-values examples (G2 lab cluster, hosted trial).
- [`../helm/values.yaml`](../helm/values.yaml) — every key with its default and inline comments.
