# DEPLOYMENT — k8s-mcp on PCAI

A read-only Kubernetes ops MCP server (MCP 2.0 / protocol `2026-07-28`) with API-key auth, optional namespace governance, an opt-in hardened exec tool, and a built-in HPE ops console at `/ui/`. Deployment on PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) is values-driven: import the packaged chart into the PCAI catalog once, create the deployment from it, and set every knob in the chart's **Helm Values** editor (or via the PCAI API). PCAI resolves `${DOMAIN_NAME}` in the `ezua` values before rendering.

## Two charts, one image

| | `helm/` — k8s-mcp (trusted operators) | `helm-customer/` — k8s-mcp-customer (locked distribution) |
| --- | --- | --- |
| exec, namespace policy, per-user clients | values-configurable; `kubectl set env` also works | **keys do not exist** — templates never read them, pasting is inert; day-2 changes are `kubectl set env` only |
| RBAC | `rbac.scope: cluster\|namespace`, `extraResourceGroups` (wildcards refused at render) | **baked**: read-only ClusterRole via a chart constant; clamp = edit the constant + repackage (a platform action) |
| exec RBAC | chart renders the template role; autoRbac binds it | **never minted** — exec stays impossible until the operator creates the Role by hand (NOTES prints every command) |
| exposure | Istio VirtualService via `ezua.*` (PCAI convention) | same `ezua:` block — set the customer's endpoint |
| API key | out-of-band Secret (never in values) | out-of-band Secret |
| day-2 knobs | values re-apply or `kubectl set env` | `kubectl set env` (+ manual exec RBAC; NOTES prints every command) |

Why the split: with one chart, the customer owns any `lockdown` flag itself, so values-based guards were advisory. With two charts the locked posture is structural — there is nothing to flip. (A values-controlled `lockdown` flag existed in chart v0.2.0/0.2.1 and was removed in v0.2.2 in favor of the two-chart split.)

Container hardening (non-root 10001, read-only rootfs, dropped capabilities, RuntimeDefault seccomp, `/tmp` emptyDir) is fixed in **both** charts — not values-overridable. Release tooling (`./automation.sh <version>` / `./bump_version.sh <version>`) bumps **both** charts, their `image.tag`, and packages them.

## Deploying on PCAI (the values way)

1. **Import the packaged chart** (`k8s-mcp-<ver>.tgz`, or `k8s-mcp-customer-<ver>-customer.tgz` for customer deliveries) into the PCAI catalog — once per chart version.
2. **Create the API-key Secret out of band** (the one operator step the chart deliberately does not do — see the security note below).
3. **Create the deployment from the chart** in the target namespace and paste the values document into the **Helm Values** editor. PCAI resolves `${DOMAIN_NAME}` in `ezua` values before rendering.

### Required values

| Key | Why it is required |
| --- | --- |
| `ezua.domainName` | Informational only — no template reads it (kept for chart-values compatibility); safe to leave `${DOMAIN_NAME}` or delete. |
| `ezua.virtualService.endpoint` | Full public hostname, unique per release on the shared gateway (two VirtualServices claiming one host split traffic between their backends — observed live). It also becomes `MCP_HOSTNAME` in the container, pinning the Host header for DNS-rebinding protection. |
| `apiKey.existingSecret` / `existingSecretKey` | Name of the pre-created API-key Secret (default key `api-key`; default name `<deployment.name>-apikey`). The Deployment fails loud (`CreateContainerConfigError`) until the Secret exists. |

**Security invariant:** the API key is NEVER created by the chart and NEVER inlined in values — an envsubst apply once silently emptied a live key Secret and opened the endpoint. Create it out of band (operator, one-time):

```bash
kubectl -n <release-namespace> create secret generic <deployment-name>-apikey \
  --from-literal="api-key=$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Minimal required-values document (trusted chart):

```yaml
apiKey:
  existingSecret: ""          # "" = <deployment.name>-apikey (create it out of band)
ezua:
  enabled: true
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "k8s-mcp.${DOMAIN_NAME}"
    istioGateway: "istio-system/ezaf-gateway"
    timeout: 660s
```

### Optional values (trusted chart — every knob)

| Key | Default | Meaning |
| --- | --- | --- |
| `deployment.*` | `k8s-mcp`, 1 replica | Naming and scale; the server is stateless. |
| `image.repository` / `tag` | `ghcr.io/ai-solution-eng/k8s-mcp` / `v0.3.1` | Keep `tag` in lockstep with the chart's `appVersion`. |
| `imagePullSecrets` | `[]` | Only if the GHCR package stays private. |
| `service.port` | 9090 | MCP (`/mcp`) + console (`/ui/`). |
| `resources` | 100m/128Mi → 500m/512Mi | Small; raise the limit if large `--all-namespaces` YAML dumps are common (they buffer in memory before the 50k truncation). |
| `serviceAccount.*` | create, name = deployment name | The RBAC subject. |
| `console.enabled` | `true` | HPE ops console at `/ui/` (`K8S_MCP_CONSOLE_ENABLED=false` underneath). |
| `rbac.create` / `rbac.scope` | `true` / `cluster` | `cluster` = read-only ClusterRole; `namespace` = Role clamped to the release namespace (auto-RBAC off in that mode). |
| `rbac.extraResourceGroups` | `[]` | CRD apiGroups the tools may read (`resources: "*"`, get/list/watch only). Add one entry per installed operator (e.g. `serving.kserve.io`, `genai.hpe.com`). NEVER `["*"]` — refused at render time (it would re-grant secrets read). `networking.istio.io/virtualservices` is always granted. |
| `namespaces.allowed` / `blocked` | `""` / `""` | `K8S_MCP_ALLOWED_NAMESPACES` / `K8S_MCP_BLOCKED_NAMESPACES` — comma-separated fnmatch globs; blacklist always wins; `""` = all namespaces readable. |
| `exec.enabled` | `false` | Opt-in hardened exec tool (`exec_in_pod`). Off = the tool is not even registered. |
| `exec.namespaces` | `""` | `K8S_MCP_EXEC_NAMESPACES` — the exec allowlist; `""` = follow the general namespace policy. |
| `exec.requireLabel` | `true` | Target pod must carry `k8s-mcp.io/exec: "true"`. |
| `exec.allowedCommands` | `""` | Extra binaries beyond the built-in read-only allowlist (`K8S_MCP_EXEC_ALLOWED_COMMANDS`). |
| `exec.autoRbac` | `true` | Cluster scope only: the server binds its own pods/exec RoleBindings at startup (`K8S_MCP_EXEC_AUTO_RBAC`). |
| `clients.value` / `clients.existingSecret` | `""` | Per-user keys with per-user exec assignments (`K8S_MCP_CLIENTS`, `name:key[:ns-patterns];…`). CONTAINS SECRETS — prefer `existingSecret`. |
| `listing.maxItems` | `""` | `K8S_MCP_MAX_LIST_ITEMS` (server default 500) — cap on `-A`/cluster-wide list results; a truncation marker naming the env appears when the cap bites. `""` = server default (renders no env; the default chart render is unchanged). |
| `listing.concurrency` | `""` | `K8S_MCP_LIST_CONCURRENCY` (server default 8) — bounded-semaphore width for the parallel per-namespace `-A` fan-out. `""` = server default. |
| `metrics.enabled` / `metrics.interval` | `false` / `30s` | Opt-in observability: `K8S_MCP_METRICS_ENABLED=true` serves `GET /metrics` (counters only) and renders the Prometheus Operator ServiceMonitor (scrape port `mcp`, path `/metrics`). Default off — the default chart render is byte-identical to the pre-metrics baseline. |
| `podAnnotations` / `nodeSelector` / `tolerations` / `affinity` | istio sidecar injection off | Scheduling knobs. |
| `ezua.authorizationPolicy.*` | `enabled: false` | Gateway-level auth gate (oauth2-proxy / SSO bearer). Off by default — the rotating-token pain for machine MCP callers is an accepted lab trade; the template is real, so flipping it on enforces tokens for this host **on top of** the API key. |

### ezua / Istio wiring

When `ezua.enabled: true` the chart renders the VirtualService on `ezua.virtualService.istioGateway` (default `istio-system/ezaf-gateway`): `/mcp` gets a long timeout (660s — multi-tool agent turns and log tails run for minutes); `/` (the console at `/ui/`, health pokes) rides the fallback route. The Kyverno pre-install policy stamps the `hpe-ezua/*` vendor labels PCAI expects. Unlike the search MCP charts, **this chart does render an AuthorizationPolicy** — gated by `ezua.authorizationPolicy.enabled` (default off), adding an oauth2-proxy gate in `istio-system` for the host.

## Upgrading

Edit values, re-apply:

1. Change the values in the PCAI **Helm Values** editor (or re-submit via the PCAI API) — new `image.tag`, namespace policy, exec settings.
2. Apply. PCAI re-renders and rolls the Deployment; exec RBAC re-provisions at startup; `kubectl set env` on the Deployment works identically for day-2 knobs (it triggers a rollout).
3. Re-run the [VERIFICATION](VERIFICATION.md) checks.

Releasing a new version is a maintainer flow: `./automation.sh <version>` bumps both charts, builds/pushes the image, packages the charts, prunes old archives. Manual stragglers: `VERSION` in `server.py`.

## Day-2 operations

### Namespace policy (read access)

Set `namespaces.allowed` / `namespaces.blocked` in values (or `kubectl -n <ns> set env deploy/<name> K8S_MCP_ALLOWED_NAMESPACES=…`). Read tools filter cluster-wide results to the policy; kubectl-backed tools require `-n`, reject namespaced queries without it, and rewrite/reject `-A` per the policy (glob expansion against live namespaces, cap 20; blacklist-only → rejected, since raw kubectl output cannot be filtered reliably).

### Exec in certain namespaces (opt-in)

Exec is **off by default** — the tool doesn't even exist for clients. Turn on with `exec.enabled: true` + `exec.namespaces` in values. On startup the server provisions the pods/exec RoleBindings itself for every namespace on the list (globs expanded against live namespaces) and logs the summary:

```
RBAC provisioning: SA mcp-ns/k8s-mcp bound to 'k8s-mcp-pods-exec' —
created: ['team-a']; already present: none; skipped: [('debug-*', 'no live namespaces matched')]; refused: none
```

The provisioner is escalation-proof by construction: it may reference only this chart's exec template role (`bind` is resourceNames-restricted), cannot create or modify role content, never creates ClusterRoleBindings, and skips blocked or nonexistent namespaces. Four gates must agree before an exec succeeds: **exec list → general namespace policy → RoleBinding (RBAC) → pod label** (label gate: `exec.requireLabel`).

Manual RBAC fallback (`exec.autoRbac: false`) — bind each namespace yourself, permissions stay namespaced:

```bash
SERVER_NS=<release-namespace>
for NS in team-a debug-x; do
kubectl apply -n "$NS" -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: <deployment-name>-exec}
subjects:
- {kind: ServiceAccount, name: <deployment-name>, namespace: "${SERVER_NS}"}
roleRef: {kind: ClusterRole, name: <deployment-name>-pods-exec, apiGroup: rbac.authorization.k8s.io}
EOF
done
```

(Standalone pre-provisioning also works without starting the server: `kubectl -n <ns> exec deploy/<name> -- python server.py --provision-rbac`.) Removing exec: set `exec.enabled: false` — the tool disappears on the next rollout; leftover RoleBindings are harmless but can be deleted.

### Per-user keys with per-user exec assignments

`clients.value` (or better, `clients.existingSecret`) takes `name:key[:exec-ns-patterns];…`:

```yaml
clients:
  existingSecret: k8s-mcp-clients   # Secret holding the whole K8S_MCP_CLIENTS string
  existingSecretKey: clients
```

Each client connects with their own key — no extra headers needed; the optional `X-Exec-Namespaces` request header **narrows** (never widens) a request's exec scope; every exec AUDIT line names the calling client; RBAC provisioning binds the union of the deployment list and all client assignments (restart after changing the map).

### API key rotation & audit trail

```bash
kubectl -n <ns> create secret generic <deployment-name>-apikey \
  --from-literal="api-key=<new>" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n <ns> rollout restart deploy/<name>      # then update clients
```

Every exec allow/deny and RBAC provisioning action is audit-logged: `kubectl -n <ns> logs deploy/<name> | grep AUDIT`.

## Customer chart specifics (`helm-customer/`)

The locked chart's values deliberately contain only benign wiring: image, naming, service, exposure endpoint, API-key Secret reference, console toggle. Pasting `exec: {enabled: true}` into the PCAI frontend is inert — nothing consumes the key. Security changes are platform actions from the kube command line; the chart's NOTES.txt prints the exact commands (API-key Secret creation, `kubectl set env` for the locked env knobs, and the manual exec-Role runbook if exec is ever deliberately enabled).

## Legacy envsubst path (v0.1.x parity)

`apply-mcp.sh` automates the legacy one-shot flow (envsubst a raw Deployment manifest + `kubectl apply` + print the bearer token). The raw manifest (`k8s-mcp-2-0-server.yaml`) is no longer tracked in this repo — the Helm charts supersede it — so treat this script as historical unless you restore the manifest. All `K8S_MCP_*` knobs it drives are the same env vars the charts set from values.

## The MCP fleet — the servers agents see alongside k8s-mcp

k8s-mcp is one server in a fleet of MCP servers backing the ops agents (DSH). Two facts worth knowing before you deploy any of the others:

- **Auth is not uniform — clients will flag the others as "may require auth".** k8s-mcp is the only server with built-in client auth (the API-key middleware). The rest authenticate nobody at the pod: in-cluster callers are trusted by network position, and anything exposed through the PCAI gateway leans on the gateway's oauth2-proxy gate (enabled per chart). When an MCP client probes those endpoints it reports "may require auth" — that is the gateway gate, not a missing credential in the server.
- **Most of the fleet is read-only by design.** applygate is the deliberate write exception; workbench is read/write but confined to its own PVC and intended to sit behind gateway authn.

| MCP server | What it provides | Key tools | Typical use-cases |
| --- | --- | --- | --- |
| k8s-mcp (**this repo**) | Read-mostly cluster inspection & debugging (`run_kubectl` accepts read verbs only) | `cluster_health`, `list_pods`, `describe_resource`, `get_events`, `run_kubectl`, `exec_in_pod` (opt-in, allowlisted binaries), `check_rbac`, CRD/VirtualService readers | "Is anything wrong in the cluster?" triage; inspect pods/deployments/services/PVCs; read logs; check RBAC; debug CRs (e.g. KServe InferenceServices) |
| applygate | The governed K8s **write** path (plan → confirm → apply; nothing mutates implicitly) | `plan_apply`, `apply_manifest`, `delete_resource`, `get_resource_status` | Dry-run/validate a manifest before changing anything; apply into allowlisted namespaces; delete allowlisted resources; verify what was applied |
| logsearch | Regex fan-out over pod logs within a namespace | `search_logs`, `count_matches`, `get_pod_logs`, `list_log_sources` | "Where is this error coming from?" — per-pod match counts, merged chronological matches, tail one pod/container (incl. previous crashed container) |
| prometheus | PromQL queries + alert/rule/series inspection | `prom_query`, `prom_query_range`, `prom_alerts`, `prom_rules`, `prom_series`, `prom_label_values` | Current/historical metrics; firing alerts and the exact rule expressions behind them; label/series discovery |
| searxng | Web search + page fetching via self-hosted SearXNG (sidecar) | `search`, `fetch_content` | Multi-engine web search (region/time/category filters); fetch a URL as clean markdown, escalating to browser-grade TLS/headless rendering for JS-only pages |
| workbench | Persistent, PVC-backed workspaces (files + env + governed commands) | `workspace_create/list/delete`, `write_file`, `read_file`, `list_files`, `run_command`, `set_env`/`get_env`, `delete_file` | Artifacts that survive pod restarts; allowlisted argv commands inside a workspace; per-workspace env vars |
| ddgs-mcp / ddgs-lite | DuckDuckGo web search (`searxng` supersedes it) | `search`, `fetch_content` | Same two tools as `searxng`; kept for clients pinned to the old endpoint |
| sqlhandler / sqlhandler-omnilife | SQL analytics over configured source tables (one engine, two data-source bindings) | `list_tables`, `search_tables`, `describe_table`, `profile_table`, `run_sql`, `scan_table` | Discover tables/columns; profile value ranges before writing filters; SELECTs with joins/aggregations; time travel |
| rag-knowledge / rag-memory | Multimodal RAG datasets + agent long-term memory (two stores, identical tool surface) | `search_dataset(s)`, `get_dataset_files`, `unlock_dataset`, `search_memory`, `add_memory`, `describe_media`, `transcribe_audio` | Search indexed documents/logs/media; store and recall curated decisions/preferences; describe media, transcribe audio |
| seaborn | Statistical visualization + dataset profiling | `summarize_dataset`, `create_plot`, `scatter_plot`, `line_plot`, `histogram_plot`, `box_plot`, `correlation_heatmap`, … | Profile columns/stats before plotting; distributions, trends, category comparisons, correlations, regression diagnostics |

Workflow notes: `prometheus` says *what* is degraded, `logsearch` + k8s-mcp say *where* and *why*, and `applygate` is the only sanctioned way to *fix* something — always `plan_apply` first, then `apply_manifest(..., confirm_apply=True)`. applygate enforces a default-deny namespace policy + kind allowlist + confirm gates; this server deliberately cannot write.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| HTTP 401 from the endpoint | API key missing/mismatch in the client header (`Authorization: Bearer` or `X-API-Key`). |
| Startup warning "endpoint is UNAUTHENTICATED" | `K8S_MCP_API_KEY` not set — never deploy that way; create the Secret and restart. |
| Pod `CreateContainerConfigError` (`couldn't find key api-key in Secret …`) | The API-key Secret is missing or the key name differs — create it (see Required values); the pod recovers once the key exists. |
| Console at `/ui/` shows 404 | `console.enabled: false` (or `K8S_MCP_CONSOLE_ENABLED=false`), or the `ui/` directory is missing from the image — rebuild. |
| `list_virtual_services` returns 403 Forbidden | The deployed ClusterRole predates the Istio grant — re-apply so the chart's role includes `networking.istio.io/virtualservices`. |
| `RBAC provisioning: cannot read template ClusterRole: Forbidden` | The provisioner ClusterRole predates the `get` grant — upgrade/re-apply the chart. |
| `RBAC provisioning: template ClusterRole missing` | Exec enabled but the exec template role wasn't rendered — check `rbac.create` and `exec.enabled`, re-apply. |
| `exec policy: namespace 'x' is not in the exec namespace list` | Add it to `exec.namespaces` and re-apply/restart. |
| exec fails 403 Forbidden | RoleBinding missing — check startup logs for the provisioning summary, or bind manually. |
| `pod lacks label k8s-mcp.io/exec="true"` | Label the workload (or set `exec.requireLabel: false` — discouraged). |
| `binary 'x' is not in the exec allowlist` | Add to `exec.allowedCommands` (shells/interpreters stay hard-denied). |
| `not covered by the caller's exec assignment` | The key's per-user pattern list doesn't include the namespace — widen the assignment (admin) or use the right key. |
| `not covered by the x-exec-namespaces header` / `invalid x-exec-namespaces header` → 400 | The client's own narrowing header excludes the namespace, or patterns are malformed. |
| `refusing exec into the MCP server's own pod` | Working as intended (credential isolation). |
| Two releases fight over one hostname | Endpoints must be unique per release on the shared gateway — split traffic otherwise lands on both backends. |
| Re-applying wiped my env knobs (legacy path only) | `envsubst` renders `K8S_MCP_*` from the current shell — with the charts this cannot happen (values live in the release); legacy users should re-export or use `kubectl set env`. |
