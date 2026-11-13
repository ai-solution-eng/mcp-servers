# Deployment — prometheus-mcp

## 1. The deployment contract

Deployment is a values problem. The chart is **packaged and imported into
PCAI once** (AI Essentials / EzAppConfig import — PCAI uploads it to
ChartMuseum and its controller installs it); after that, every
site-specific decision below is a value, edited in the chart's **Helm
Values** editor (or the PCAI API) and re-applied. You never run
`helm install` or `kubectl apply` for the deployment itself; every
`helm --set a.b=c` corresponds 1:1 to a values key.

**What PCAI does for you:** resolves `${DOMAIN_NAME}` in ezua values
before rendering (keep the placeholder — note `ezua.domainName` is
informational only; the gateway host comes solely from
`ezua.virtualService.endpoint`), creates the target namespace, and installs
through its controller.

**What you must do out-of-band**, because it cannot be a value: point the
chart at a reachable Prometheus (a value — but the Prometheus itself must
exist), and optionally pre-deploy the API-key Secret (§2.1). This chart has
the lightest prerequisite set in the MCP fleet — no mandatory Secret, no
PVC by default, no cross-namespace RBAC.

**Upgrade** = import the new chart version, keep the values, re-apply (§5).
Operators running plain Helm do the same with `-f <values-file>`;
per-cluster values live in `helm/local/` (gitignored, hardlink-ignored,
never packaged).

## 2. Prerequisites

### 2.1 Secrets the operator creates

| Secret | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| `prometheus-mcp-apikey` (OPTIONAL — name is your choice, wired via `apiKey.existingSecret`; default `existingSecretKey: api-keys`) | `api-keys` (comma-separated list) | `templates/deployment.yaml` → `PROMETHEUS_API_KEYS`, rendered ONLY when the values reference a Secret | The optional API-key gate on `/mcp` (fleet decision 2026-09, optional-by-design): a read-only observation surface behind the SSO-gated edge gateway may run open | With no Secret referenced: **nothing breaks** — the server runs open with a loud startup warning naming the env vars. With a Secret referenced but missing: pod sits in **`CreateContainerConfigError`** | `kubectl -n <ns> create secret generic prometheus-mcp-apikey --from-literal="api-keys=$(openssl rand -hex 32)"` |

The chart never inlines a key. Rotation is zero-downtime
(`api-keys=new,old`; env re-read per request). The gate scopes to `/mcp`
ONLY — `/health`, `/healthz`, console, `/api/*`, `/metrics` stay key-free
so kubelet probes and the ServiceMonitor scrape work. In-cluster callers
(LLM gateway, DSH) present the key in `Authorization: Bearer …` or
`X-API-Key` — update those clients' configs in the same change.

### 2.2 Patches / cluster objects (the things that are not values)

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| Namespace with `hpe-ezua` labels | PCAI convention for platform-managed vendor services. | Platform tooling cannot recognize the workload. | `kubectl get ns <ns> --show-labels` |
| Kyverno ClusterPolicy (chart-rendered, UNGATED — pre-install hook, idempotent) | Stamps `hpe-ezua/*` vendor labels onto this release's workloads. | Cosmetic-to-platform: missing vendor labels on a cluster without Kyverno. The chart still installs (hook deletion policy tolerates absence of the CRD on current builds). | `kubectl get clusterpolicy | grep prometheus-mcp` |
| Istio `VirtualService` on `istio-system/ezaf-gateway` (chart-rendered; `ezua.enabled: true` is the chart DEFAULT here) | The product path: `/mcp` → MCP server (9095), `/` → web console. `ezua.virtualService.endpoint` is `required` — `templates/virtualservice.yaml` (and `authorizationpolicy.yaml` when the auth gate is on) abort the render with `Valid .Values.ezua.virtualService.endpoint is required !` on an empty endpoint. | Unresolved `${DOMAIN_NAME}` literal registers a host that matches nothing (route silently vanishes). With `ezua.enabled: false`: in-cluster Service access only. | `curl -I https://<endpoint>/healthz` from outside; `kubectl -n istio-system get virtualservice | grep prometheus-mcp` |
| Istio `AuthorizationPolicy` (optional, `ezua.authorizationPolicy.enabled: true`) | Gateway-level auth gate (action CUSTOM) delegating this host to the PCAI oauth2-proxy provider in `istio-system`. | Off = anonymous gateway reads (the default, an accepted lab trade for machine callers — see FEATURES.md auth posture). | Unauthenticated external `/mcp` call returns the oauth2-proxy redirect/challenge when on |
| ServiceMonitor (optional, `metrics.enabled: true`) — requires **prometheus-operator CRDs** on the cluster | Scrapes the opt-in `/metrics` endpoint into Prometheus. | Off = no self-metrics (default). On without the CRD: the render fails on the unknown kind. | `kubectl get servicemonitor -n <ns>` |
| `NetworkPolicy` — the MCP network zone (optional, `networkPolicy.enabled: false` default) | Default-deny ingress admitting only `authorizedClients.namespaces` (+ same-namespace pods when `networkPolicy.sameNamespace`, the edge-gateway pods via `ingressGatewayNamespace`/`ingressGatewayLabels` while `allowEzafGatewayIngress: true` — fleet doctrine keeps the browser path open). Kubelet probes: `probeCidrs` + `probeExceptCidrs` (a `0.0.0.0/0` probeCidrs with empty probeExceptCidrs FAILS the render). Optional egress governance: `networkPolicy.egress.enabled` + `egress.extraEgress`; DNS egress always rendered when egress mode is on. | Off = baseline. On without callers listed: they time out — and `monitoring` unlisted kills ServiceMonitor scraping **silently**. | From an allowed ns: `curl -m 3 http://<service>:9095/healthz`; from a non-listed ns: expect timeout |
| `PodDisruptionBudget` (optional, `podDisruptionBudget.enabled: false` default) | Voluntary-eviction guard: `minAvailable: 1` pins the single-replica default pod through a drain/apply window; raise `deployment.replicaCount` >= 2 first for drainable HA. | Off = baseline; `kubectl drain` evicts the sole replica during maintenance. | `kubectl -n <ns> get pdb` |
| GPU nodes with a working driver mount (only for NVLink auto-detect) | The `nvlinkAutodetect` DaemonSet runs `nvidia-smi topo -m` from the host driver at node boot. | It fails soft — the GPU tab falls back to the built-in grouping (or your `gpuNvlinkDomains` pin); the page never breaks. | `kubectl -n <ns> get ds -o wide` (detector pods Ready on GPU nodes) |

### 2.3 External dependencies

| Dependency | Purpose | How to prove reachability from the cluster |
|---|---|---|
| A reachable Prometheus HTTP API (`prometheusUrl`, default `http://kubeprom-prometheus.prometheus.svc.cluster.local:9090` — the kube-prometheus-stack default, verified on G2) | Defines what this deployment IS — the thing it observes | From a pod in the cluster: `kubectl -n <ns> exec deploy/prometheus-mcp -- python3 -c "import urllib.request;print(urllib.request.urlopen('http://kubeprom-prometheus.prometheus.svc.cluster.local:9090/-/healthy').status)"` — or simply run `prom_query` for `up` and read the result. **Prove it from the cluster, not your laptop** — a laptop-reachable but cluster-unreachable URL produces a deploying pod whose every tool call errors. |
| Pushgateway (for the NVLink detector, `nvlinkAutodetect.pushgatewayUrl`) | The bridge the detector pushes island topology through | Detector pod logs show successful pushes; the GPU tab renders islands |
| (Optional) The PCAI oauth2-proxy extension in `istio-system` | The auth provider behind `ezua.authorizationPolicy` | Gateway challenge on unauthenticated external calls |

If your Prometheus requires auth, `PROM_BEARER_TOKEN_ENV` names an env var
holding a bearer token — the token VALUE never appears in config; wire the
env out-of-band (Secret) since the chart does not render this var.

## 3. Values

### 3.1 Required values

| Key | Why it is required | Default |
|---|---|---|
| `prometheusUrl` | Defines what this deployment IS — the Prometheus it observes. | `http://kubeprom-prometheus.prometheus.svc.cluster.local:9090` (kube-prometheus-stack default, verified on G2). Adjust the release/namespace for your cluster. |
| `ezua.enabled` + `ezua.virtualService.endpoint` | Gateway exposure — MCP clients run OUTSIDE the cluster, so the gateway route is the product path. | `ezua.enabled: true` in the chart; keep the `${DOMAIN_NAME}` placeholder (PCAI resolves it before rendering). `ezua.domainName` is informational only — no template reads it. |

```yaml
# Required block — adjust the # SITE: lines and apply
prometheusUrl: http://kubeprom-prometheus.prometheus.svc.cluster.local:9090   # SITE: your in-cluster Prometheus
ezua:
  enabled: true                      # SITE: deploy the VirtualService
  domainName: ${DOMAIN_NAME}          # informational — no template reads it
  virtualService:
    endpoint: prometheus-mcp.${DOMAIN_NAME}   # SITE: host on the ezaf-gateway
    istioGateway: istio-system/ezaf-gateway
    timeout: 660s                    # long on purpose: wide range queries run for minutes
```

### 3.2 Optional values (all have chart defaults)

**GPU topology and node views:**

| Key | Default | Notes |
|---|---|---|
| `gpuNvlinkDomains` | `""` | GPU-tab NVLink island grouping — JSON string or YAML list of GPU-index groups (`"[[0,1,2,3],[4,5,6,7]]"`). Precedence: this override > auto-detection > built-in default (two 4-GPU islands per 8-GPU node). Invalid values fall back server-side; they never break the pod. |
| `nodeFilter` | `""` | Nodes-tab default selection regex, applied to node names (rendered as `PROM_UI_NODE_FILTER`). Precedence: the browser `?filter=` input > this regex > built-in default (GPU nodes, auto-detected from the nvidia GPU allocatable; none detected → every node). `all` shows every node; an invalid or empty-matching regex falls back server-side — it never empties the tab. |
| `nvlinkAutodetect.enabled` | `true` | The two-container DaemonSet that detects real NVLink islands at GPU-node boot. Disable when you pin `gpuNvlinkDomains` and want no resident detector pods. Fails soft. |
| `nvlinkAutodetect.pushInterval` | `20` | Re-push cadence — MUST stay under the pushgateway's `--metric.timetolive=30s` or the metric evaporates. |
| `nvlinkAutodetect.pushgatewayUrl` / `nodeSelector` / `image` / `privileged` / `driverInstallDir` | pushgateway service DNS / `nvidia.com/gpu.present: "true"` / `python:3.12-slim` / `true` / `/run/nvidia/driver` | Defaults match kube-prometheus-stack + GPU-operator clusters; no driver-image pinning anywhere. |

**Persistence (saved-query store):**

| Key | Default | Effect |
|---|---|---|
| `persistence.enabled` | `false` | `query_save`/`query_list`/`query_delete`/`query_saved` keep their JSON store in memory unless this wires `PROMETHEUS_SAVED_QUERIES_PATH` to `<mountPath>/<savedQueriesFile>` on a dedicated PVC (`<deployment.name>-data`) — every pod update/reschedule otherwise silently forgets every saved query. |
| `persistence.mountPath` / `savedQueriesFile` | `/data` / `saved-queries.json` | Where the store volume mounts; the file name is pinned explicitly so a moved `mountPath` cannot silently orphan an existing store. |
| `persistence.size` / `storageClass` | `1Gi` / `""` | PVC request; empty = cluster default StorageClass. |
| `persistence.accessModes` | `[ReadWriteOnce]` | `replicaCount > 1` with a RWO class strands extra replicas Pending — flip to a RWX class (gl4f-filesystem RWX on G2) or keep 1 replica (the fleet default). |
| `persistence.shared` | `false` | **Per-replica honesty:** the store is per-replica; `replicaCount > 1` fragments stores. Set `true` ONLY when the store is genuinely shared (single replica, or an RWX volume every replica mounts); it renders `PROMETHEUS_SAVED_QUERIES_SHARED=1` and silences the `query_save` warning. A declaration about the deployment, NOT concurrency control — there is no cross-process locking. |

**Workload and exposure:**

| Key | Default | Notes |
|---|---|---|
| `image.repository` / `tag` / `pullPolicy` | chart-managed / **`Always`** | `pullPolicy: Always` is deliberate: an early mutable tag was overwritten in the registry, and `IfNotPresent` would silently serve a stale cached digest. Keep it until a fresh immutable tag regime is in place. |
| `imagePullSecrets` | `[]` | Only if the GHCR package is private (public packages pull anonymously). |
| `deployment.replicaCount` | `1` | Stateless MCP 2.0 — any replica serves any request. |
| `deployment.appName` | `prometheus-mcp` | Label/selector + container name on Deployment, Service, VirtualService — not the release name (`deployment.name` is, and names the PVC). Leave at the default; mismatched selectors break the Service/VS wiring. |
| `resources` | `100m`/`128Mi` requests, `512Mi` memory limit | A query proxy, not a compute node. |
| `service.type` / `port` / `targetPort` | `ClusterIP` / `9095` | Don't move the port without moving the probes' target. |
| `metrics.enabled` (+ `serviceMonitor` / `interval`) | `false` | `GET /metrics` self-metrics (one counter family: per-tool request counts with outcome ok/error — no PromQL text, label names, or error strings exported). Default OFF renders no env and no ServiceMonitor; when on, `/metrics` is served key-free on the same port. Requires prometheus-operator CRDs. |
| `apiKey.existingSecret` (+ `existingSecretKey`) | `""` / `api-keys` | OPTIONAL API-key gate on `/mcp` — rendered ONLY when the values point at a pre-deployed Secret; empty (default) = the server runs open with the startup warning. The chart NEVER creates the Secret and never inlines a key. |
| `ezua.authorizationPolicy.enabled` | `false` | Gateway-level auth gate (Istio `AuthorizationPolicy`, action CUSTOM) delegating this host to the PCAI **oauth2-proxy** extension — external callers then need a valid PCAI token. OFF by default: the rotating 30-min SSO token is a known pain for machine MCP callers. `namespace: istio-system`, `providerName: oauth2-proxy`. |
| `extraAllowedHosts` | `[]` | EXTRA in-cluster svc-DNS Host allowlist entries for the SDK's DNS-rebinding protection (joined into `MCP_EXTRA_ALLOWED_HOSTS`; verbatim or `host:*`). With the pinned `MCP_HOSTNAME` this turns Host-header protection ON; with NEITHER set the SDK's implicit loopback-only protection applies. When transport security is active the chart AUTO-prepends the release's own service DNS as the first entry. |
| `networkPolicy.*` | `enabled: false` | The MCP network zone — see §2.2 for the sub-keys. Hardened profile: `helm/values-examples/values-hardened-g2.yaml`. |
| `podDisruptionBudget.enabled` / `.minAvailable` | `false` / `1` | See §2.2. |

There is deliberately NO `webui` toggle: the read-only console at `/` and
its `/api/*` endpoints are always served by the same container (every
dashboard card fails soft, and the API mirrors the MCP-side caps).

### 3.3 Underlying detail: values → environment variables

`templates/deployment.yaml` renders the env vars (you normally never touch
these directly):

| Env var | Rendered from |
|---|---|
| `PROM_URL` | `prometheusUrl` |
| `PROM_UI_GPU_NVLINK_DOMAINS` | `gpuNvlinkDomains` (JSON string or YAML list — both render to the same value; omitted when empty) |
| `PROM_UI_NODE_FILTER` | `nodeFilter` (omitted when empty — the built-in GPU-nodes default applies) |
| `PROMETHEUS_METRICS_ENABLED` | rendered `"true"` only when `metrics.enabled=true` (otherwise absent — no `/metrics` route) |
| `PROMETHEUS_API_KEYS` | rendered from the `apiKey.existingSecret` Secret reference ONLY when set (otherwise absent — `/mcp` runs open with the startup warning) |
| `MCP_HOSTNAME` | `ezua.virtualService.endpoint` whenever `ezua.enabled` + endpoint are set (DNS-rebinding Host pin) |
| `MCP_EXTRA_ALLOWED_HOSTS` | own service DNS first (`<deployment.name>-service.<ns>.svc.cluster.local:*`), then `extraAllowedHosts` joined with commas — rendered when transport security is active |
| `PROMETHEUS_SAVED_QUERIES_PATH` | `persistence.mountPath`/`persistence.savedQueriesFile` ONLY when `persistence.enabled=true` |
| `PROMETHEUS_SAVED_QUERIES_SHARED` | rendered `"1"` ONLY when `persistence.enabled=true` AND `persistence.shared=true` |

Server-side env knobs not wired by the chart (sane defaults built in,
documented for completeness): `PROM_TIMEOUT` (30 s), `PROM_MAX_SERIES` (20),
`PROM_MAX_POINTS` (60 per range series), `PROM_MAX_LABEL_VALUES` (200),
`PROM_BEARER_TOKEN_ENV` (name of an env var holding a bearer token, if your
Prometheus requires auth — the token value itself never appears in config),
`PROMETHEUS_MIN_STEP_SECONDS` (default 15 — see FEATURES.md D14), and
`PROMETHEUS_OVERVIEW_CACHE_TTL` (default 20).

## 4. Import and install (PCAI steps)

1. **Confirm the Prometheus target** — the kube-prometheus-stack service
   DNS your cluster actually runs (§2.3); you will paste it as
   `prometheusUrl`.
2. **(Optional) tighten auth** — pre-deploy the API-key Secret (§2.1) and/or
   plan `ezua.authorizationPolicy.enabled: true`.
3. **Import the packaged chart once** into PCAI (AI Essentials / EzAppConfig
   import).
4. **Create the deployment from the catalog entry**; PCAI creates the
   namespace and opens the chart's Helm Values editor.
5. **Paste a full values document** — `helm/values-examples/values.g2.yaml`
   or `values.hosted-trial.yaml` (full documents; the editor replaces the
   chart's bundled values), adjust every `# SITE:` line, keep
   `${DOMAIN_NAME}` as-is.
6. **Apply.** PCAI renders and installs; watch the pod come up (the
   NVLink DaemonSet joins if GPU nodes exist and `nvlinkAutodetect` is on).
7. **Verify:** `/healthz` answers; `prom_query` for `up` returns data (the
   §2.3 proof); the console dashboard renders; one MCP handshake per
   [VERIFICATION.md](VERIFICATION.md).

## 5. Upgrade / rollback / uninstall

Upgrades are a values edit + re-apply, not a redeploy:

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the `# SITE:`-marked values carry over
   unchanged (maps merge recursively; lists replace, not append).
3. Image tags flow from the chart; `pullPolicy: Always` means a re-apply
   picks up a rebuilt image even at the same tag.
4. The NVLink DaemonSet is part of the chart — an upgrade re-renders it (or
   removes it when `nvlinkAutodetect.enabled=false`).

**Rollback / uninstall (PCAI-way):** roll back by re-importing the previous
chart version and re-applying the same values (the values document is the
state). Uninstall = delete the deployment from the PCAI UI; the
operator-created Secret (if any) stays until deleted. The saved-query PVC
survives uninstall — delete it explicitly if you want the store gone.

Plain-Helm equivalent (developer/secondary path):
`helm upgrade prometheus-mcp helm/ -n <ns> --reuse-values --set apiKey.existingSecret=...`
style applies; render-check with `helm template`. NOT the PCAI path.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| New pod crash-loops at startup (`ModuleNotFoundError: mcp_metrics`) while old pods look fine | A REBUILT image from the pre-src-layout packaging silently omitted the declared py-module (W7 fix — caught live during the 2026-10 deploy observation); the src-layout + explicit COPY + build-time import gate now prevent it | Rebuild from the current tree (`src/prometheus_mcp/` layout); check the build log's import gate before touching values |
| Pod runs an old image despite a new tag | `pullPolicy` was `IfNotPresent` while an early mutable tag got overwritten in the registry — a stale cached digest served silently | The chart pins `pullPolicy: Always` for exactly this reason; keep it until an immutable-tag regime exists |
| Every tool call errors with connection refused / timeout to the Prometheus service | `prometheusUrl` points at a release/namespace your cluster does not run (the kube-prometheus-stack default does not fit every install) | Find the real service (`kubectl get svc -A | grep prometheus`), update `prometheusUrl`, re-apply; prove with the §2.3 in-cluster check |
| Extra replicas stuck Pending after scaling up | `persistence.accessModes: [ReadWriteOnce]` + a RWO StorageClass — one node can mount it | Flip to a RWX class (`gl4f-filesystem` on G2) or keep `replicaCount: 1` (fleet default) |
| Saved queries vanish after every pod update | `persistence.enabled: false` (default) — the store is in-memory only, by design until wired | Set `persistence.enabled: true` (+ RWX class if multi-replica, + `shared: true` to declare the store shared) |
| GPU tab shows the built-in grouping, not the real islands | Detector DaemonSet not running (no GPU nodes / driver mount) or the pushgateway TTL ate the metric | Check detector pods + logs; keep `nvlinkAutodetect.pushInterval` (20) under the pushgateway's 30s TTL; or pin `gpuNvlinkDomains` |
| External callers suddenly need a browser login on every call | `ezua.authorizationPolicy.enabled: true` — the gateway delegates to oauth2-proxy and machine callers get the rotating-SSO-token pain | Accept the trade (lab), or exempt this host by flipping the gate off and using the `/mcp` API-key gate instead |
| `Valid .Values.ezua.virtualService.endpoint is required !` at render | `ezua.enabled: true` (chart default) with a blank endpoint | Set the endpoint — unique per release on the shared gateway |
| External clients get nothing on the gateway host; in-cluster works | Unresolved `${DOMAIN_NAME}` literal on a plain-Helm site | Apply through PCAI (which resolves the placeholder) or write the literal domain |
| Prometheus stops scraping `/metrics` after enabling the network zone | `monitoring` not in `authorizedClients.namespaces` — the deny rule matches the scrape namespace silently | Keep `monitoring` listed whenever the ServiceMonitor is on |

## 7. Rebuilding the image (W7 packaging fix)

Site operators rarely rebuild, but if you do, know the failure mode this
chart just fixed: `server.py` imports `mcp_metrics` (the Wave-3
instrumentation), and the pre-src-layout loose-scripts packaging
(`[tool.setuptools] py-modules`) **silently omits a declared module whose
file is not in the build context** — a rebuilt image without the explicit
COPY crash-looped at startup (`ModuleNotFoundError`) while probes looked
fine on the old pods. The app now ships as the src-layout package
(`src/prometheus_mcp/`): the whole package rides one `COPY src/ ./src/` +
the pyproject `packages` declaration, the fleet-hardlinked `mcp_auth.py`
stays a top-level py-module beside it (`COPY mcp_auth.py ./`), and a
build-time sanity gate imports every shipped module of the INSTALLED
package so packaging drift fails in the build log instead of in a pod.
Setuptools still silently omits anything missing from the build context, so
both COPYs remain load-bearing. The image also carries the fleet's
build-time permission neutralizer (`chmod -R a+rX /app`) — files created by
agents land `0600` on the ops box and COPY preserves modes, so a non-root
container would otherwise fail on the first unreadable module. If your
rebuild crash-loops, check both before touching values.

## 8. Deployment targets

Behavior that differs by target, and the paste-ready values for each
(`helm/values-examples/` — sanitized; real per-site values live in
`helm/local/`). This server has no mandatory API-key Secret — the auth
posture is entirely the `ezua.authorizationPolicy` gate above.

### Proxied corporate site (SITE: your-cluster.example)

- **Domain.** PCAI envsubsts `${DOMAIN_NAME}` in pasted values before
  rendering, so the hosted-trial placeholders work as-pasted; the G2 site
  files deliberately carry the literal domain instead (the G2 example ships
  it already; `ezua.domainName` is informational).
- GPU wiring: `gpuNvlinkDomains` pinned to the operator-confirmed two
  4-GPU islands, `nvlinkAutodetect.enabled: false` (no resident detector
  pods); `persistence.enabled: true` (RWX `gl4f-filesystem`, `shared: true`
  — single replica) so saved queries survive pod updates; metrics +
  ServiceMonitor on; no `apiKey` block (the server runs open behind the
  SSO-gated edge, per the optional-by-design decision).
- Sanitized example: [helm/values-examples/values.g2.yaml](../helm/values-examples/values.g2.yaml).

### Hosted trial (customer-hosted PCAI)

- **`${DOMAIN_NAME}` placeholders stay as-is** (PCAI resolves them before
  rendering on current builds; substitute the literal domain only on a build
  that does not — an unresolved placeholder registers a gateway host that
  matches nothing).
- `nvlinkAutodetect.enabled: false` on CPU-only trial clusters (`true` where
  GPU nodes exist); `persistence.enabled: false` keeps the chart's default
  in-memory saved-query store unless the trial wants durability.
- Decide the auth posture explicitly: `ezua.authorizationPolicy.enabled`
  (`false` = anonymous reads through the gateway — acceptable for a
  read-only surface where the trial posture allows; `true` = PCAI
  SSO/bearer enforced at the gateway).
- Metrics off keeps the render minimal.
- Sanitized example: [helm/values-examples/values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml).
