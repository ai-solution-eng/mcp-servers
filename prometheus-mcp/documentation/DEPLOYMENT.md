# Deployment — prometheus-mcp

> **What changed (2026-10-07 doc wave):** the `nodeFilter` key and its
> `PROM_UI_NODE_FILTER` env are now documented (the Nodes-tab default
> selection shipped earlier but was missing from this walkthrough); a new
> "Rebuilding the image" note documents the W7 packaging fix — `mcp_metrics.py`
> was absent from published images (new pods crash-looped; caught live during
> the 2026-10 deploy observation) and now ships as a py-module + explicit
> Dockerfile COPY with a build-time import gate; and every G2/Hosted profile
> claim was re-verified against `helm/values.yaml` +
> `helm/values-examples/` (metrics on/off, persistence RWX + `shared`,
> NVLink detector off, literal-vs-`${DOMAIN_NAME}` domains).

Deployment is a values problem: import the packaged chart into PCAI once,
then everything below is edited in the chart's values (PCAI **Helm Values**
editor, or the PCAI API) and re-applied. Operators running plain Helm do the
same with `-f <values-file>`; per-cluster values live in `helm/local/`
(gitignored, hardlink-ignored, never packaged).

## Required values

| Key | Why it is required | Default |
|---|---|---|
| `prometheusUrl` | Defines what this deployment IS — the Prometheus it observes. | `http://kubeprom-prometheus.prometheus.svc.cluster.local:9090` (kube-prometheus-stack default, verified on G2). Adjust the release/namespace for your cluster. |
| `ezua.enabled` + `ezua.virtualService.endpoint` | Gateway exposure through the PCAI Istio gateway. MCP clients run OUTSIDE the cluster (DSH runs on the operator's laptop), so the gateway route is the product path. | `ezua.enabled: true` in the chart. PCAI resolves the `${DOMAIN_NAME}` placeholder in submitted values before rendering — keep it as-is. `ezua.domainName` is informational only; no template reads it (the gateway host comes solely from `endpoint`). |

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

## Optional values (all have chart defaults)

| Key | Default | Notes |
|---|---|---|
| `gpuNvlinkDomains` | `""` | GPU-tab NVLink island grouping — JSON string or YAML list of GPU-index groups (`"[[0,1,2,3],[4,5,6,7]]"`). Precedence: this override > auto-detection > built-in default (two 4-GPU islands per 8-GPU node). Invalid values fall back server-side; they never break the pod. |
| `nodeFilter` | `""` | Nodes-tab default selection regex, applied to node names (rendered as `PROM_UI_NODE_FILTER`). Precedence: the browser `?filter=` input > this regex > built-in default (GPU nodes, auto-detected from the nvidia GPU allocatable; none detected → every node). `all` shows every node; an invalid or empty-matching regex falls back server-side — it never empties the tab. |
| `nvlinkAutodetect.enabled` | `true` | The two-container DaemonSet that detects real NVLink islands at GPU-node boot (`nvidia-smi topo -m` from the host-installed driver → pushgateway → GPU tab). Disable when you pin `gpuNvlinkDomains` and want no resident detector pods. Fails soft: a node without a working driver mount never breaks the page. |
| `nvlinkAutodetect.pushInterval` | `20` | Re-push cadence — MUST stay under the pushgateway's `--metric.timetolive=30s` or the metric evaporates. |
| `nvlinkAutodetect.pushgatewayUrl` / `nodeSelector` / `image` / `privileged` / `driverInstallDir` | pushgateway service DNS / `nvidia.com/gpu.present: "true"` / `python:3.12-slim` / `true` / `/run/nvidia/driver` | Defaults match kube-prometheus-stack + GPU-operator clusters; no driver-image pinning anywhere. |
| `image.repository` / `tag` / `pullPolicy` | chart-managed / **`Always`** | `pullPolicy: Always` is deliberate: an early mutable tag was overwritten in the registry, and `IfNotPresent` would silently serve a stale cached digest. Keep it until a fresh immutable tag regime is in place. |
| `imagePullSecrets` | `[]` | Only if the GHCR package is private (public packages pull anonymously). |
| `deployment.replicaCount` | `1` | Stateless MCP 2.0 — any replica serves any request. |
| `resources` | `100m`/`128Mi` requests, `512Mi` memory limit | A query proxy, not a compute node. |
| `ezua.authorizationPolicy.enabled` | `false` | Gateway-level auth gate (Istio `AuthorizationPolicy`, action CUSTOM) delegating this host to the PCAI **oauth2-proxy** extension — external callers then need a valid PCAI token. OFF by default: the rotating 30-min SSO token is a known pain for machine MCP callers (accepted lab trade — flip on per environment if the trust posture changes). `namespace: istio-system`, `providerName: oauth2-proxy`. |
| `service.type` / `port` / `targetPort` | `ClusterIP` / `9095` | Don't move the port without moving the probes' target. |
| `metrics.enabled` (+ `serviceMonitor` / `interval`) | `false` | `GET /metrics` self-metrics (one counter family: per-tool request counts with outcome ok/error — no PromQL text, label names, or error strings exported). Default OFF renders no env and no ServiceMonitor — the default pod has no `/metrics` route; when on, `/metrics` is served key-free on the same port (the API-key gate below scopes to `/mcp` only) so the ServiceMonitor can scrape it. Requires prometheus-operator CRDs. |
| `apiKey.existingSecret` (+ `existingSecretKey`) | `""` | OPTIONAL API-key gate on `/mcp` (fleet decision 2026-09): rendered ONLY when the values point at a pre-deployed Secret — `/mcp` then requires a key (`PROMETHEUS_API_KEYS`; the fleet-universal `MCP_API_KEYS` is honored too), everything else (`/health`, `/healthz`, console, `/api/*`, `/metrics`) stays key-free. Empty (default) = the server runs open with a loud startup warning. The chart NEVER creates the Secret and never inlines a key. |
| `extraAllowedHosts` | `[]` | EXTRA in-cluster svc-DNS Host allowlist additions for the MCP SDK's DNS-rebinding protection (joined into `MCP_EXTRA_ALLOWED_HOSTS`; matched verbatim or as `host:*`). Together with the pinned `MCP_HOSTNAME` (rendered from `ezua.virtualService.endpoint` whenever ezua + endpoint are set) this turns Host-header protection ON; with NEITHER set the SDK's implicit loopback-only protection applies unchanged. When transport security is active, the chart AUTO-prepends the release's own service DNS (`<deployment.name>-service.<ns>.svc.cluster.local:*` — the gateway relay's Host header) as the first entry. |

There is deliberately NO `webui` toggle: the read-only console at `/` and its
`/api/*` endpoints are always served by the same container (every dashboard
card fails soft, and the API mirrors the MCP-side caps).

## Underlying detail: values → environment variables

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
| `MCP_EXTRA_ALLOWED_HOSTS` | own service DNS first (`<deployment.name>-service.<ns>.svc.cluster.local:*`), then `extraAllowedHosts` joined with commas — rendered when transport security is active (ezua endpoint pinned or extras non-empty) |
| `PROMETHEUS_SAVED_QUERIES_PATH` | `persistence.mountPath`/`persistence.savedQueriesFile` ONLY when `persistence.enabled=true` |
| `PROMETHEUS_SAVED_QUERIES_SHARED` | rendered `"1"` ONLY when `persistence.enabled=true` AND `persistence.shared=true` (the per-replica warning in `query_save` results then goes silent) |

Server-side env knobs not wired by the chart (sane defaults built in,
documented for completeness): `PROM_TIMEOUT` (30 s), `PROM_MAX_SERIES` (20),
`PROM_MAX_POINTS` (60 per range series), `PROM_MAX_LABEL_VALUES` (200),
`PROM_BEARER_TOKEN_ENV` (name of an env var holding a bearer token, if your
Prometheus requires auth — the token value itself never appears in config),
`PROMETHEUS_MIN_STEP_SECONDS` (range-query step floor, default **15**: a
caller-supplied step below the floor is clamped up to it with an honest
notice in the result; `0` disables — decision D14, caps `step=1s`-style
point floods), `PROMETHEUS_OVERVIEW_CACHE_TTL` (short-TTL cache for the
`/api/overview` dashboard payload, default **20** s; `0` disables — a
refresh within the TTL is served instantly and marked `cached=true` with
`cache_age_seconds`, and concurrent identical overviews share one
computation), and `PROMETHEUS_SAVED_QUERIES_PATH` (saved-query store: unset
= in-memory for the session, tool results say so; set = durable, written
atomically) plus `PROMETHEUS_SAVED_QUERIES_SHARED` (default "0" — set "1"
to declare the store genuinely shared across replicas, silencing the
per-replica warning in `query_save` results; it is a declaration, not
concurrency control).

## Gateway exposure (ezua / Istio + oauth2-proxy)

When `ezua.enabled=true` the chart renders one VirtualService on
`istio-system/ezaf-gateway`: `/mcp` routes to the MCP server (port 9095,
`timeout: 660s` — range queries over wide windows can run for minutes), and
the root route sends `/` (console + `/api/*`, health endpoints) to the same
service. The vendor-label Kyverno ClusterPolicy ships ungated in this chart
(pre-install hook, idempotent). Auth is the one knob to think about:

- `ezua.authorizationPolicy.enabled: false` (default) — the gateway accepts
  anonymous calls to this host. Accept where the trial posture allows it.
- `true` — renders an Istio `AuthorizationPolicy` (action CUSTOM) in
  `ezua.authorizationPolicy.namespace` (default `istio-system`) that
  delegates the auth decision for THIS host to the PCAI **oauth2-proxy**
  provider; external MCP callers must then present a valid PCAI SSO/bearer
  token (the rotating-token trade is documented above).

## Rebuilding the image (W7 packaging fix — 2026-10)

Site operators rarely rebuild, but if you do, know the failure mode this
chart just fixed: `server.py` imports `mcp_metrics` (the Wave-3
instrumentation), and the loose-scripts packaging (`[tool.setuptools]
py-modules`) **silently omits a declared module whose file is not in the
build context** — a rebuilt image without the explicit COPY crash-looped at
startup (`ModuleNotFoundError`) while probes looked fine on the old pods.
The fix, now in the Dockerfile, is threefold: `mcp_metrics.py` is declared in
pyproject `py-modules` AND copied explicitly (`COPY mcp_metrics.py
./mcp_metrics.py`), and a build-time sanity gate imports every shipped module
so packaging drift fails in the build log instead of in a pod. The image also
carries the fleet's build-time permission neutralizer (`chmod -R a+rX /app`)
— files created by agents land `0600` on the ops box and COPY preserves
modes, so a non-root container would otherwise fail on the first unreadable
module. If your rebuild crash-loops, check both before touching values.

## Deployment targets

Behavior that differs by target, and the paste-ready values for each
(`helm/values-examples/` — sanitized; real per-site values live in
`helm/local/`). This server has no API-key Secret to provision — the auth
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
  SSO-gated edge, per the optional-by-design decision above).
- Sanitized example:
  [helm/values-examples/values.g2.yaml](../helm/values-examples/values.g2.yaml).

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
  read-only surface where the trial posture allows; `true` = PCAI SSO/bearer
  enforced at the gateway).
- Metrics off keeps the render minimal.
- Sanitized example:
  [helm/values-examples/values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml).

## Upgrading

Upgrades are a values edit + re-apply, not a redeploy:

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the `# SITE:`-marked values carry over
   unchanged (maps merge recursively; lists replace, not append).
3. Image tags flow from the chart; `pullPolicy: Always` means a re-apply
   picks up a rebuilt image even at the same tag.
4. The NVLink DaemonSet is part of the chart — an upgrade re-renders it
   (or removes it when `nvlinkAutodetect.enabled=false`).
