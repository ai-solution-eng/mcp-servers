# Deployment — applygate-mcp

Deployment is a values problem: import the packaged chart into PCAI once,
then everything below is edited in the chart's values (PCAI **Helm Values**
editor, or the PCAI API) and re-applied. Operators running plain Helm do the
same with `-f <values-file>`; per-cluster secrets-bearing values live in
`helm/local/` (gitignored, hardlink-ignored, never packaged).

## Required values

| Key | Why it is required | Default behavior if left empty |
|---|---|---|
| `namespaces.allowed` | The namespace allowlist — THE write guardrail (comma-separated, `fnmatch` globs like `team-*`). | `""` = **default-deny**: the server starts, probes pass, and every write is refused loudly. A deployment without it is a no-op writer by design. |
| `namespaces.blocked` | Blocklist that ALWAYS wins over the allowlist. | `""` (no explicit blocklist; cluster-scoped/Secret refusals still apply). |
| `ezua.enabled` + `ezua.domainName` + `ezua.virtualService.endpoint` | Gateway exposure through the PCAI Istio gateway. | `ezua.enabled: false` = ClusterIP-only (in-cluster MCP clients only). |

```yaml
# Required block — adjust the # SITE: lines and apply
namespaces:
  allowed: "team-alpha,mcp-demo"     # SITE: namespaces this server may write in
  blocked: "kube-system,kube-public,kube-node-lease"   # SITE: always wins
ezua:
  enabled: true                      # SITE: deploy the VirtualService
  domainName: <your-domain>          # SITE: literal cluster domain
  virtualService:
    endpoint: applygate-mcp.<your-domain>   # SITE: host on the ezaf-gateway
    istioGateway: istio-system/ezaf-gateway
    timeout: 120s
```

## Optional values (all have chart defaults)

| Key | Default | Notes |
|---|---|---|
| `kinds.allowed` | `ConfigMap,Service,Deployment,StatefulSet,Job,CronJob,Ingress,ServiceAccount,PodDisruptionBudget,HorizontalPodAutoscaler` | Narrows the write surface. Can NEVER widen it: `Secret` and cluster-scoped kinds are refused even if allowlisted, and kinds unknown to the built-in registry are refused because their scope cannot be proven. |
| `webui.enabled` | `true` | The strictly read-only HPE-branded console at `/` (+ `/api/*`). It exposes NO apply/delete endpoints; `false` strips `/`, `/ui`, `/api/*` while `/mcp` and health endpoints keep serving. |
| `persistence.enabled` / `size` / `accessModes` / `storageClass` | `true` / `1Gi` / `ReadWriteOnce` | Audit-trail PVC at `/data`. With `false`, an `emptyDir` is mounted instead (trail lost on restart — labs only); `readOnlyRootFilesystem` keeps working either way. |
| `audit.file` | `/data/audit.jsonl` | JSONL audit sink path inside the volume. |
| `deployment.replicaCount` | `1` | Stateless MCP 2.0 — any replica serves any request. |
| `image.repository` / `tag` / `pullPolicy` | chart-managed | Kept in lockstep with `Chart.yaml`/`pyproject.toml` by release tooling — leave at the chart default; pinning a stale tag in a site file is how "old server" pods happen. |
| `imagePullSecrets` | `[]` | Only if the GHCR package is private (public packages pull anonymously). |
| `resources` | `100m`/`256Mi` requests, `1`/`512Mi` limits | Modest; this is a policy gate, not a compute node. |
| `securityContext`, `podSecurityContext`, `containerSecurityContext` | non-root uid/gid 10001, `readOnlyRootFilesystem`, drop ALL caps, RuntimeDefault seccomp | Keep. The k8s client needs a writable `/tmp` — the chart mounts an `emptyDir` there. |
| `serviceAccount.create` / `name`, `rbac.create` | `true` / `applygate-mcp` / `true` | Creates the ServiceAccount + the namespaced writer Role/RoleBinding. |
| `hpe_proxies` + `proxy.http/https/noProxy` | `false` | Fleet-consistency block. The only peer is the in-cluster API, covered by the NO_PROXY cluster-local entries; `*_PROXY` env is wired only when `hpe_proxies=true`. |
| `kyverno.enabled` | `false` | Pre-install ClusterPolicy stamping `hpe-ezua/*` vendor labels. Cluster-scoped, so off by default — enable where EZUA labeling is enforced. |
| `service.type` / `port` / `targetPort` | `ClusterIP` / `9102` | Don't move the port without moving the probes' target. |

## Underlying detail: values → environment variables

`helm/values.yaml` does not template env vars (Helm cannot template inside a
values file), so `templates/deployment.yaml` renders them; you normally never
touch these directly:

| Env var | Rendered from |
|---|---|
| `APPLYGATE_ALLOWED_NAMESPACES` | `namespaces.allowed` |
| `APPLYGATE_BLOCKED_NAMESPACES` | `namespaces.blocked` |
| `APPLYGATE_ALLOWED_KINDS` | `kinds.allowed` |
| `APPLYGATE_AUDIT_FILE` | `audit.file` |
| `APPLYGATE_WEBUI_ENABLED` | `webui.enabled` |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (+ lowercase) | `proxy.*`, only when `hpe_proxies=true` |

## Gateway exposure (ezua / Istio)

When `ezua.enabled=true` the chart renders one VirtualService on
`istio-system/ezaf-gateway`: `/mcp` routes to the MCP server (port 9102,
`ezua.virtualService.timeout`), and when `webui.enabled` the root route sends
`/` and `/api/*` to the same service. The console is strictly read-only, so
the gateway surface gains no mutation path — but this is a WRITE-path server:
expose it only where the gateway enforces real auth (SSO/bearer at the
ezaf-gateway). This chart deliberately ships no auth template; where your
PCAI build offers the oauth2-proxy extension-provider pattern (see
prometheus-mcp's `ezua.authorizationPolicy`), apply the equivalent policy for
this host at the platform level. PCAI resolves `${DOMAIN_NAME}` in ezua
values before rendering on current builds; if your build does not, write the
literal domain — an unresolved placeholder registers a gateway host that
matches nothing (the route silently vanishes).

## Cross-namespace writes (operator bootstrap, one-time)

The release's Role can only grant access inside its own namespace — that is a
PCA I constraint, not a choice. To let the server write into OTHER
namespaces, an admin with rights over those namespaces applies a one-time
bootstrap manifest that creates the same writer Role + RoleBinding per target
namespace, bound to the release's ServiceAccount (see
`helm/local/rbac-bootstrap.se-g2.yaml` for the working shape — local-only,
never packaged). The server's `namespaces.allowed` must stay a subset of the
namespaces that bootstrap covers: a namespace in the policy without a
bootstrap Role means the tool's policy passes but the API answers 403. Revoke
by removing the namespace from both places — the tool-level blocklist always
wins.

## Upgrading

Upgrades are a values edit + re-apply, not a redeploy:

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the previous `# SITE:`-marked values carry
   over unchanged (maps merge recursively; lists replace, not append).
3. Image tags flow from the chart (`image.tag` is release-managed); a new
   chart version rolls the Deployment with the new tag.
4. The audit PVC persists across upgrades — the trail survives restarts and
   version bumps.

Sanity check after any change: the health endpoint reports
`namespaces_enabled` — `"false"` means probes pass but the allowlist is empty
and every write is refused.
