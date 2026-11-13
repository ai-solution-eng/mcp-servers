# DEPLOYMENT — applygate-mcp on PCAI

Deployment is a values problem: import the packaged chart into PCAI once,
create the API-key Secret out of band, then everything below is edited in the
chart's values (PCAI **Helm Values** editor, or the PCAI API) and re-applied.
Operators running plain Helm do the same with `-f <values-file>`; per-cluster
secrets-bearing values live in `helm/local/` (gitignored, hardlink-ignored,
never packaged).

This document is the procedure and the failure modes. The *why* behind each
guardrail is in [FEATURES.md](FEATURES.md).

## The deployment contract

- **The chart is packaged and imported into PCAI once.** PCAI uploads the
  archive to ChartMuseum and its controller installs it; `helm install` is the
  operator/secondary path, not the PCAI one.
- **The operator's only surface is the chart's Helm Values editor.** Every
  site decision is a value — including `namespaces.allowed`, which is the
  difference between a no-op writer and a working one.
- **What PCAI does for you:** `${DOMAIN_NAME}` substitution in the `ezua`
  values, the Istio VirtualService on `istio-system/ezaf-gateway` (when
  `ezua.enabled`), namespace creation.
- **What you must do out of band:** the API-key **Secret**, and the
  **per-namespace writer RBAC** for every namespace outside the release
  namespace (§2.2). Neither can be a value — the first is secret material, the
  second is a privilege grant the release deliberately cannot make for itself.
- **Upgrade = import the new chart version, keep the values document,
  re-apply.** The audit PVC persists across upgrades.

Two properties are non-negotiable in any values document: **the API key is
never inlined** (only a Secret name), and **the namespace allowlist is
explicit** (an empty allowlist is a valid, deliberate no-op writer).

## 2. Prerequisites

Ordered: do these before the first apply.

### 2.1 Secrets the operator creates

| Secret | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| `applygate-mcp-apikey` (default; `apiKey.existingSecret` overrides) | `api-keys` (`apiKey.existingSecretKey`) | `APPLYGATE_API_KEYS` env | `/mcp` requires a key; the chart **never creates or inlines** it | Pod `CreateContainerConfigError` — nothing serves. The template has **no fallback name**: blanking `apiKey.existingSecret` renders an invalid `secretKeyRef` | `kubectl -n <ns> create secret generic applygate-mcp-apikey --from-literal="api-keys=$(openssl rand -hex 32)"` |
| `applygate-mcp-clients` *(optional)* | `clients` | `APPLYGATE_CLIENTS` env | Per-request caller-name registry for audit `caller.name` — **names** keys the middleware already matched; never authenticates | Unset = fingerprint-only attribution (behavior unchanged). Set-but-wrong key name → `CreateContainerConfigError` | `kubectl -n <ns> create secret generic applygate-mcp-clients --from-literal=clients='pipeline-bot:key-1;deploy-bot:key-2'` |
| `ghcr-pull` *(optional)* | `.dockerconfigjson` | `imagePullSecrets` | Only if the GHCR package is private | `ImagePullBackOff` | `kubectl -n <ns> create secret docker-registry ghcr-pull --docker-server=ghcr.io …` |

Keys are a comma-separated list (`api-keys=new,old`) — that **is** the
zero-downtime rotation mechanism: append the new key, move clients over, drop
the old one. The server re-reads the env per request, so no restart is
needed. The fleet-universal `MCP_API_KEYS` env is honored too.

### 2.2 Patches and cluster objects

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| **Per-namespace writer Role + RoleBinding** (one-time, admin) | The chart's Role is namespaced **by construction** — a PCAI release can only create RBAC in its own namespace. Writing into any other namespace needs the same writer Role bound to the release ServiceAccount there | The tool's namespace policy passes and the API answers **403**; the failing result carries `rbac_hint` naming the manifests to extend | `kubectl auth can-i patch deployments -n <target-ns> --as=system:serviceaccount:<release-ns>:applygate-mcp` → `yes` |
| **Audit PVC** `<deployment.name>-audit` | The trail must survive restarts to be worth anything; the container rootfs is read-only | Pod `Pending` if the PVC cannot bind (no default StorageClass). With `persistence.enabled: false` an `emptyDir` is mounted — the trail is lost on restart (labs only) | `kubectl -n <ns> get pvc <deployment.name>-audit` → `Bound` |
| **StorageClass / access mode** | `persistence.accessModes` defaults to `ReadWriteOnce`; `persistence.storageClass` empty = the cluster default | PVC stuck `Pending`; the pod never schedules | `kubectl get storageclass`; check the PVC events |
| **Prometheus Operator CRDs** (only when `metrics.enabled`) | Renders a `ServiceMonitor` (`monitoring.coreos.com/v1`) | Apply fails on an unknown kind; with the CRD present but `monitoring` outside the network zone, scrapes die **silently** | `kubectl get crd servicemonitors.monitoring.coreos.com` |
| **Kyverno CRDs** (only when `kyverno.enabled`) | Optional pre-install `ClusterPolicy` stamping `hpe-ezua/*` vendor labels. Default **false** so the default render stays unchanged | With `kyverno.enabled: true` on a cluster without the CRD, the pre-install hook fails and the release does not install | `kubectl get crd clusterpolicies.kyverno.io` |
| **Istio gateway** `istio-system/ezaf-gateway` (only when `ezua.enabled`) | The VirtualService attaches to it | Renders, but no route exists at the public host | `kubectl -n istio-system get gateway ezaf-gateway` |
| **Unique endpoint host** (site rule) | `ezua.virtualService.endpoint` is the route host | Two releases on one host split traffic between their backends | `kubectl get virtualservice -A`, compare hosts |
| **Single replica** (site rule) | Plan-binding state is per-process | With `replicaCount > 1`, an apply can land on a replica that never saw the plan and refuse — re-plan is the remedy, but the default of 1 is the supported shape | `kubectl -n <ns> get deploy applygate-mcp -o jsonpath='{.spec.replicas}'` |

**Why the RBAC bootstrap is a prerequisite and not a chart feature.** A
release that could grant itself write access to other namespaces would make
the namespace allowlist advisory. The one-time admin manifest keeps the
privilege grant in a human's hands; `namespaces.allowed` must stay a **subset**
of the namespaces that bootstrap covers — a namespace in the policy without a
bootstrap Role means the tool's policy passes but the API answers 403.

Working shape: [`helm/local/rbac-bootstrap.se-g2.yaml`](../helm/local/rbac-bootstrap.se-g2.yaml)
(local-only, never packaged).

### 2.3 External dependencies

- **The Kubernetes API** — in-cluster by definition (the pod's ServiceAccount;
  kubeconfig outside the cluster). Prove the *permissions*, not just
  connectivity: `kubectl auth can-i create configmaps -n <allowed-ns>
  --as=system:serviceaccount:<ns>:applygate-mcp`.
- **The audit volume** — a writable mount at `/data`. With no writable sink
  the writer degrades to stderr-only (best-effort by design: a broken audit
  sink never blocks an operation, but it always screams).
- **Nothing else.** No database, no object store, no model endpoint. If
  `metrics.enabled`, Prometheus must reach the pod on port 9102 from inside
  the cluster.

## 3. Values

### 3.1 Required

| Key | Why it is required | Default behaviour if left empty |
|---|---|---|
| `namespaces.allowed` | The namespace allowlist — THE write guardrail (comma-separated, `fnmatch` globs like `team-*`) | `""` = **default-deny**: the server starts, probes pass, every write is refused loudly. A deployment without it is a no-op writer by design. |
| `apiKey.existingSecret` / `existingSecretKey` | Names the pre-created API-key Secret | Pod fails loud until the Secret exists. Never blank the name — there is no fallback. |
| `ezua.enabled` + `ezua.domainName` + `ezua.virtualService.endpoint` | Gateway exposure through the PCAI Istio gateway | `ezua.enabled: false` (the chart default) = ClusterIP-only, in-cluster MCP clients only. When enabled, an empty endpoint aborts the render: `Valid .Values.ezua.virtualService.endpoint is required !` |

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

### 3.2 Optional (grouped by purpose)

**Write surface** — `kinds.allowed` (default `ConfigMap,Service,Deployment,
StatefulSet,Job,CronJob,Ingress,ServiceAccount,PodDisruptionBudget,
HorizontalPodAutoscaler`). It narrows the write surface and can **never**
widen it: `Secret` and cluster-scoped kinds are refused even if allowlisted,
and kinds unknown to the built-in registry are refused because their scope
cannot be proven. `namespaces.blocked` always wins over `namespaces.allowed`.

**Plan binding** — `planBinding.unplannedApply` (`''` renders nothing; unset =
the server's built-in **deny**). Renders `APPLYGATE_UNPLANNED_APPLY` only when
set: `warn` = the documented D11 migration path (applies + logs loudly),
`allow` = pre-D11 behavior. Unknown values fail closed.
[What D11 enforces](FEATURES.md#3-plan-binding-decision-d11-ratified-2026-09)

**Audit trail** — `audit.file` (`/data/audit.jsonl`), `persistence.enabled` /
`size` / `accessModes` / `storageClass` (`true` / `1Gi` /
`[ReadWriteOnce]` / `""`). The PVC carries
`helm.sh/resource-policy: keep`. Fleet convention: this path is documented
for ops mount/expose — mount the same path/PVC into the logsearch pod to make
the trail searchable.

**Caller attribution (optional, additive)** —
`clients.existingSecret`/`existingSecretKey` (renders `APPLYGATE_CLIENTS`
only when set: `name:key;name:key;...`, names keys the middleware already
matched → audit `caller.name`), `callerPassthrough.trustedCidrs` (renders
`MCP_CALLER_TRUSTED_CIDRS` only when set: CIDRs of trusted direct peers whose
`X-MCP-Caller` claim is recorded → audit `caller.via`). **Empty = fail-closed:
the header is ignored from every peer.** Neither knob unlocks anything.

**Transport security** — `mcpHostname` (`''`) and `extraAllowedHosts`
(`[]`). `mcpHostname` is the public FQDN clients use to reach `/mcp`;
**set it in lockstep with `ezua.virtualService.endpoint`** — unset on a
gateway-fronted deployment means every request answers **421 "Invalid Host
header"**. `extraAllowedHosts` lists EXTRA in-cluster service-DNS Host values;
whenever transport security is active the chart AUTO-prepends the release's
own service DNS (`<deployment.name>-service.<ns>.svc.cluster.local:*`, the
gateway relay's Host header), so sites never list that one. Both unset = dev
mode: the SDK's implicit loopback-only protection applies untouched.

**Observability** — `metrics.enabled` / `metrics.interval` (`false` / `30s`).
Additive and OFF by default; renders `APPLYGATE_METRICS_ENABLED` + the
ServiceMonitor; `/metrics` serves on the same container port
(prometheus-client import-guarded, honest fallback without it). Requires the
prometheus-operator CRDs.

**Console** — `webui.enabled` (`true`): the strictly read-only console at `/`
(+ `/api/*`). It exposes NO apply/delete endpoints; `false` strips `/`,
`/ui`, `/api/*` while `/mcp` and the health endpoints keep serving.

**Runtime** — `deployment.replicaCount` (`1`; stateless MCP 2.0 except the
per-process plan session), `image.repository`/`tag`/`pullPolicy`
(chart-managed; keep them in lockstep with `Chart.yaml`/`pyproject.toml` —
pinning a stale tag in a site file is how "old server" pods happen),
`imagePullSecrets` (`[]`), `resources` (`100m`/`256Mi` requests,
`1`/`512Mi` limits — this is a policy gate, not a compute node),
`securityContext`/`podSecurityContext`/`containerSecurityContext` (non-root
10001, read-only rootfs, drop ALL caps, RuntimeDefault seccomp — keep them;
the Kubernetes client needs a writable `/tmp`, which the chart mounts as an
`emptyDir`), `serviceAccount.create`/`name`, `rbac.create`, `service.type`/
`port`/`targetPort` (`ClusterIP`/`9102` — do not move the port without moving
the probes' target).

**Site plumbing** — `proxy.http`/`https`/`noProxy` (`{}`; each key is wired
only when non-empty, `proxy: {}` = fully off; the only peer is the in-cluster
API, covered by the NO_PROXY cluster-local entries), `kyverno.enabled`
(`false`; cluster-scoped pre-install vendor-label policy), `podDisruptionBudget`
(`enabled: false`, `minAvailable: 1`; with a single replica `minAvailable: 1`
blocks **all** voluntary evictions — a drain waits, which is the intended
safety trade), `networkPolicy.*` (`enabled: false`; see
[Enabling the network zone](#enabling-the-network-zone)).

> `securityContext.runAsNonRoot`/`runAsUser`/`runAsGroup`/`fsGroup` are
> present in values but **not consumed by any template** — the pod security
> actually renders from `podSecurityContext` and `containerSecurityContext`,
> and the image runs `USER 10001`. Editing them is inert (kept for
> values-shape consistency).

### 3.3 Values → environment variables

`helm/values.yaml` does not template env vars (Helm cannot template inside a
values file), so `templates/deployment.yaml` renders them. You normally never
touch these directly, but they are what a `kubectl set env` day-2 action
addresses:

| Env var | Rendered from |
|---|---|
| `APPLYGATE_ALLOWED_NAMESPACES` | `namespaces.allowed` |
| `APPLYGATE_BLOCKED_NAMESPACES` | `namespaces.blocked` |
| `APPLYGATE_ALLOWED_KINDS` | `kinds.allowed` |
| `APPLYGATE_AUDIT_FILE` | `audit.file` |
| `APPLYGATE_WEBUI_ENABLED` | `webui.enabled` |
| `APPLYGATE_UNPLANNED_APPLY` | `planBinding.unplannedApply` (only when set; unset = server default deny) |
| `APPLYGATE_METRICS_ENABLED` | `metrics.enabled` (only when true) |
| `APPLYGATE_API_KEYS` | `apiKey.existingSecret{,Key}` — always from the operator-created Secret |
| `APPLYGATE_CLIENTS` | `clients.existingSecret{,Key}` (only when set) |
| `MCP_CALLER_TRUSTED_CIDRS` | `callerPassthrough.trustedCidrs` (only when set; empty = header ignored everywhere) |
| `MCP_HOSTNAME` | `mcpHostname` (only when set; pins the public FQDN) |
| `MCP_EXTRA_ALLOWED_HOSTS` | own service DNS FIRST (always, when transport security is active), then `extraAllowedHosts` joined with commas; absent when neither is set (dev) |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (+ lowercase) | `proxy.*` — each key wired only when non-empty |

### 3.4 Paste-ready profiles

Paste a file **whole** into the Helm Values editor (it replaces the chart's
bundled `values.yaml`), then adjust every `# SITE:` line:

| File | For |
|---|---|
| [`helm/values-examples/values.g2.yaml`](../helm/values-examples/values.g2.yaml) | The HPE SE-G2 lab cluster (literal G2 endpoints, sanitized) — the working reference |
| [`helm/values-examples/values-hardened-g2.yaml`](../helm/values-examples/values-hardened-g2.yaml) | The same site with the MCP network zone ON |
| [`helm/values-examples/values.hosted-trial.yaml`](../helm/values-examples/values.hosted-trial.yaml) | A customer hosted trial (`${DOMAIN_NAME}` placeholders, neutral namespace allowlist) |

**Before the first apply — pre-deploy the key Secret.** Both G2 examples point
`apiKey.existingSecret` at the fleet convention `mcp-fleet-apikeys` (key
`api-keys`):

```bash
kubectl -n <namespace> create secret generic mcp-fleet-apikeys \
  --from-literal='api-keys=<key1>,<key2>'
```

### 3.5 Migrating from `hpe_proxies`

The `hpe_proxies` boolean flag is removed. Configure `proxy.http`,
`proxy.https` and `proxy.noProxy` directly — a key is active only when
non-empty, and `proxy: {}` (or omitting the block) means fully off. A site
that previously relied on `hpe_proxies: true` + the chart's built-in proxy
defaults now needs an explicit `proxy` block with its real addresses.

## 4. Import and install

1. **Create the prerequisites** — the API-key Secret (§2.1) and, for writes
   outside the release namespace, the per-namespace writer RBAC (§2.2). Do
   this first: the pod cannot start without the Secret.
2. **Import the packaged chart** `applygate-mcp-0.5.0.tgz` into the PCAI
   catalog. Once per chart version.
3. **Create the deployment** from that chart, in the namespace that holds the
   Secret.
4. **Open the chart's Helm Values editor** and paste your values document
   (§3.4). Adjust every `# SITE:` line: `namespaces.allowed`,
   `namespaces.blocked`, the endpoint, `mcpHostname`, the Secret name.
5. **Apply.** PCAI renders and installs: Deployment, ClusterIP Service,
   ServiceAccount + namespaced writer Role/RoleBinding, the audit PVC, the
   optional VirtualService, and (only when enabled) the ServiceMonitor.
6. **Verify** — follow [VERIFICATION.md](VERIFICATION.md). The two checks that
   matter first: `/healthz` reports `"namespaces_enabled": true` for your
   allowed namespaces, and a `plan_apply` of a throwaway ConfigMap comes back
   `ok` (nothing is created by a plan).

## 5. Upgrade, rollback and uninstall

**Upgrade (PCAI way).**

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the previous `# SITE:`-marked values carry
   over unchanged (maps merge recursively; lists replace, not append).
3. Apply. Image tags flow from the chart (`image.tag` is release-managed); a
   new chart version rolls the Deployment with the new tag.
4. The audit PVC persists across upgrades — the trail survives restarts and
   version bumps.

Sanity check after any change: the health endpoint reports
`namespaces_enabled` — `"false"` means probes pass but the allowlist is empty
and every write is refused.

**Rollback.** Re-import the previous chart version and re-apply the same
values. The audit trail is untouched by a rollback (it is a separate PVC), so
a rollback never loses history.

**Uninstall.** Delete the deployment from PCAI. The audit PVC carries
`helm.sh/resource-policy: keep` and survives the uninstall on purpose — the
trail is the product. Delete it explicitly when you truly mean to destroy it.
The API-key and clients Secrets are yours and are not removed. Any
per-namespace bootstrap Roles you applied by hand must be removed by hand.

**Plain-Helm equivalent (operators, not the PCAI path).**

```bash
helm lint helm/
helm template g2 helm/ -f helm/values-examples/values.g2.yaml        # eyeball the render
helm upgrade --install applygate-mcp helm/ -n <ns> \
  -f helm/local/values.<site>.yaml
```

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Every write refused with `DEFAULT-DENY: APPLYGATE_ALLOWED_NAMESPACES is unset or empty` | `namespaces.allowed` left empty (the chart default). Set the allowlist in the Helm Values editor, re-apply. `/healthz` shows `"namespaces_enabled": false` in this state. |
| Refusal: `namespace 'x' is not matched by APPLYGATE_ALLOWED_NAMESPACES` | The namespace is not on the allowlist (or a glob misses it). The refusal names the current list. |
| Refusal mentions `APPLYGATE_BLOCKED_NAMESPACES` | The blocklist always wins — remove the pattern or pick another namespace. |
| Refusal: `kind 'Secret' is hard-refused` / `cluster-scoped` | Working as designed. Manage Secrets out-of-band; cluster-scoped objects are never writable here. |
| Tool policy passes but the API answers **403** | The target namespace has no writer Role for this ServiceAccount. Apply the one-time per-namespace RBAC bootstrap (§2.2); the failing result's `rbac_hint` names the manifests. |
| `409 Conflict` / `FieldManagerConflict` on `plan_apply` or `apply_manifest` | Server-side apply will not take ownership of fields another field manager owns — classically `helm` on a Helm-deployed object. Observed live: patching a live Deployment's `resources.limits.memory` refused with the two conflicting fields named, and `force: true` produced a **verbatim-identical refusal** (a plan can never become a mutation). Fix: change the field through the owner (`helm upgrade` with the chart's values) or deliberately adopt ownership with a force-apply from the owning manager. |
| D11 refusal: `no plan_apply has been recorded …` | Working as designed (default deny). Run `plan_apply` on the exact bytes first and carry `manifest_sha256`; `planBinding.unplannedApply: warn\|allow` is the documented migration path for automation that predates the binding. |
| D11 refusal: sha mismatch / "re-run plan_apply on the exact bytes" | The manifest changed between plan and apply, or the carried sha belongs to a superseded plan (a newer plan for the namespace wins). Re-plan the bytes you intend to apply. |
| Apply refuses "plan not found" on a multi-replica deployment | Plan state is per-process: the apply reached a replica that never saw the plan. Keep `replicaCount: 1` (the default) or re-plan. |
| Client hangs or 504 on `/mcp` | The gateway route is missing (`ezua.enabled=false`) or the timeout is too small — `timeout: 120s` suits plan/apply. |
| HTTP 421 "Invalid Host header" through the gateway | `mcpHostname` is unset or does not match `ezua.virtualService.endpoint`. Set both to the same FQDN. |
| Console 404s at `/` but `/mcp` works | `webui.enabled: false` — re-enable and re-apply. |
| Audit lines vanish after a restart | `persistence.enabled: false` (emptyDir). Enable the audit PVC (default 1Gi). |
| Audit chain reports `"ok": false` at a line number | The trail was modified, truncated or reordered after that line. Treat the deployment as compromised, preserve the file, and investigate with the caller fingerprints in the surrounding entries. |
| `verify_audit_chain` refuses a path argument | By design: the tool verifies ONLY the server's configured `APPLYGATE_AUDIT_FILE`. The path is server configuration. |

## Gateway exposure (ezua / Istio)

When `ezua.enabled=true` the chart renders one VirtualService on
`istio-system/ezaf-gateway`: `/mcp` routes to the MCP server (port 9102,
`ezua.virtualService.timeout`), and when `webui.enabled` the root route sends
`/` and `/api/*` to the same service. The console is strictly read-only, so
the gateway surface gains no mutation path, and `/mcp` itself requires the API
key at the pod — but this is still a **WRITE-path server**: expose it only
where the gateway enforces real auth (SSO/bearer at the ezaf-gateway).

This chart deliberately ships **no AuthorizationPolicy template**. Where your
PCAI build offers the oauth2-proxy extension-provider pattern (see
prometheus-mcp's `ezua.authorizationPolicy`), apply the equivalent policy for
this host at the platform level. PCAI resolves `${DOMAIN_NAME}` in `ezua`
values before rendering on current builds; if your build does not, write the
literal domain — an unresolved placeholder registers a gateway host that
matches nothing (the route silently vanishes).

## Cross-namespace writes (operator bootstrap, one-time)

The release's Role can only grant access inside its own namespace — that is a
PCAI constraint, not a choice. To let the server write into OTHER namespaces,
an admin with rights over those namespaces applies a one-time bootstrap
manifest that creates the same writer Role + RoleBinding per target namespace,
bound to the release's ServiceAccount (see
`helm/local/rbac-bootstrap.se-g2.yaml` for the working shape — local-only,
never packaged).

The server's `namespaces.allowed` must stay a **subset** of the namespaces
that bootstrap covers: a namespace in the policy without a bootstrap Role
means the tool's policy passes but the API answers 403. Revoke by removing
the namespace from both places — the tool-level blocklist always wins.

## Deployment targets

Behaviour that differs by target, with the paste-ready values for each
(`helm/values-examples/` — sanitized; real per-site values live in
`helm/local/`):

### Proxied corporate site (SITE: your-cluster.example)

- **Domain.** PCAI envsubsts `${DOMAIN_NAME}` in pasted values before
  rendering, so the hosted-trial placeholders work as-pasted; the G2 site
  files deliberately carry the literal domain instead (plain `helm -f`
  readability — either form deploys correctly on PCAI).
- **Fleet API key.** The shared fleet Secret `mcp-fleet-apikeys` (key
  `api-keys`), created cluster-side before the deploy — the examples point
  `apiKey.existingSecret` at it.
- **RBAC bootstrap.** Cross-namespace writes (the `project-user-*` and tool
  namespaces on the allowlist) need the one-time
  `helm/local/rbac-bootstrap.se-g2.yaml` applied by an admin per target
  namespace.
- **Transport security.** The G2 file sets `mcpHostname` to the literal FQDN,
  in lockstep with `ezua.virtualService.endpoint` — without the pin every
  gateway-fronted request gets HTTP 421.
- **Plumbing.** Explicit `proxy` block (each key non-empty; the former
  `hpe_proxies` flag is removed), `kyverno.enabled: true` (EZUA labeling
  enforced), metrics + ServiceMonitor shipped **off** (flip
  `metrics.enabled: true`; needs the prometheus-operator CRDs).
- **Network zone optional.** The plain G2 file leaves `networkPolicy.enabled:
  false`; [`values-hardened-g2.yaml`](../helm/values-examples/values-hardened-g2.yaml)
  turns the ingress allowlist on (in-cluster callers locked to the
  gateway-relay + agentic-frontend namespaces; browser path stays open).

### Hosted trial (customer-hosted PCAI)

- **`${DOMAIN_NAME}` placeholders stay as-is** — PCAI resolves them before
  rendering on current builds; on a build that does not, substitute the
  literal domain.
- **The API-key Secret is provisioned out of band** per the customer's key
  process; keep the default-deny write surface (`namespaces.allowed` = only
  the trial namespaces) and expose the host only where the ezaf-gateway
  enforces real auth.
- **Transport security.** The trial file pins
  `mcpHostname: applygate-mcp.${DOMAIN_NAME}` — the placeholder resolves in the
  PCAI values editor exactly like the endpoint; on a plain-Helm site
  substitute the literal domain in BOTH keys (gateway requests 421 otherwise).
- `proxy: {}` (fully off), `kyverno.enabled: false` unless the platform
  enforces vendor labels, metrics off keeps the render minimal, and the
  network zone stays off (the gateway relay's netpol governs in-cluster
  access on a trial).
- Cross-namespace writes need the same per-namespace admin bootstrap as on
  G2 (the release Role is namespace-scoped everywhere).

## Enabling the network zone

The chart ships an optional ingress NetworkPolicy (`networkPolicy.enabled`,
default **false** — the default render is byte-identical to the baseline).
When on, only the allowlisted callers reach the MCP: the authorized client
namespaces (plus same-namespace pods, the node IPs in `probeCidrs` for kubelet
probes, and — only if you flip `allowEzafGatewayIngress` — the gateway pods).
Six steps:

1. `kubectl get ns` to find your callers' namespaces.
2. Append any extra caller namespaces to
   `networkPolicy.authorizedClients.namespaces` (keep `monitoring` —
   Prometheus scrapes `/metrics` on the same port, and an unlisted scrape
   namespace dies **silently**).
3. Put your cluster's pod CIDR into `networkPolicy.probeExceptCidrs` (or real
   node IPs in `probeCidrs`). Never `0.0.0.0/0` with an empty
   `probeExceptCidrs` — the template fails the render.
4. The browser path stays OPEN by fleet doctrine — do NOT set
   `ezua.virtualService.enabled: false`. Closing a console is an explicit
   per-chart, per-site decision, never a default.
5. Apply via the PCAI values editor (`helm upgrade` for operators).
6. Verify — an allowed namespace gets HTTP 200 from `service:port/mcp`, any
   other namespace times out.

## The MCP fleet

This server is one of seven; the fleet map, the shared
auth/namespace/read-verb pattern and the gateway's federated MCP surface are
documented once in [`../README.md`](../README.md). For this server's
operator: applygate is the fleet's only general write path, k8s-mcp is the
read half, and both are registered in the same PCAI_LLM gateway with per-tool
ACLs — a key granted `applygate__plan_apply` but not
`applygate__apply_manifest` can look but not touch.
