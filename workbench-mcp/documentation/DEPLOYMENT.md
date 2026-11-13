# Deployment — workbench-mcp

## 1. The deployment contract

Deployment is a values problem. The chart is **packaged and imported into
PCAI once** (AI Essentials / EzAppConfig import — PCAI uploads it to
ChartMuseum and its controller installs it); after that, every
site-specific decision below is a value, edited in the chart's **Helm
Values** editor (or the PCAI API) and re-applied. No template edits, no
post-render patching, no editing rendered manifests.

**What PCAI does for you:** resolves `${DOMAIN_NAME}` in ezua values before
rendering, creates the target namespace, and renders the Istio
VirtualService on the `ezaf-gateway` (`/mcp` → MCP server, `/` → web UI).

**What you must do out-of-band**, because it cannot be a value:

- Create the API-key Secret — **mandatory**: `run_command` is arbitrary
  process execution, so every route except `/health`/`/healthz` is keyed
  (§2.1). The pod fails loud without it.
- Ensure a usable StorageClass for the workspace PVC — the workspaces ARE
  the product (§2.2).
- Decide the command-execution policy in values: the D18-narrow allowlist
  is the default posture; widening it (or enabling `sandbox_run`) is an
  explicit operator decision (§3.2).

**Upgrade** = import the new chart version, keep the values, re-apply (§5).
Workspaces live on the PVC and survive restarts AND upgrades.

## 2. Prerequisites

### 2.1 Secrets the operator creates

| Secret | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| `workbench-mcp-apikey` (chart default; the fleet convention `mcp-fleet-apikeys` works too) | `api-keys` (comma-separated list) | `templates/deployment.yaml` → `WORKBENCH_API_KEYS`, via `apiKey.existingSecret{,Key}` | Every HTTP route except `/health`/`/healthz` requires an API key — `run_command` is arbitrary process execution and the file tools are read/write; "inside the cluster only" is not a control for this surface | Pod sits in **`CreateContainerConfigError`** from the first schedule — the values reference the Secret unconditionally, the chart never creates it | `kubectl -n <ns> create secret generic workbench-mcp-apikey --from-literal="api-keys=$(openssl rand -hex 32)"` |

Rotation is zero-downtime: `api-keys=new,old` (append → move clients →
drop the old); the env re-reads per request. The console HTML at `/` and
`/ui` is public-but-inert (in-page unlock bar); every `/api/*` data route
and `/mcp` stays keyed.

### 2.2 Patches / cluster objects (the things that are not values)

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| A StorageClass able to bind the workspace PVC (`persistence.*`, default 10Gi at `/data`) — **the load-bearing prerequisite** | Workspaces are PVC-backed so they survive pod restarts; RWX (`ReadWriteMany`, the chart default) lets every replica see the same workspaces. Single-replica clusters without an RWX class can drop to `ReadWriteOnce` with `replicaCount: 1`. `workbench.root` MUST equal `persistence.mountPath` (both default `/data`). | PVC stuck Pending (no class) or stranded replicas (RWO + `replicaCount > 1`); without the mount the server has no root to serve | `kubectl -n <ns> get pvc` (Bound); check the class's provisioner supports the chosen access mode |
| PVC writability for uid 10001 (`securityContext.fsGroup: 10001`, chart default) | The mounted volume must be group-writable by the server user. | Pod starts but every write fails with EACCES — the workspace exists and nothing can be saved. | `kubectl -n <ns> exec deploy/workbench-mcp -- touch /data/.wtest && kubectl -n <ns> exec deploy/workbench-mcp -- rm /data/.wtest` |
| Namespace with `hpe-ezua` labels | PCAI convention for platform-managed vendor services. | Platform tooling cannot recognize the workload. | `kubectl get ns <ns> --show-labels` |
| Kyverno ClusterPolicy (opt-in, `kyverno.enabled: false` default) | Pre-install hook stamping `hpe-ezua/*` vendor labels. Cluster-scoped, so off by default (also keeps clusters without the Kyverno CRD installable). | Cosmetic-to-platform when off (the default). | `kubectl get clusterpolicy | grep workbench-mcp` |
| Istio `VirtualService` on `istio-system/ezaf-gateway` (chart-rendered when `ezua.enabled: true`) | The product path: `/mcp` → MCP server (9103, timeout 660s — `run_command` can legitimately run to its max 600s), `/` + `/ui` + `/api/*` → the console. | With `ezua.enabled: false`: ClusterIP-only (in-cluster MCP clients only); an unresolved `${DOMAIN_NAME}` registers a host that matches nothing. | `curl -I https://<endpoint>/healthz`; `kubectl -n istio-system get virtualservice | grep workbench` |
| Executor-pool objects (opt-in, `executors.enabled: true`): `workbench-exec` Deployment, deny-all NetworkPolicy, Role + RoleBinding granting the workbench SA `get`/`list` on pods + `create` on `pods/exec` in its OWN namespace | The `sandbox_run` executor pool — pair with `workbench.sandboxExec: true` (two gates, both must be on). Trust posture: RBAC cannot select pods by label, so this permits exec into ANY pod in the workbench namespace — which holds only workbench + executor pods by design; blast radius exactly one namespace. | Pool off = `sandbox_run` refuses self-describingly (the default). Role absent (RBAC could not be created): exec 403s with a self-describing error including the `kubectl auth can-i create pods/exec` recipe | `kubectl -n <ns> get deploy workbench-exec`; `kubectl auth can-i create pods/exec --as=system:serviceaccount:<ns>:<sa>` |
| `NetworkPolicy` — the MCP network zone (optional, `networkPolicy.enabled: false` default) | Default-deny ingress admitting only `authorizedClients.namespaces` (+ same-namespace pods when `networkPolicy.sameNamespace`, the edge-gateway pods via `ingressGatewayNamespace`/`ingressGatewayLabels` while `allowEzafGatewayIngress: true` — fleet doctrine keeps the browser path open). Kubelet probes: `probeCidrs` + `probeExceptCidrs`. Optional egress governance: `networkPolicy.egress.enabled` + `egress.extraEgress`. | Off = baseline. On without callers listed: they time out; `monitoring` unlisted (metrics on) kills ServiceMonitor scraping silently. | From an allowed ns: `curl -m 3 http://<service>:9103/healthz`; from a non-listed ns: expect timeout |
| `PodDisruptionBudget` (optional, `podDisruptionBudget.enabled: false` default) | Voluntary-eviction guard (`minAvailable: 1` pins the single-replica default pod through a drain/apply window). | Off = baseline. | `kubectl -n <ns> get pdb` |
| CA ConfigMap `ezaf-root-ca` (only with `caCert.enabled: true`) | Mounted and pointed at via `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`PIP_CERT` for the container AND every `run_command` child — what makes `pip install ...` work against a corporate MITM proxy. Must exist in the release namespace (copy the cluster-wide one if absent). | pip/requests inside workspaces die on certificate verification against the intercepting proxy. | `kubectl -n <ns> get cm ezaf-root-ca`; one `pip download` through a workspace |

### 2.3 External dependencies

| Dependency | Purpose | How to prove reachability from the cluster |
|---|---|---|
| The container runtime / kubelet exec path (in-cluster) | `sandbox_run` dispatch rides the Kubernetes exec subresource | `kubectl auth can-i create pods/exec --as=system:serviceaccount:<ns>:<sa>`; a `sandbox_run` call returning `pod:` + `elapsed_s` |
| PyPI / package hosts (only when `pip` is allow-listed) | `run_command` children installing packages | From a workspace: `run_command` `curl -sS -m 10 -o /dev/null -w '%{http_code}' https://pypi.org/simple/` — requires the proxy + CA wiring on a MITM cluster AND pip added to the allowlist (D18 keeps it out by default) |
| (Conventions) The logsearch mount of this root | Fleet audit-JSONL searchability: mount the workbench root into logsearch; `.audit.jsonl` and `.audit.jsonl.1` are searchable | `search_logs` against the workbench namespace finds audit events |

## 3. Values

### 3.1 Required values

| Key | Why it matters | Default |
|---|---|---|
| `ezua.enabled` + `ezua.domainName` + `ezua.virtualService.endpoint` | Gateway exposure — MCP harnesses run OUTSIDE the cluster, so the gateway route is the product path. The `/mcp` timeout (660s) is generous because `run_command` can legitimately run to its max (600 s). | `ezua.enabled: false` = ClusterIP-only (in-cluster MCP clients only). |

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

### 3.2 Optional values (all have chart defaults)

**Storage and workspaces:**

| Key | Default | Notes |
|---|---|---|
| `persistence.enabled` / `size` / `mountPath` / `accessModes` / `storageClass` | `true` / `10Gi` / `/data` / `ReadWriteMany` | THE point of the workbench: workspaces must survive pod restarts. RWX so every replica sees the same workspaces; single-replica clusters without an RWX class can drop to `ReadWriteOnce` with `replicaCount: 1`. `workbench.root` MUST equal `persistence.mountPath`. |
| `workbench.root` | `/data` | PVC mount holding all workspaces. |

**Command-execution policy (the values that define what agents may run):**

| Key | Default | Notes |
|---|---|---|
| `workbench.execAllowlist` | `ls,cat,head,tail,grep,find,wc,du,df,mkdir,touch,cp,mv,tar,git,diff,sort,uniq` | argv[0] allowlist for `run_command` — anything not listed is refused; no shells by design. **Fleet decision D18: no interpreters (`python3`) and no package managers (`pip`/`pip3`) in the default** — an allow-listed interpreter can compute paths at runtime and sidestep the argv-level workspace confinement, and pip executes python code. Re-add explicitly per site (or via a template's `extra_allowed`) when the threat model accepts the documented residual; the D18-legal python path is `sandbox_run` (`executors.*` + `workbench.sandboxExec`). |
| `workbench.execDenylist` | `curl,wget,sudo,su,nc,ncat,ssh,scp,setsid` | Hard deny — WINS over the allowlist (and over template extras). |
| `workbench.execTimeoutDefault` / `execTimeoutMax` | `60` / `600` s | Per-command bounds (per-call `timeout_s` is clamped into these). |
| `workbench.maxFileBytes` | `8388608` (8 MiB) | `write_file` cap — enforced on EVERY payload AND cumulatively on `append=true` (existing size + incoming payload ≤ cap), so chunked appends can never bypass it. |
| `workbench.maxOutputBytes` | `204800` (200 KiB) | stdout/stderr cap per `run_command`. |
| `workbench.execMaxConcurrency` | `4` | Worker width of the dedicated `run_command` pool (env re-read per call) — a saturated pool returns the structured busy response without executing, audited as `run_command_busy`. |
| `workbench.maxListEntries` | `500` | Entries per `list_files` response. |
| `workbench.templates` | `{}` | Workspace templates for `workspace_create(name, template=<name>)` — **default OFF: an empty object renders no env at all and the tool's `template` parameter is refused**, exactly the pre-templates behavior. Templates may only (a) widen that workspace's exec allowlist with operator-supplied bare binary names (denylist still wins) and (b) pre-run `canned_setup` argv commands inside the new workspace through the exact `run_command` machinery (audited as `workspace_template_setup`). Only the template NAME persists; the widened allowlist is re-derived from current values on every call. See the `helm/values.yaml` comment block for a worked example. |

**Sandbox exec (opt-in):**

| Key | Default | Notes |
|---|---|---|
| `executors.enabled` (+ `appName` / `containerName` / `image` / `replicas` / `autoscaling.*`) | `false` / `workbench-exec` / `python` / workbench image / `2` / off | **Sandbox-exec v1 (default OFF — the default render is byte-identical to the baseline)**: renders the executor Deployment (the SAME image, `python3 -c "import time; time.sleep(2147483647)"` command — no new image to build), its deny-all NetworkPolicy, and the namespaced Role + RoleBinding (§2.2 trust posture). Executors are hardened: no SA token, non-root uid 10001, caps dropped, seccomp RuntimeDefault, read-only rootfs + emptyDir `/tmp`, 100m/128Mi → 1 CPU/512Mi, no tolerations, control-plane `DoesNotExist` affinity, hostname topologySpread, no GPU keys anywhere. `autoscaling.enabled: true` renders an HPA (min 2 / max 4 / CPU 70%). |
| `workbench.sandboxExec` | `false` | The `sandbox_run` tool's server-side switch (renders `WORKBENCH_SANDBOX_EXEC=1` ONLY when true). Off = the tool refuses self-describingly naming `executors.enabled`. Related knobs: `workbench.sandboxLabel` (`app=workbench-exec`), `sandboxContainer` (`python`), `sandboxTimeoutS` (`120`, clamped 1..600), `sandboxMaxOutputChars` (`50000`) — each rendered into env ONLY when it diverges from its default. |

**Workload, exposure, auth, egress:**

| Key | Default | Notes |
|---|---|---|
| `deployment.replicaCount` | `1` | Stateless MCP — any replica serves any request (needs the RWX volume to share state). |
| `image.repository` / `tag` / `pullPolicy` | chart-managed | Kept in lockstep with `Chart.yaml` by release tooling — leave at the chart default; a stale tag in a site file is how an "old MCP server" pod happens. |
| `imagePullSecrets` | `[]` | Only if the GHCR package is private (public packages pull anonymously). |
| `resources` | `250m`/`256Mi` requests, `1`/`512Mi` limits | A scratch pad, not a compute node. |
| `securityContext` | non-root uid 10001, `fsGroup: 10001`, `seccompProfile: RuntimeDefault` | Keep — `fsGroup` is what makes the mounted PVC writable by the server user. |
| `containerSecurityContext` | `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, all caps dropped | Fleet-audit P0 posture — `run_command` children can write only the PVC and `/tmp`. |
| `apiKey.existingSecret` / `existingSecretKey` | `workbench-mcp-apikey` / `api-keys` | **Mandatory wiring, never created by the chart** — see §2.1 for the failure mode and rotation. |
| `webui.enabled` | `true` | The HPE-branded console at `/` — workspace switcher, file tree + viewer/saver, env editor, run-command console, audit tail. Read/write surface calling the SAME core functions (confinement, caps, allowlist, audit all still apply). Auth posture: console HTML (`/`, `/ui`) public-but-inert (in-page unlock bar); every `/api/*` data route stays API-key-gated, and the endpoint sits behind gateway authn. |
| `metrics.enabled` (+ `serviceMonitor` / `interval`) | `false` | `GET /metrics` self-metrics (per-tool request counters, nothing else). Default OFF renders no env and no ServiceMonitor. |
| `workbench.logLevel` | `INFO` | Server logging level. |
| `proxy.http`/`https`/`noProxy` | `{}` (empty dict) | Per-key proxy wiring: each key is wired only when non-empty — a non-empty key puts proxy env on the pod AND passes it through to `run_command` children — this is what makes `pip install ...` work inside a workspace on a cluster with no direct internet egress. `proxy: {}` means fully off; NO_PROXY keeps in-cluster traffic direct. (The former `hpe_proxies` boolean is removed — a site that relied on it + chart built-in proxy defaults now needs an explicit `proxy` block with its real addresses.) |
| `caCert.enabled` / `configMap` / `configMapKey` | `false` / `ezaf-root-ca` / `ezaf-root-ca.crt` | Independent of the proxy wiring: set when workloads must trust a corporate MITM CA. Wired for the container AND every `run_command` child (§2.2). |
| `kyverno.enabled` | `false` | Pre-install ClusterPolicy stamping `hpe-ezua/*` vendor labels. Cluster-scoped, so off by default (also keeps clusters without the Kyverno CRD installable). |
| `service.type` / `port` / `targetPort` | `ClusterIP` / `9103` | Don't move the port without moving the probes' target. |
| `deployment.appName` | `workbench-mcp` | Kubernetes object + pod/container name and label. |
| `networkPolicy.*` / `podDisruptionBudget.*` | `false` / `false` | See §2.2. |

### 3.3 Underlying detail: values → environment variables

`templates/deployment.yaml` renders the env vars (you normally never touch
these directly):

| Env var | Rendered from |
|---|---|
| `WORKBENCH_ROOT` | `workbench.root` (must match `persistence.mountPath`) |
| `WORKBENCH_SHARED_PATHS` | operator-set shared paths (D7 escape hatch; not a chart key — set via `env:` if needed) |
| `WORKBENCH_EXEC_ALLOWLIST` / `WORKBENCH_EXEC_DENYLIST` | `workbench.execAllowlist` / `execDenylist` |
| `WORKBENCH_EXEC_TIMEOUT_DEFAULT` / `WORKBENCH_EXEC_TIMEOUT_MAX` | `workbench.execTimeoutDefault` / `execTimeoutMax` |
| `WORKBENCH_MAX_FILE_BYTES` / `WORKBENCH_MAX_OUTPUT_BYTES` / `WORKBENCH_MAX_LIST_ENTRIES` | `workbench.maxFileBytes` / `maxOutputBytes` / `maxListEntries` |
| `WORKBENCH_EXEC_MAX_CONCURRENCY` | `workbench.execMaxConcurrency` (default `4`) |
| `WORKBENCH_AUDIT_MAX_BYTES` | `workbench.auditMaxBytes` (default `104857600` = 100 MiB) |
| `WORKBENCH_UI_ENABLED` | `webui.enabled` |
| `WORKBENCH_LOG_LEVEL` | `workbench.logLevel` |
| `WORKBENCH_TEMPLATES` | `workbench.templates` as JSON — rendered ONLY when the object is non-empty (default: no env at all, template parameter refused) |
| `WORKBENCH_METRICS_ENABLED` | rendered `"true"` only when `metrics.enabled=true` (otherwise absent — no `/metrics` route) |
| `WORKBENCH_SANDBOX_EXEC` | rendered `"1"` only when `workbench.sandboxExec=true` (otherwise absent — the `sandbox_run` tool refuses) |
| `WORKBENCH_SANDBOX_LABEL` / `WORKBENCH_SANDBOX_CONTAINER` / `WORKBENCH_SANDBOX_TIMEOUT_S` / `WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS` | `workbench.sandboxLabel` / `sandboxContainer` / `sandboxTimeoutS` / `sandboxMaxOutputChars` — each rendered ONLY when it diverges from its server default (default render carries none of them) |
| `HOME` | `/tmp` on a per-pod `emptyDir` — read-only-rootfs scratch so pip/tempfiles keep working (writes reset with the pod; workspaces persist on the PVC). `run_command` children get their own `HOME` = the workspace root (src/workbench_mcp/server.py). |
| `WORKBENCH_API_KEYS` | `apiKey.existingSecret{,Key}` — always from the operator-created Secret |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (+ lowercase) | `proxy.*` — each key wired only when non-empty (empty = not rendered; passed through to `run_command` children) |
| `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `PIP_CERT` | `caCert.*`, only when `caCert.enabled=true` |

Numeric caps tolerate helm's float-ish rendering (`8.388608e+06`) — the
server parses them defensively.

## 4. Import and install (PCAI steps)

1. **Pre-create the API-key Secret** (§2.1) — mandatory; the pod fails loud
   without it.
2. **Check the StorageClass story** (§2.2): an RWX-capable class for
   multi-replica, or plan `ReadWriteOnce` + `replicaCount: 1`.
3. **Import the packaged chart once** into PCAI (AI Essentials /
   EzAppConfig import).
4. **Create the deployment from the catalog entry**; PCAI creates the
   namespace and opens the chart's Helm Values editor.
5. **Paste a full values document** — `helm/values-examples/values.g2.yaml`
   or `values.hosted-trial.yaml` (full documents; the editor replaces the
   chart's bundled values), adjust every `# SITE:` line, keep
   `${DOMAIN_NAME}` as-is.
6. **Apply.** PCAI renders and installs; the PVC binds and the pod comes up.
7. **Verify:** `/healthz` answers; with a key configured, an unkeyed `/api/*`
   call answers 401 and the keyed call answers; create one workspace and
   write + read a file through it; MCP handshake per
   [VERIFICATION.md](VERIFICATION.md).

## 5. Upgrade / rollback / uninstall

Upgrades are a values edit + re-apply, not a redeploy:

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the `# SITE:`-marked values carry over
   unchanged (maps merge recursively; lists replace, not append).
3. Image tags flow from the chart; a new chart version rolls the Deployment
   with the new tag.
4. **Workspaces live on the PVC and survive restarts AND upgrades** — agents
   keep their files across the bump. `pvc.yaml` renders unchanged unless
   you change `persistence.*` (shrinking a PVC is not supported).

**Rollback / uninstall (PCAI-way):** roll back by re-importing the previous
chart version and re-applying the same values (the values document is the
state). Uninstall = delete the deployment from the PCAI UI. The PVC and its
workspaces, and the operator-created Secret, survive until explicitly
deleted — that persistence is the product, so uninstall does not silently
destroy agent work.

Plain-Helm equivalent (developer/secondary path):
`helm upgrade --install workbench-mcp helm/ -n <ns> -f helm/local/values.<site>.yaml`.
NOT the PCAI path; exists for local development and CI.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod `CreateContainerConfigError` from first schedule | The values reference the API-key Secret (`apiKey.existingSecret`, default `workbench-mcp-apikey`) and it does not exist in the namespace | Create the Secret per §2.1 — the chart never creates it; the pod recovers on the next reconcile |
| Browser gets raw 401 JSON at `/` and the console is unreachable | Pre-W7 behavior (key middleware covered the console HTML) | Upgrade to a chart with the W7 fix — `GET /` and `/ui` are now public-but-inert with the in-page unlock bar; `/api/*` stays keyed |
| `run_command` refuses `python3` / `pip` | Fleet decision D18: no interpreters or package managers in the default allowlist (an interpreter can compute paths at runtime and sidestep argv confinement) | Deliberate operator choice: widen `workbench.execAllowlist` (or a template's `extra_allowed`), or use the D18-legal `sandbox_run` path (`executors.enabled` + `workbench.sandboxExec`, both switches) |
| `run_command` returns `{"ok": false, "busy": true, ...}` without executing | The dedicated 4-worker pool is saturated with long runs (each can run to 600s) | Retry shortly (the designed behavior); raise `workbench.execMaxConcurrency` if the workload genuinely needs width |
| Command refused with a path-escape error naming `WORKBENCH_SHARED_PATHS` | D7 argv screening caught a token resolving outside the addressed workspace (another workspace's files, or the audit JSONL) | Use the addressed workspace, or add the specific directory to `WORKBENCH_SHARED_PATHS` — never `WORKBENCH_ROOT` itself |
| Workspace writes fail with EACCES; PVC is Bound | `securityContext.fsGroup` missing/mismatched — the volume is not group-writable by uid 10001 | Keep the chart's `fsGroup: 10001`; verify with the §2.2 exec touch test |
| Extra replicas stuck Pending after scaling | `persistence.accessModes: [ReadWriteMany]` on a StorageClass that only does RWO | Switch to an RWX class (gl4f-filesystem on G2) or return to `replicaCount: 1` |
| `sandbox_run` 403s with the `kubectl auth can-i` recipe | The executor Role/RoleBinding is absent (cluster where the chart could not create RBAC) or `executors.enabled` is off | Enable `executors.enabled` (renders pool + netpol + Role/RoleBinding) and `workbench.sandboxExec` — both gates |
| `/mcp` answers 421 Misdirected Request on the gateway-relay hop | Transport security active and the relay's Host header (`workbench-mcp-service.<ns>.svc.cluster.local:*`) missing | The chart auto-prepends it when transport security is on; on a manual render add it via `extraAllowedHosts` |
| `pip install` inside a workspace fails on a MITM-proxy cluster | Three gates stack: D18 keeps pip out of the allowlist; the proxy env must be wired; the CA must be trusted | Widen the allowlist deliberately, set `proxy.http/https`, enable `caCert.enabled` with the proxy root in the ConfigMap (§2.2) |
| `workspace_create(name, template=...)` refuses the parameter | `workbench.templates` empty/unset — the default render carries no templates env (opt-in by design) | Define templates in values (`workbench.templates`), apply, retry |
| Prometheus stops scraping `/metrics` after enabling the network zone | `monitoring` not in `authorizedClients.namespaces` — silent by deny | Keep `monitoring` listed whenever the ServiceMonitor is on |
