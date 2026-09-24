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
| `workbench.templates` | `{}` | Workspace templates for `workspace_create(name, template=<name>)` — **default OFF: an empty object renders no env at all and the tool's `template` parameter is refused**, exactly the pre-templates behavior. Each named template may only (a) **widen** that workspace's exec allowlist with operator-supplied *bare binary names* (`extra_allowed` — the denylist still wins, so a template can never re-enable a denied binary) and (b) pre-run `canned_setup` argv commands **inside the new workspace through the exact `run_command` machinery** (confinement, server-PATH allowlist resolution, timeouts, output caps, audit — each setup run is audited as a `workspace_template_setup` event carrying the template name). Only the template *name* persists in the workspace; the widened allowlist is re-derived from the current values on every call, so removing a template shrinks its workspaces back to the base allowlist. See the `helm/values.yaml` comment block for a worked example. |
| `metrics.enabled` (+ `serviceMonitor` / `interval`) | `false` | `GET /metrics` self-metrics (per-tool request counters, nothing else). Default OFF renders no env and no ServiceMonitor — the default pod has no `/metrics` route; when on, `/metrics` is key-free like the probes. |
| `apiKey.existingSecret` / `existingSecretKey` | `workbench-mcp-apikey` / `api-keys` | **Mandatory wiring, never created by the chart**: every route except `/health`/`/healthz` requires an API key, so the Secret must exist in the target namespace before `helm install` or the pod sits in `CreateContainerConfigError`. Comma-separated keys (`api-keys=new,old`) are the zero-downtime rotation mechanism (env re-read per request). |
| `webui.enabled` | `true` | The HPE-branded console at `/` — workspace switcher, file tree + viewer/saver, env editor, run-command console, audit tail. The UI is read/write and calls the SAME core functions (confinement, caps, allowlist, audit all still apply). Auth posture: the console HTML (`/`, `/ui`) is public-but-inert (an in-page unlock bar collects the key — the browser cannot load the page behind a 401); every `/api/*` data route stays API-key-gated, and the endpoint sits behind gateway authn. |
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
| `WORKBENCH_TEMPLATES` | `workbench.templates` as JSON — rendered ONLY when the object is non-empty (default: no env at all, template parameter refused) |
| `WORKBENCH_METRICS_ENABLED` | rendered `"true"` only when `metrics.enabled=true` (otherwise absent — no `/metrics` route) |
| `HOME` | fixed `/tmp` (read-only-rootfs scratch so pip/tempfiles keep working; resets with the pod — workspaces persist on the PVC) |
| `WORKBENCH_API_KEYS` | `apiKey.existingSecret{,Key}` — always from the operator-created Secret |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (+ lowercase) | `proxy.*`, only when `hpe_proxies=true` (passed through to `run_command` children) |
| `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `PIP_CERT` | `caCert.*`, only when `caCert.enabled=true` |

Numeric caps tolerate helm's float-ish rendering (`8.388608e+06`) — the
server parses them defensively.

## Gateway exposure (ezua / Istio)

When `ezua.enabled=true` the chart renders one VirtualService on
`istio-system/ezaf-gateway`: `/mcp` routes to the MCP server (port 9103,
`timeout: 660s`), and the root route sends `/`, `/ui`, `/api/*` and the
health endpoints to the same service. Trust note: the console HTML at `/`
and `/ui` is public-but-inert (an in-page unlock bar collects the API key —
the browser cannot load the page behind a 401), while every `/api/*` data
route and `/mcp` stay API-key-gated at the pod — gateway authn (this
ezaf-gateway VirtualService) is the additional layer between it and a
browser. The
MCP tools carry their own confirm gates and audit trail; the browser surface
adds convenience, not guardrails. PCAI resolves `${DOMAIN_NAME}` in ezua
values before rendering on current builds; if your build does not, write the
literal domain — an unresolved placeholder registers a gateway host that
matches nothing.

## Deployment targets

Behavior that differs by target, and the paste-ready values for each
(`helm/values-examples/` — sanitized; real per-site values live in
`helm/local/`):

### Internal G2 (SE-G2 lab cluster, `pcai-se-ai-application.hst.rdlabs.hpecorp.net`)

- **Literal domain.** This PCAI build does not envsubst `${DOMAIN_NAME}` —
  write the literal domain into `ezua.domainName` and
  `ezua.virtualService.endpoint` (the G2 example ships it already).
- **Proxy + MITM CA on** — `hpe_proxies: true` and `caCert.enabled: true`
  (the `ezaf-root-ca` ConfigMap present in the release namespace): this is
  what lets `pip install ...` inside `run_command` reach PyPI through the
  HPE proxy. Note the interplay with fleet decision D18: `pip` must also be
  allow-listed (`workbench.execAllowlist`) for it to run at all — the G2
  site chooses its allowlist deliberately.
- Fleet API key (Secret `mcp-fleet-apikeys`, key `api-keys`), Kyverno
  vendor-label policy on, metrics + ServiceMonitor on.
- Sanitized example:
  [helm/values-examples/values.g2.yaml](../helm/values-examples/values.g2.yaml).

### Hosted trial (customer-hosted PCAI)

- **`${DOMAIN_NAME}` placeholders stay as-is** (PCAI resolves them before
  rendering on current builds; substitute the literal domain only on a build
  that does not).
- **Keep the D18 narrow allowlist** (`ls,cat,...,uniq` — no interpreters, no
  package managers) unless the trial explicitly needs them; the trial posture
  leans on the documented defense-in-depth (no SA token, read-only rootfs,
  argv screening).
- `hpe_proxies: false` / `caCert.enabled: false` unless the trial cluster
  egresses via the HPE proxy; `kyverno.enabled: false` unless the platform
  enforces vendor labels; metrics off keeps the render minimal.
- API-key Secret provisioned out of band per the customer's key process
  (`mcp-fleet-apikeys` convention or the customer's own Secret name) — every
  route except `/health`/`/healthz` is keyed, so the read/write web UI also
  sits behind the key plus the gateway authn.
- Sanitized example:
  [helm/values-examples/values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml).

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
