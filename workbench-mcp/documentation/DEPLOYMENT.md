# Deployment — workbench-mcp

Deployment is a values problem: import the packaged chart into PCAI once,
then everything below is edited in the chart's values (PCAI **Helm Values**
editor, or the PCAI API) and re-applied. Operators running plain Helm do the
same with `-f <values-file>`; per-cluster values live in `helm/local/`
(gitignored, hardlink-ignored, never packaged).

## Required values

| Key | Why it matters | Default |
|---|---|---|
| `ezua.enabled` + `ezua.domainName` + `ezua.virtualService.endpoint` | Gateway exposure — MCP harnesses run OUTSIDE the cluster, so the gateway route is the product path. The `/mcp` timeout is generous because `run_command` can legitimately run to its max (600 s). | `ezua.enabled: false` = ClusterIP-only (in-cluster MCP clients only). |

```yaml
# Required block — adjust the # SITE: lines and apply
ezua:
  enabled: true                      # SITE: deploy the VirtualService
  domainName: <your-domain>          # SITE: literal cluster domain
  virtualService:
    endpoint: workbench-mcp.<your-domain>   # SITE: /mcp -> MCP server; / -> web UI
    istioGateway: istio-system/ezaf-gateway
    timeout: 660s
```

## Optional values (all have chart defaults)

| Key | Default | Notes |
|---|---|---|
| `persistence.enabled` / `size` / `mountPath` / `accessModes` / `storageClass` | `true` / `10Gi` / `/data` / `ReadWriteMany` | THE point of the workbench: workspaces must survive pod restarts. RWX so every replica sees the same workspaces; single-replica clusters without an RWX class can drop to `ReadWriteOnce` with `replicaCount: 1`. `workbench.root` MUST equal `persistence.mountPath` (both default `/data`). |
| `workbench.root` | `/data` | PVC mount holding all workspaces. |
| `workbench.execAllowlist` | `python3,pip,pip3,ls,cat,head,tail,grep,find,wc,du,df,mkdir,touch,cp,mv,tar,git,diff,sort,uniq` | argv[0] allowlist for `run_command` — anything not listed is refused; no shells by design. |
| `workbench.execDenylist` | `curl,wget,sudo,su,nc,ncat,ssh,scp,setsid` | Hard deny — WINS over the allowlist. |
| `workbench.execTimeoutDefault` / `execTimeoutMax` | `60` / `600` s | Per-command bounds (per-call `timeout_s` is clamped into these). |
| `workbench.maxFileBytes` | `8388608` (8 MiB) | `write_file` cap. |
| `workbench.maxOutputBytes` | `204800` (200 KiB) | stdout/stderr cap per `run_command`. |
| `workbench.maxListEntries` | `500` | Entries per `list_files` response. |
| `workbench.logLevel` | `INFO` | Server logging level. |
| `webui.enabled` | `true` | The HPE-branded console at `/` — workspace switcher, file tree + viewer/saver, env editor, run-command console, audit tail. The UI is read/write and calls the SAME core functions (confinement, caps, allowlist, audit all still apply) but is unauthenticated at the pod: keep it behind gateway authn. |
| `deployment.replicaCount` | `1` | Stateless MCP — any replica serves any request (needs the RWX volume to share state). |
| `image.repository` / `tag` / `pullPolicy` | chart-managed | Kept in lockstep with `Chart.yaml` by release tooling — leave at the chart default; a stale tag in a site file is how an "old MCP server" pod happens. |
| `imagePullSecrets` | `[]` | Only if the GHCR package is private (public packages pull anonymously). |
| `resources` | `250m`/`256Mi` requests, `1`/`512Mi` limits | A scratch pad, not a compute node. |
| `securityContext` | non-root uid 10001, `fsGroup: 10001` | Keep — `fsGroup` is what makes the mounted PVC writable by the server user. |
| `hpe_proxies` + `proxy.http/https/noProxy` | `false` | When `true`, proxy env lands on the pod AND passes through to `run_command` children — this is what makes `pip install ...` work inside a workspace on a cluster with no direct internet egress. NO_PROXY keeps in-cluster traffic direct. |
| `caCert.enabled` / `configMap` / `configMapKey` | `false` / `ezaf-root-ca` / `ezaf-root-ca.crt` | The HPE proxy re-terminates TLS; when enabled, mounts the namespace's `ezaf-root-ca` ConfigMap and points `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`PIP_CERT` at it for the container AND every `run_command` child (copy the ConfigMap into the release namespace if absent). |
| `kyverno.enabled` | `false` | Pre-install ClusterPolicy stamping `hpe-ezua/*` vendor labels. Cluster-scoped, so off by default (also keeps clusters without the Kyverno CRD installable). |
| `service.type` / `port` / `targetPort` | `ClusterIP` / `9103` | Don't move the port without moving the probes' target. |

## Underlying detail: values → environment variables

`templates/deployment.yaml` renders the env vars (you normally never touch
these directly):

| Env var | Rendered from |
|---|---|
| `WORKBENCH_ROOT` | `workbench.root` (must match `persistence.mountPath`) |
| `WORKBENCH_EXEC_ALLOWLIST` / `WORKBENCH_EXEC_DENYLIST` | `workbench.execAllowlist` / `execDenylist` |
| `WORKBENCH_EXEC_TIMEOUT_DEFAULT` / `WORKBENCH_EXEC_TIMEOUT_MAX` | `workbench.execTimeoutDefault` / `execTimeoutMax` |
| `WORKBENCH_MAX_FILE_BYTES` / `WORKBENCH_MAX_OUTPUT_BYTES` / `WORKBENCH_MAX_LIST_ENTRIES` | `workbench.maxFileBytes` / `maxOutputBytes` / `maxListEntries` |
| `WORKBENCH_UI_ENABLED` | `webui.enabled` |
| `WORKBENCH_LOG_LEVEL` | `workbench.logLevel` |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (+ lowercase) | `proxy.*`, only when `hpe_proxies=true` (passed through to `run_command` children) |
| `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `PIP_CERT` | `caCert.*`, only when `caCert.enabled=true` |

Numeric caps tolerate helm's float-ish rendering (`8.388608e+06`) — the
server parses them defensively.

## Gateway exposure (ezua / Istio)

When `ezua.enabled=true` the chart renders one VirtualService on
`istio-system/ezaf-gateway`: `/mcp` routes to the MCP server (port 9103,
`timeout: 660s`), and the root route sends `/`, `/ui`, `/api/*` and the
health endpoints to the same service. Trust note, verbatim from the chart:
the web UI is read/write and unauthenticated at the pod — gateway authn (this
ezaf-gateway VirtualService) is the only thing between it and a browser. The
MCP tools carry their own confirm gates and audit trail; the browser surface
adds convenience, not guardrails. PCAI resolves `${DOMAIN_NAME}` in ezua
values before rendering on current builds; if your build does not, write the
literal domain — an unresolved placeholder registers a gateway host that
matches nothing.

## Upgrading

Upgrades are a values edit + re-apply, not a redeploy:

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the `# SITE:`-marked values carry over
   unchanged (maps merge recursively; lists replace, not append).
3. Image tags flow from the chart; a new chart version rolls the Deployment
   with the new tag.
4. Workspaces live on the PVC and survive restarts AND upgrades — agents
   keep their files across the bump. `pvc.yaml` renders unchanged unless you
   change `persistence.*` (shrinking a PVC is not supported).
