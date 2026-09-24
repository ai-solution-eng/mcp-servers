# Deployment — logsearch-mcp

Deployment is a values problem: import the packaged chart into PCAI once,
then everything below is edited in the chart's values (PCAI **Helm Values**
editor, or the PCAI API) and re-applied. Operators running plain Helm do the
same with `-f <values-file>`; per-cluster values live in `helm/local/`
(gitignored, hardlink-ignored, never packaged).

## Required values

| Key | Why it matters | Default behavior if left empty |
|---|---|---|
| `logsearch.allowedNamespaces` | The namespace allowlist — this READ-ONLY server's ONLY access control (comma-separated `fnmatch` globs). | `""` = **DENY ALL namespaces** (default-deny, fleet decision D8, 2026-09): an unconfigured release answers nothing. Pod logs routinely contain sensitive strings (tokens in stack traces, PII, internal hostnames) — set it explicitly. Trusted lab clusters may instead set `logsearch.emptyAllowsAll: true` to restore the pre-D8 open default. |
| `logsearch.blockedNamespaces` | Blocklist; ALWAYS wins over the allowlist — use it to punch holes back out of a broad allow-list. | `""`. |
| `ezua.enabled` + `ezua.domainName` + `ezua.virtualService.endpoint` | Gateway exposure through the PCAI Istio gateway. | `ezua.enabled: false` = ClusterIP-only (in-cluster MCP clients only). |

```yaml
# Required block — adjust the # SITE: lines and apply
logsearch:
  allowedNamespaces: "team-alpha,team-beta,mcp-demo"   # SITE: REQUIRED under default-deny (D8)
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
| `logsearch.maxTotalLines` | `300` | Default cap on merged `search_logs` matches (bounds the MCP response = the agent's context window). Enforced DURING the fan-out — the pull stops when the budget is full (`pods_skipped_budget` in the response). |
| `logsearch.maxLineChars` | `2000` | Per-line char cap on search output; overlong lines get an explicit `...[truncated N chars]` marker. `0` disables. |
| `logsearch.fetchConcurrency` | `8` | Parallel pod-fetch fan-out width (asyncio.gather under a bounded semaphore); `1` = sequential. |
| `logsearch.maxRegexChars` | `512` | User regexes longer than this are refused by the compile-time ReDoS screen (see the server README's "Regex safety"). |
| `logsearch.exportRoot` | `""` | **export_matches destination — default OFF**: `""` renders no env at all and the tool refuses with a self-describing setup message (writes nothing; byte-identical default server). Set it to an ABSOLUTE directory on a writable volume agents can read back — fleet convention: a path on the workbench/shared PVC (e.g. `/data/exports`, plus workbench's `WORKBENCH_SHARED_PATHS=/data/exports`) so any workbench workspace can read exports. `dest_name` is validated to a bare file name (no `/` or `\` separators, no `..`, no leading dot — the write cannot escape the export area); writes are bounded by the same caps as `search_logs` and land atomically (temp file + rename — a reader never sees a half-written export, and a re-export replaces, never appends). The search reuses the EXACT `search_logs` pipeline (namespace policy, ReDoS screen, caps — no bypass), and a failed search writes nothing. |
| `logsearch.emptyAllowsAll` | `false` | D8 escape hatch: `true` restores the pre-D8 open default (empty allowlist = every namespace searchable). Trusted lab clusters only. |
| `metrics.enabled` (+ `serviceMonitor` / `interval`) | `false` | `GET /metrics` self-metrics (per-tool request counters, nothing else). Default OFF renders no env and no ServiceMonitor — the default pod has no `/metrics` route; when on, `/metrics` is key-free like the probes. |
| `apiKey.existingSecret` / `existingSecretKey` | `logsearch-mcp-apikey` / `api-keys` | **Mandatory wiring, never created by the chart**: `/mcp` requires an API key (pod logs routinely contain sensitive strings), so the Secret must exist in the target namespace before `helm install` or the pod sits in `CreateContainerConfigError`. Comma-separated keys (`api-keys=new,old`) are the zero-downtime rotation mechanism (env re-read per request); the fleet-universal `MCP_API_KEYS` env is honored too. |
| `webui.enabled` | `true` | The HPE-branded log-search console at `/` + `/api/*` — a human front-end over the SAME seams/policy/caps. `false` = MCP-only surface (`/health`, `/healthz`, `/mcp`). |
| `deployment.replicaCount` | `1` | Stateless MCP 2.0 — any replica serves any request. |
| `image.repository` / `tag` / `pullPolicy` | chart-managed | Kept in lockstep with `Chart.yaml` by release tooling — leave at the chart default; pinning a stale tag in a site file is how "old server" pods happen. |
| `imagePullSecrets` | `[]` | Only if the GHCR package is private (public packages pull anonymously). |
| `resources` | `100m`/`256Mi` requests, `512Mi` memory limit | Modest; log tails are streamed, not stored. |
| `securityContext` | non-root uid 10001 | Keep. |
| `rbac.create` | `true` | Creates the ServiceAccount + the read-only Role/RoleBinding (release namespace only). `false` falls back to the namespace default ServiceAccount — leave `true`. |
| `rbac.clusterWide` | `false` | Opt-in: renders the SAME two read-only rules (`pods` get/list + `pods/log` get — nothing else, ever) as a ClusterRole + ClusterRoleBinding, so the SA reads pods/logs cluster-wide. Lab/trusted clusters only; pair with a deliberate namespace policy — RBAC bounds what the SA can read, the policy bounds what agents may ask for. Cluster-wide includes `kube-system`; use `logsearch.blockedNamespaces` ("kube-*") to keep system logs out of agents' reach. Switching an existing release from false→true removes the old namespaced Role/RoleBinding on upgrade. |
| `hpe_proxies` + `proxy.http/https/noProxy` | `false` | Fleet-consistency block. The only peer is the in-cluster API, covered by the NO_PROXY cluster-local entries; no `caCert` wiring exists because there is no outbound TLS to trust. |

## Underlying detail: values → environment variables

`templates/deployment.yaml` renders the env vars (you normally never touch
these directly):

| Env var | Rendered from |
|---|---|
| `LOGSEARCH_ALLOWED_NAMESPACES` | `logsearch.allowedNamespaces` |
| `LOGSEARCH_EMPTY_ALLOWS_ALL` | `logsearch.emptyAllowsAll` (the D8 escape hatch) |
| `LOGSEARCH_BLOCKED_NAMESPACES` | `logsearch.blockedNamespaces` |
| `LOGSEARCH_MAX_PODS` | `logsearch.maxPods` |
| `LOGSEARCH_MAX_LINES_PER_POD` | `logsearch.maxLinesPerPod` |
| `LOGSEARCH_MAX_TOTAL_LINES` | `logsearch.maxTotalLines` |
| `LOGSEARCH_MAX_LINE_CHARS` | `logsearch.maxLineChars` |
| `LOGSEARCH_FETCH_CONCURRENCY` | `logsearch.fetchConcurrency` |
| `LOGSEARCH_MAX_REGEX_CHARS` | `logsearch.maxRegexChars` |
| `LOGSEARCH_EXPORT_ROOT` | `logsearch.exportRoot` — rendered ONLY when non-empty (default: no env at all, `export_matches` refuses) |
| `LOGSEARCH_WEBUI_ENABLED` | `webui.enabled` |
| `LOGSEARCH_METRICS_ENABLED` | rendered `"true"` only when `metrics.enabled=true` (otherwise absent — no `/metrics` route) |
| `LOGSEARCH_API_KEYS` | `apiKey.existingSecret{,Key}` — always from the operator-created Secret |
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

## Cross-namespace log reads

Two supported paths, both READ-ONLY (`pods` get/list + `pods/log` get — never
anything more):

1. **Chart-native, cluster-wide** — `rbac.clusterWide: true`. Renders a
   ClusterRole + ClusterRoleBinding with the same two rules; the SA then
   reads pods/logs everywhere. Intended for lab/trusted clusters (the
   namespace policy remains the agent-facing gate). Switching an existing
   release flips Role/RoleBinding → ClusterRole/ClusterRoleBinding on the
   next `helm upgrade` (the old namespaced objects are pruned as no-longer-
   rendered).
2. **Per-namespace bootstrap (targeted)** — for selective grants, an admin
   with rights over the target namespaces applies a one-time Role + RoleBinding
   per namespace bound to the release's ServiceAccount (the release identity
   cannot grant itself access elsewhere — PCAI constraint); see
   `helm/local/logsearch-rbac-bootstrap.se-g2.yaml` for the working shape
   (local-only, never packaged). Re-run is safe (idempotent).

Without one of the two, a namespace allowed by the policy still answers 403
from the API. Grants stay read-only by design; adding kube-system logs is an
explicit sensitivity call (cluster-wide mode includes it by construction —
see `rbac.clusterWide` above for the `blockedNamespaces` counterweight).

## Fleet conventions this server participates in

Two conventions, both documentation-only in this chart (no `audit.*` /
`export.*` key renders anything but `exportRoot`):

- **Audit-JSONL searchability** — this server writes no audit trail of its
  own (read-only search surface); it is the SEARCH side of the fleet
  convention ("an audit trail nobody can search is unauditable in
  practice"). Audit-writing servers (workbench writes `<root>/.audit.jsonl`;
  applygate's hash-chained trail is owned by its chart/agent) expose their
  JSONL so it can be searched here: ship the file to a pod's stdout (e.g. a
  log-tailing sidecar), keep that namespace inside
  `logsearch.allowedNamespaces`, and `search_logs` / `count_matches` make
  the trail queryable.
- **`export_matches` destination** — exports are meant to be read back by
  agents, so point `logsearch.exportRoot` at a directory shared with the
  workbench (the workbench/shared PVC plus
  `WORKBENCH_SHARED_PATHS=/data/exports`) rather than at a server-local
  path only this pod can see.

## Deployment targets

Behavior that differs by target, and the paste-ready values for each
(`helm/values-examples/` — sanitized; real per-site values live in
`helm/local/`):

### Internal G2 (SE-G2 lab cluster, `pcai-se-ai-application.hst.rdlabs.hpecorp.net`)

- **Literal domain.** This PCAI build does not envsubst `${DOMAIN_NAME}` —
  write the literal domain into `ezua.domainName` and
  `ezua.virtualService.endpoint` (the G2 example ships it already).
- **Lab posture, set EXPLICITLY.** `emptyAllowsAll: true` keeps this lab's
  pre-D8 open default (with an empty `allowedNamespaces`) — that is a
  deliberate escape-hatch choice, never a default; flip it off (and set an
  allowlist) the moment the cluster stops being a lab.
- `rbac.clusterWide: true` — cluster-wide pod/log READS via the chart's
  opt-in knob (same two read-only rules); `logsearch.blockedNamespaces` stays
  the agent-side counterweight for system namespaces.
- Fleet API key (Secret `mcp-fleet-apikeys`, key `api-keys`), metrics +
  ServiceMonitor on.
- Sanitized example:
  [helm/values-examples/values.g2.yaml](../helm/values-examples/values.g2.yaml).

### Hosted trial (customer-hosted PCAI)

- **`${DOMAIN_NAME}` placeholders stay as-is** (PCAI resolves them before
  rendering on current builds; substitute the literal domain only on a build
  that does not).
- **Production posture: DEFAULT-DENY.** An explicit
  `logsearch.allowedNamespaces` is REQUIRED; `emptyAllowsAll` stays `false`
  (never restore the pre-D8 open default on a trial); keep
  `rbac.clusterWide: false` — blast radius exactly one namespace, plus the
  one-time read-only bootstrap per namespace the trial needs to search.
- API-key Secret provisioned out of band per the customer's key process
  (`mcp-fleet-apikeys` convention or the customer's own Secret name); pod
  logs are sensitive — the key gate and the namespace policy are the two
  controls that matter.
- `hpe_proxies: false`; metrics off keeps the render minimal.
- Sanitized example:
  [helm/values-examples/values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml).

## Upgrading

Upgrades are a values edit + re-apply, not a redeploy:

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the `# SITE:`-marked values carry over
   unchanged (maps merge recursively; lists replace, not append).
3. Image tags flow from the chart (`image.tag` is release-managed); a new
   chart version rolls the Deployment with the new tag.

**D8 note (charts older than the 2026-09 hardening wave):** the namespace
policy used to be open when `allowedNamespaces` was empty; it is now
default-deny. Deployments with an explicit `allowedNamespaces` list are
unaffected. A deployment relying on the old empty-means-open behavior flips
to deny-all on upgrade unless it sets `logsearch.emptyAllowsAll: true` (the
`LOGSEARCH_EMPTY_ALLOWS_ALL` escape hatch) — decide that deliberately, don't
discover it from users. Wave 1 also moved the caps keys from (accidentally)
under `apiKey:` to under `logsearch:` — previously the three cap env vars
rendered EMPTY and the server ran on its built-in defaults; re-applying the
updated values makes the rendered env match the documented numbers.

Sanity check after any change: `/healthz` answers, and the web console's
status panel (`/api/status`) shows the effective namespace policy and caps —
the exact values the tools enforce.
