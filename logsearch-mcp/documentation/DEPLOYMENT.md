# Deployment — logsearch-mcp

Deployment is a values problem: import the packaged chart into PCAI once,
then everything below is edited in the chart's values (PCAI **Helm Values**
editor, or the PCAI API) and re-applied. Operators running plain Helm do the
same with `-f <values-file>`; per-cluster values live in `helm/local/`
(gitignored, hardlink-ignored, never packaged).

## Required values

| Key | Why it matters | Default behavior if left empty |
|---|---|---|
| `logsearch.allowedNamespaces` | The namespace allowlist — this READ-ONLY server's ONLY access control (comma-separated `fnmatch` globs). | `""` = **ALL namespaces allowed**. Pod logs routinely contain sensitive strings (tokens in stack traces, PII, internal hostnames) — set it explicitly for anything beyond a lab cluster. |
| `logsearch.blockedNamespaces` | Blocklist; ALWAYS wins over the allowlist — use it to punch holes back out of a broad allow-list. | `""`. |
| `ezua.enabled` + `ezua.domainName` + `ezua.virtualService.endpoint` | Gateway exposure through the PCAI Istio gateway. | `ezua.enabled: false` = ClusterIP-only (in-cluster MCP clients only). |

```yaml
# Required block — adjust the # SITE: lines and apply
logsearch:
  allowedNamespaces: "team-alpha,team-beta,mcp-demo"   # SITE: "" = ALL namespaces readable
  blockedNamespaces: "kube-system,kube-public,kube-node-lease"   # SITE: always wins
ezua:
  enabled: true                      # SITE: deploy the VirtualService
  domainName: <your-domain>          # SITE: literal cluster domain
  virtualService:
    endpoint: logsearch-mcp.<your-domain>   # SITE: host on the ezaf-gateway
    istioGateway: istio-system/ezaf-gateway
    timeout: 300s
```

## Optional values (all have chart defaults)

| Key | Default | Notes |
|---|---|---|
| `logsearch.maxPods` | `50` | Max pods touched by one fan-out search/count — bounds API-server load. |
| `logsearch.maxLinesPerPod` | `1000` | Max tail lines fetched per pod (also the tail `count_matches` uses). |
| `logsearch.maxTotalLines` | `300` | Default cap on merged `search_logs` matches (bounds the MCP response = the agent's context window). |
| `webui.enabled` | `true` | The HPE-branded log-search console at `/` + `/api/*` — a human front-end over the SAME seams/policy/caps. `false` = MCP-only surface (`/health`, `/healthz`, `/mcp`). |
| `deployment.replicaCount` | `1` | Stateless MCP 2.0 — any replica serves any request. |
| `image.repository` / `tag` / `pullPolicy` | chart-managed | Kept in lockstep with `Chart.yaml` by release tooling — leave at the chart default; pinning a stale tag in a site file is how "old server" pods happen. |
| `imagePullSecrets` | `[]` | Only if the GHCR package is private (public packages pull anonymously). |
| `resources` | `100m`/`256Mi` requests, `512Mi` memory limit | Modest; log tails are streamed, not stored. |
| `securityContext` | non-root uid 10001 | Keep. |
| `rbac.create` | `true` | Creates the ServiceAccount + the read-only Role/RoleBinding (release namespace only). `false` falls back to the namespace default ServiceAccount — leave `true`. |
| `hpe_proxies` + `proxy.http/https/noProxy` | `false` | Fleet-consistency block. The only peer is the in-cluster API, covered by the NO_PROXY cluster-local entries; no `caCert` wiring exists because there is no outbound TLS to trust. |

## Underlying detail: values → environment variables

`templates/deployment.yaml` renders the env vars (you normally never touch
these directly):

| Env var | Rendered from |
|---|---|
| `LOGSEARCH_ALLOWED_NAMESPACES` | `logsearch.allowedNamespaces` |
| `LOGSEARCH_BLOCKED_NAMESPACES` | `logsearch.blockedNamespaces` |
| `LOGSEARCH_MAX_PODS` | `logsearch.maxPods` |
| `LOGSEARCH_MAX_LINES_PER_POD` | `logsearch.maxLinesPerPod` |
| `LOGSEARCH_MAX_TOTAL_LINES` | `logsearch.maxTotalLines` |
| `LOGSEARCH_WEBUI_ENABLED` | `webui.enabled` |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (+ lowercase) | `proxy.*`, only when `hpe_proxies=true` |

## Gateway exposure (ezua / Istio)

When `ezua.enabled=true` the chart renders one VirtualService on
`istio-system/ezaf-gateway`: `/mcp` routes to the MCP server (port 9101,
`ezua.virtualService.timeout` — generous, because wide fan-outs take real
time), and the root route sends `/` (console + `/api/*`, health endpoints) to
the same service. Enabling ezua also reproduces the fleet's vendor-label
Kyverno ClusterPolicy (gated on `ezua.enabled` in this chart, so the default
render of a non-PCAI install stays unchanged). PCAI resolves
`${DOMAIN_NAME}` in ezua values before rendering on current builds; if your
build does not, write the literal domain — an unresolved placeholder
registers a gateway host that matches nothing (the route silently vanishes).

## Cross-namespace log reads (operator bootstrap, one-time)

The chart's Role lives in the release namespace only — the release identity
cannot grant itself access elsewhere (PCAI constraint). To search OTHER
namespaces, an admin with rights over them applies a one-time read-only
bootstrap (Role + RoleBinding: `pods` get/list + `pods/log` get, bound to the
release's ServiceAccount — see `helm/local/logsearch-rbac-bootstrap.se-g2.yaml`
for the working shape; local-only, never packaged). Without it, a namespace
allowed by the policy still answers 403 from the API. Grants stay read-only
by design; adding kube-system logs is an explicit sensitivity call.

## Upgrading

Upgrades are a values edit + re-apply, not a redeploy:

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the `# SITE:`-marked values carry over
   unchanged (maps merge recursively; lists replace, not append).
3. Image tags flow from the chart (`image.tag` is release-managed); a new
   chart version rolls the Deployment with the new tag.

Sanity check after any change: `/healthz` answers, and the web console's
status panel (`/api/status`) shows the effective namespace policy and caps —
the exact values the tools enforce.
