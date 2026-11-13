# Deployment — logsearch-mcp

## 1. The deployment contract

Deployment is a values problem. The chart is **packaged and imported into
PCAI once** (AI Essentials / EzAppConfig import — PCAI uploads it to
ChartMuseum and its controller installs it); after that, every site-specific
decision below is a value, edited in the chart's **Helm Values** editor
(or the PCAI API) and re-applied. No template edits, no post-render
patching, no editing rendered manifests.

**What PCAI does for you:** resolves `${DOMAIN_NAME}` in ezua values before
rendering, creates the target namespace, applies the `hpe-ezua` vendor
labels (with the chart's Kyverno policy when `ezua.enabled`), and renders
the Istio VirtualService that exposes the server on the `ezaf-gateway`.

**What you must do out-of-band**, because it cannot be a value: create the
API-key Secret (§2.1) and, for cross-namespace log reads, apply the
per-namespace bootstrap Roles (§2.2).

**Upgrade** = import the new chart version, keep your values, re-apply
(§5). Operators running plain Helm do the equivalent with
`helm upgrade -f <values-file>`; per-cluster values live in `helm/local/`
(gitignored, hardlink-ignored, never packaged).

## 2. Prerequisites

### 2.1 Secrets the operator creates

| Secret | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| `logsearch-mcp-apikey` (chart default; the fleet convention `mcp-fleet-apikeys` works too — one shared Secret covers every fleet server pointed at it) | `api-keys` (comma-separated list) | `templates/deployment.yaml` → `LOGSEARCH_API_KEYS`, via `apiKey.existingSecret{,Key}` | `/mcp` requires an API key (`X-API-Key` or `Authorization: Bearer`) — **`/mcp` must never trust network position**; pod logs routinely contain tokens in stack traces, PII, internal hostnames | Pod sits in **`CreateContainerConfigError`** from the first schedule — the values reference the Secret unconditionally, the chart never creates it | `kubectl -n <ns> create secret generic logsearch-mcp-apikey --from-literal="api-keys=$(openssl rand -hex 32)"` |

Rotation is zero-downtime: `api-keys=new,old` (append → move clients →
drop the old); the server re-reads the env per request, no restart. The
read-only web console and `/health` probes stay key-free.

### 2.2 Patches / cluster objects (the things that are not values)

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| Namespace with `hpe-ezua` labels | PCAI convention: the platform recognizes and manages the release namespace. | PCAI-created namespaces carry the labels; a hand-made namespace without them is invisible to platform tooling. | `kubectl get ns <ns> --show-labels` |
| Read-only `Role` + `RoleBinding` (chart-rendered, `rbac.create: true` default) | The SA needs `pods` get/list + `pods/log` get — the only verbs this server uses. `rbac.clusterWide: true` renders the SAME two rules as ClusterRole/ClusterRoleBinding (lab/trusted clusters). | Without the grant every search answers 403 from the API server; the tool reports it per-namespace. | `kubectl -n <ns> auth can-i get pods/log --as=system:serviceaccount:<ns>:<release>-sa` (or the SA name from the render) |
| Per-namespace bootstrap `Role` + `RoleBinding` (admin-applied, one-time, `helm/local/logsearch-rbac-bootstrap*.yaml` shows the shape) | Selective cross-namespace reads without going cluster-wide. The release identity cannot grant itself access elsewhere — an admin over the TARGET namespaces must apply it; re-run is idempotent. | A namespace allowed by the policy still answers 403 from the API. | Same `auth can-i` check run against the target namespace |
| Istio `VirtualService` on `istio-system/ezaf-gateway` (chart-rendered when `ezua.enabled: true`) | The product path for MCP clients outside the cluster: `/mcp` → 9101, `/` → console. `ezua.virtualService.endpoint` is `required` — an empty endpoint aborts the render before anything is created. | With `ezua.enabled: false` no VirtualService renders — in-cluster Service access only; an unresolved `${DOMAIN_NAME}` literal registers a host that matches nothing (the route silently vanishes). | `curl -I https://<endpoint>/healthz` from outside; `kubectl -n istio-system get virtualservice | grep logsearch` |
| Kyverno ClusterPolicy (chart-rendered, gated on `ezua.enabled`) | Stamps `hpe-ezua/*` vendor labels so EzUA recognizes the service. Gated here so non-PCAI renders stay unchanged (prometheus/searxng ship theirs ungated). | Cosmetic-to-platform: the workload lacks the vendor labels PCAI tooling looks for. | `kubectl get clusterpolicy | grep logsearch` |
| `NetworkPolicy` — the MCP network zone (optional, `networkPolicy.enabled: false` default) | Default-deny ingress admitting only `authorizedClients.namespaces` (+ same-namespace pods when `networkPolicy.sameNamespace`, the edge-gateway pods selected by `networkPolicy.ingressGatewayNamespace`/`ingressGatewayLabels` while `allowEzafGatewayIngress: true` — fleet doctrine keeps the browser path open, and the edge pods are live-verified `app: istio-ingressgateway`). Kubelet probes come from node IPs: `probeCidrs` allows them, `probeExceptCidrs` carves the pod CIDR back out (a `0.0.0.0/0` probeCidrs with empty probeExceptCidrs FAILS the render). Optional egress governance: `networkPolicy.egress.enabled` (default false — admin MCPs reach the k8s API/Prometheus/internet legitimately) + `egress.extraEgress` rules; DNS egress is always rendered when egress mode is on. | Off = baseline. On without your callers listed: allowed namespaces get 200 from `service:port/mcp`, any other namespace times out — and `monitoring` unlisted kills ServiceMonitor scraping **silently**. | From an allowed ns: `curl -m 3 http://<service>:9101/healthz`; from a non-listed ns: expect timeout |
| `PodDisruptionBudget` (optional, `podDisruptionBudget.enabled: false` default) | Voluntary-eviction guard: `minAvailable: 1` keeps the (single-replica default) pod from being evicted during a drain/apply window. With ONE replica and `minAvailable: 1` it blocks ALL voluntary evictions — the intended safety trade; raise `deployment.replicaCount` >= 2 first if you want drainable HA. | Off = baseline; `kubectl drain` evicts the sole replica during maintenance. | `kubectl -n <ns> get pdb` |

### 2.3 External dependencies

| Dependency | Purpose | How to prove reachability from the cluster |
|---|---|---|
| The Kubernetes API server (in-cluster) | Everything — pod discovery and log reads ride the SA's in-cluster credentials. | Any tool call succeeding is the proof; `kubectl -n <ns> logs deploy/logsearch-mcp --tail=50` shows startup. |
| A writable volume for `logsearch.exportRoot` (only when exports are wanted) | The `export_matches` destination the calling agent reads back. Fleet convention: a path shared with workbench (`/data/exports` + workbench `WORKBENCH_SHARED_PATHS=/data/exports`). **The chart ships no volume wiring for this path** — see §7 Known chart gaps. | `kubectl -n <ns> exec deploy/logsearch-mcp -- touch <exportRoot>/.write-test` |

## 3. Values

### 3.1 Required values

| Key | Why it matters | Default behavior if left empty |
|---|---|---|
| `logsearch.allowedNamespaces` | The namespace allowlist — this READ-ONLY server's ONLY access control (comma-separated `fnmatch` globs). | `""` = **DENY ALL namespaces** (default-deny, fleet decision D8): an unconfigured release answers nothing. Set it explicitly; trusted lab clusters may instead set `logsearch.emptyAllowsAll: true` to restore the pre-D8 open default. |
| `logsearch.blockedNamespaces` | Blocklist; ALWAYS wins over the allowlist — punch holes back out of a broad allow-list. | `""`. |
| `ezua.enabled` + `ezua.domainName` + `ezua.virtualService.endpoint` | Gateway exposure through the PCAI Istio gateway. | `ezua.enabled: false` = ClusterIP-only (in-cluster MCP clients only). With ezua on, an empty endpoint aborts the render. |

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

### 3.2 Optional values (all have chart defaults)

Grouped by concern. Full copies: `helm/values.yaml` (annotated) and
`helm/values-examples/` (paste-ready per target).

**Search behavior** — the caps bound what one call can cost:

| Key | Default | Notes |
|---|---|---|
| `logsearch.maxPods` | `50` | Max pods touched by one fan-out search/count — bounds API-server load. |
| `logsearch.maxLinesPerPod` | `1000` | Max tail lines fetched per pod (also the tail `count_matches` uses). |
| `logsearch.maxTotalLines` | `300` | Default cap on merged matches when the call omits `max_total_lines` — enforced DURING the fan-out (the pull stops when the budget is full). An explicit per-call value overrides the env. |
| `logsearch.maxLineChars` | `2000` | Per-line char cap; overlong lines get an explicit `...[truncated N chars]` marker. `0` disables. |
| `logsearch.fetchConcurrency` | `8` | Parallel pod-fetch fan-out width; `1` = sequential. |
| `logsearch.maxRegexChars` | `512` | User regexes longer than this are refused by the compile-time ReDoS screen. |

**Policy and posture:**

| Key | Default | Notes |
|---|---|---|
| `logsearch.emptyAllowsAll` | `false` | D8 escape hatch: `true` restores the pre-D8 open default (empty allowlist = every namespace searchable). Trusted lab clusters only. |
| `logsearch.exportRoot` | `""` | **`export_matches` destination — default OFF**: `""` renders no env at all and the tool refuses (writes nothing; byte-identical default server). Set an ABSOLUTE directory agents can read back — fleet convention: workbench/shared PVC path (e.g. `/data/exports` + workbench `WORKBENCH_SHARED_PATHS=/data/exports`). `dest_name` is validated to a bare file name; writes are bounded by the search caps and land atomically (temp file + rename — a re-export replaces, never appends). The search reuses the EXACT `search_logs` pipeline (namespace policy, ReDoS screen, caps — no bypass), and a failed search writes nothing. Chart provides no volume wiring for this path (§7). |
| `apiKey.existingSecret` / `existingSecretKey` | `logsearch-mcp-apikey` / `api-keys` | **Mandatory wiring, never created by the chart** — see §2.1 for the failure mode and rotation. |
| `webui.enabled` | `true` | The HPE-branded log-search console at `/` + `/api/*` — a human front-end over the SAME seams/policy/caps. `false` = MCP-only surface (`/health`, `/healthz`, `/mcp`). |
| `metrics.enabled` (+ `serviceMonitor` / `interval`) | `false` | `GET /metrics` self-metrics (per-tool request counters, nothing else). Default OFF renders no env and no ServiceMonitor; when on, `/metrics` is key-free like the probes. |

**Workload and exposure:**

| Key | Default | Notes |
|---|---|---|
| `deployment.replicaCount` | `1` | Stateless MCP 2.0 — any replica serves any request. |
| `image.repository` / `tag` / `pullPolicy` | chart-managed | Kept in lockstep with `Chart.yaml` by release tooling — leave at the chart default; pinning a stale tag in a site file is how "old server" pods happen. The image build ends with a permission neutralizer (`RUN chmod -R a+rX /app`) so files that land mode-0600 on the ops box cannot brick the non-root (10001) pod at boot. |
| `imagePullSecrets` | `[]` | Only if the GHCR package is private (public packages pull anonymously). |
| `resources` | `100m`/`256Mi` requests, `512Mi` memory limit | Modest; log tails are streamed, not stored. |
| `securityContext` | non-root uid 10001 | Keep. |
| `deployment.appName` | `logsearch-mcp` | Label/selector + container name on Deployment, Service, VirtualService — not the release name (`deployment.name` is, and names the ServiceAccount). Leave at the default; mismatched selectors break the Service/VS wiring. |
| `service.type` / `targetPort` | `ClusterIP` / `9101` | Cluster-internal; the only external path is the ezaf-gateway VirtualService. Don't move the port without moving the probes' target. |
| `rbac.create` | `true` | Creates the ServiceAccount + the read-only Role/RoleBinding (release namespace only). `false` falls back to the namespace default ServiceAccount — leave `true`. |
| `rbac.clusterWide` | `false` | Opt-in cluster-wide pod/log READS via the same two read-only rules. Lab/trusted clusters only; pair with `blockedNamespaces` ("kube-*") — cluster-wide includes `kube-system`. Switching false→true removes the old namespaced Role/RoleBinding on upgrade. |
| `proxy.http`/`https`/`noProxy` | `{}` (empty dict) | Per-key proxy wiring (fleet convention): each key is wired only when non-empty, and `proxy: {}` (or omitting the block) means fully off. Fleet-consistency block — the only peer is the in-cluster API, covered by the NO_PROXY cluster-local entries; no `caCert` wiring exists because there is no outbound TLS to trust. (The former `hpe_proxies` boolean is removed — a site that relied on `hpe_proxies: true` + the chart's built-in proxy defaults now needs an explicit `proxy` block with its real addresses.) |
| `extraAllowedHosts` | `[]` | EXTRA DNS-rebinding allowlist entries (in-cluster svc-DNS Host values, verbatim or `host:*`), joined into `MCP_EXTRA_ALLOWED_HOSTS`. With `ezua` enabled the endpoint renders as `MCP_HOSTNAME` automatically; when transport security is active the chart AUTO-prepends the release's own service DNS as the first entry — this key lists only EXTRA hosts; the default render still has neither env. |
| `networkPolicy.*` | `enabled: false` | The MCP network zone — see §2.2 for the sub-keys (`sameNamespace`, `authorizedClients.namespaces`, `allowEzafGatewayIngress`, `ingressGatewayNamespace`/`ingressGatewayLabels`, `probeCidrs`/`probeExceptCidrs`, `egress.enabled`/`egress.extraEgress`). Hardened profile: `helm/values-examples/values-hardened-g2.yaml`. |
| `podDisruptionBudget.enabled` / `.minAvailable` | `false` / `1` | See §2.2. |

### 3.3 Underlying detail: values → environment variables

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
| `MCP_HOSTNAME` | `ezua.virtualService.endpoint` — rendered ONLY when `ezua.enabled` and the endpoint are set (DNS-rebinding Host pin) |
| `MCP_EXTRA_ALLOWED_HOSTS` | own service DNS first (`<deployment.name>-service.<ns>.svc.cluster.local:*`), then `extraAllowedHosts` — rendered when transport security is active |
| `LOGSEARCH_WEBUI_ENABLED` | `webui.enabled` |
| `LOGSEARCH_METRICS_ENABLED` | rendered `"true"` only when `metrics.enabled=true` (otherwise absent — no `/metrics` route) |
| `LOGSEARCH_API_KEYS` | `apiKey.existingSecret{,Key}` — always from the operator-created Secret |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (+ lowercase) | `proxy.*` — each key wired only when non-empty (empty = not rendered) |

## 4. Import and install (PCAI steps)

1. **Pre-create the API-key Secret** in the target namespace (§2.1) — the
   pod fails loud without it.
2. **Import the packaged chart once** into PCAI (AI Essentials → import
   the chart package; PCAI uploads it to ChartMuseum and lists it in the
   catalog).
3. **Create the deployment from the catalog entry**; PCAI creates the
   namespace and opens the chart's Helm Values editor.
4. **Paste a full values document** — pick the matching example from
   `helm/values-examples/` (these are full documents: the editor replaces
   the chart's bundled values), adjust every `# SITE:` line, keep
   `${DOMAIN_NAME}` as-is (PCAI resolves it before rendering).
5. **Apply.** PCAI renders and installs; watch the pod come up.
6. **Verify:** `/healthz` answers; the web console's status panel
   (`/api/status`) shows the effective namespace policy and caps — the
   exact values the tools enforce; one MCP handshake + `list_log_sources`
   call per [VERIFICATION.md](VERIFICATION.md).

## 5. Upgrade / rollback / uninstall

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
to deny-all on upgrade unless it sets `logsearch.emptyAllowsAll: true` —
decide that deliberately, don't discover it from users. Wave 1 also moved
the caps keys from (accidentally) under `apiKey:` to under `logsearch:` —
re-applying the updated values makes the rendered env match the documented
numbers.

**Rollback / uninstall (PCAI-way):** roll back by re-importing the previous
chart version and re-applying the same values (PCAI keeps the values
document; the values are the state). Uninstall = delete the deployment from
the PCAI UI — the rendered objects go; the operator-created Secret and any
bootstrap Roles stay (delete them with `kubectl delete` if wanted).

Plain-Helm equivalent (developer/secondary path): render-check, then
install — `helm lint helm/`, `helm template g2 helm/ -f helm/values-examples/values.g2.yaml`
(plus the hardened and hosted-trial examples),
`helm upgrade --install logsearch-mcp helm/ -n <ns> -f helm/local/values.<site>.yaml`
(`helm template ... | kubectl delete -f -` to unwind). This is NOT the PCAI
path; it exists for local development and CI.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod `CreateContainerConfigError` from first schedule | The values reference the API-key Secret (`apiKey.existingSecret`, default `logsearch-mcp-apikey`) and it does not exist in the namespace | Create the Secret per §2.1 — the chart never creates it; the pod recovers on the next reconcile without a values change |
| Every search answers "Error: namespace ... is not allowed ..." naming the policy env vars | Fleet decision D8: empty allowlist = deny all | Set an explicit `logsearch.allowedNamespaces` (or, lab only, `emptyAllowsAll: true`); verify with `/api/status` in the console |
| A namespace the allowlist permits still answers 403 from the API server | RBAC gap, not policy: the SA cannot read that namespace's pods/logs | Either `rbac.clusterWide: true` (chart-native) or apply the per-namespace bootstrap Role/RoleBinding (§2.2); `kubectl auth can-i get pods/log --as=system:serviceaccount:<ns>:<sa>` proves it |
| `Valid .Values.ezua.virtualService.endpoint is required !` at render | `ezua.enabled: true` with a blank endpoint | Set the endpoint (unique per release on the gateway); with `ezua.enabled: false` the endpoint is never read |
| External clients get nothing on the gateway host; in-cluster works | Unresolved `${DOMAIN_NAME}` literal on a plain-Helm site — the VirtualService registered a host that matches nothing | Write the literal domain (or apply through PCAI, which resolves the placeholder) |
| `unsafe regex ... rejected: ...` on search | The ReDoS screen refused a nested-quantifier pattern before it could freeze the server | Rewrite per the error's hint (quantify character classes, not quantified groups; atomic/possessive inner quantifiers) |
| Prometheus stops scraping `/metrics` after enabling the network zone | `monitoring` not in `networkPolicy.authorizedClients.namespaces` — the deny rule matches the scrape namespace and the failure is silent | Keep `monitoring` listed whenever the ServiceMonitor is on; verify from the monitoring ns with `curl -m 3 .../metrics` |
| `/mcp` answers 421 Misdirected Request on the gateway-relay hop | Transport security is active and the relay's Host header (`<name>-service.<ns>.svc.cluster.local:*`) is missing from the allowlist | The chart auto-prepends it when transport security is on; if you render env manually or run plain Helm, add it via `extraAllowedHosts` |
| `export_matches` refuses with setup instructions | `LOGSEARCH_EXPORT_ROOT` unset (default OFF — deliberate) | Set `logsearch.exportRoot` to an absolute directory on a writable, agent-readable volume (§2.3); no restart needed beyond the apply |
| New image, same chart: pod runs "old" code | A stale `image.tag` pinned in a site values file | Remove the site pin — the tag flows from the chart's `appVersion` lockstep |

## 7. Known chart gaps (reported, not fixed here)

- **`logsearch.exportRoot` has no volume wiring.** The chart renders the env
  but ships no pvc.yaml / extraVolumes keys, so the export directory is the
  container's own filesystem unless an operator wires a volume out-of-band.
  The fleet convention (share exports with workbench via the shared PVC)
  cannot be fully expressed in values today.

## 8. Fleet conventions this server participates in

- **Audit-JSONL searchability** — this server writes no audit trail of its
  own (read-only search surface); it is the SEARCH side of the fleet
  convention ("an audit trail nobody can search is unauditable in
  practice"). Audit-writing servers (workbench writes `<root>/.audit.jsonl`
  + `.audit.jsonl.1`) expose their JSONL so it can be searched here: ship
  the file to a pod's stdout (e.g. a log-tailing sidecar), keep that
  namespace inside `logsearch.allowedNamespaces`, and `search_logs` /
  `count_matches` make the trail queryable.
- **`export_matches` destination** — exports are meant to be read back by
  agents, so point `logsearch.exportRoot` at a directory shared with the
  workbench (the workbench/shared PVC plus
  `WORKBENCH_SHARED_PATHS=/data/exports`) rather than at a server-local
  path only this pod can see.

## 9. Deployment targets

Behavior that differs by target, and the paste-ready values for each
(`helm/values-examples/` — sanitized; real per-site values live in
`helm/local/`):

### Proxied corporate site (SITE: your-cluster.example)

- **Domain.** PCAI envsubsts `${DOMAIN_NAME}` in pasted values before
  rendering, so the hosted-trial placeholders work as-pasted; the G2 site
  files deliberately carry the literal domain instead (plain `helm -f`
  readability — either form deploys correctly on PCAI).
- **Lab posture, set EXPLICITLY.** `emptyAllowsAll: true` keeps this lab's
  pre-D8 open default (with an empty `allowedNamespaces`) — that is a
  deliberate escape-hatch choice, never a default; flip it off (and set an
  allowlist) the moment the cluster stops being a lab.
- `rbac.clusterWide: true` — cluster-wide pod/log READS via the chart's
  opt-in knob; `logsearch.blockedNamespaces` stays the agent-side
  counterweight for system namespaces.
- Fleet API key (Secret `mcp-fleet-apikeys`, key `api-keys`), metrics +
  ServiceMonitor on.
- **Network zone optional.** The plain G2 file leaves `networkPolicy.enabled:
  false`; the hardened variant (`values-hardened-g2.yaml`) turns the
  ingress allowlist on for the admin-surface posture (in-cluster callers
  locked to the gateway-relay + agentic-frontend namespaces + `monitoring`;
  browser path stays open).
- Sanitized example: [helm/values-examples/values.g2.yaml](../helm/values-examples/values.g2.yaml).

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
- `proxy: {}` (fully off); metrics off keeps the render minimal, and the
  network zone stays off (the gateway relay's netpol governs in-cluster
  access on a trial).
- Sanitized example: [helm/values-examples/values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml).
