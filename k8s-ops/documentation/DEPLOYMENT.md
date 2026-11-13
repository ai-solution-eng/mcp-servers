# DEPLOYMENT — k8s-mcp on PCAI

Deployment is a values problem: import the packaged chart into PCAI once,
create the required Secret out of band, then everything below is edited in
the chart's values and re-applied. This document is the procedure and the
failure modes; the *why* behind each knob is in
[FEATURES.md](FEATURES.md).

## The deployment contract

- **The chart is packaged and imported into PCAI once** (AI Essentials /
  EzAppConfig import). PCAI uploads the archive to ChartMuseum and its
  controller installs it; you never run `helm install` for a PCAI
  deployment.
- **The operator's only surface is the chart's Helm Values editor** (or the
  PCAI API with the same values document). Every site-specific decision is a
  value. No template edits, no post-render patching, no editing rendered
  manifests — a change that is not a value is a prerequisite, below.
- **What PCAI does for you:** `${DOMAIN_NAME}` substitution inside the
  `ezua` values, the Istio VirtualService on `istio-system/ezaf-gateway`,
  namespace creation, and the `hpe-ezua/*` vendor labels (stamped here by the
  chart's Kyverno pre-install policy, because this chart also runs outside
  PCAI).
- **What you must do out of band, because it cannot be a value:** the
  API-key **Secret**, and the **cluster objects** in §2.2 (a PVC for the audit
  trail, exec RoleBindings, CRDs your site has or has not installed).
- **Upgrade = import the new chart version, keep the values document,
  re-apply.** The values document is the source of truth across versions.

## Two charts, one image

| | `helm/` — k8s-mcp (trusted operators) | `helm-customer/` — k8s-mcp-customer (locked distribution) |
| --- | --- | --- |
| exec, namespace policy, per-user clients | values-configurable; `kubectl set env` also works | **keys do not exist** — templates never read them, pasting is inert; day-2 changes are `kubectl set env` only |
| RBAC | `rbac.scope: cluster\|namespace`, `extraResourceGroups` (wildcards refused at render) | **baked**: read-only ClusterRole via a chart constant; clamp = edit the constant + repackage (a platform action) |
| exec RBAC | chart renders the template role; `exec.autoRbac` binds it | **never minted** — exec stays impossible until an operator creates the Role by hand (NOTES.txt prints every command) |
| exposure | Istio VirtualService via `ezua.*` (PCAI convention) | same `ezua:` block — set the customer's endpoint |
| API key | out-of-band Secret (never in values) | out-of-band Secret |
| day-2 knobs | values re-apply or `kubectl set env` | `kubectl set env` (+ manual exec RBAC) |

**Why the split:** with one chart the customer owns any `lockdown` flag
itself, so values-based guards were advisory. With two charts the locked
posture is structural — there is nothing to flip. A values-controlled
`lockdown` flag existed in chart v0.2.0/0.2.1 and was removed in v0.2.2.

Container hardening (non-root 10001, read-only rootfs, dropped capabilities,
RuntimeDefault seccomp, `/tmp` emptyDir) is fixed in **both** charts and is
not values-overridable. The image build ends with a permission neutralizer
(`RUN chmod -R a+rX /app`): files that land mode-0600 on the ops box are
COPYed mode-preserving and would otherwise brick the non-root pod at boot.

## 2. Prerequisites

Ordered: do these before the first apply. Each row states the purpose, the
failure mode without it, and how to verify.

### 2.1 Secrets the operator creates

| Secret | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| `k8s-mcp-apikey` (default; `apiKey.existingSecret` overrides the name) | `api-key` (`apiKey.existingSecretKey`) | `K8S_MCP_API_KEY` env on the Deployment | Every request must authenticate; the chart **never creates or inlines** the key | Pod `CreateContainerConfigError` (`couldn't find key api-key in Secret …`); nothing serves; a wrong key name fails the same way. Never "runs unauthenticated" — the env is a hard `secretKeyRef` | `kubectl -n <ns> create secret generic k8s-mcp-apikey --from-literal="api-key=$(openssl rand -hex 32)"` |
| `clients.existingSecret` name, e.g. `k8s-mcp-clients` *(optional)* | `clients` | `K8S_MCP_CLIENTS` env | Per-user keys with per-user exec assignments | Unset = only the shared key works (fine for a single-key site). Set-but-wrong key name → pod `CreateContainerConfigError` | `kubectl -n <ns> create secret generic k8s-mcp-clients --from-literal=clients='alice:key1:team-*;bob:key2:debug-*'` |
| `ghcr-pull` *(optional)* | `.dockerconfigjson` | `imagePullSecrets` | Only if the GHCR package is private (public packages pull anonymously) | `ImagePullBackOff` / `ErrImagePull` | `kubectl -n <ns> create secret docker-registry ghcr-pull --docker-server=ghcr.io …` |

The API key is never created by the chart and never inlined in values — an
envsubst apply once silently emptied a live key Secret and opened the
endpoint. The `secretKeyRef` is unconditional, which is what makes the
failure loud instead of quiet.

### 2.2 Patches and cluster objects

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| **Kyverno CRDs** (`kyverno.io/v1`) | `templates/kyverno.yaml` is a **pre-install hook** `ClusterPolicy` that stamps `hpe-ezua/type: vendor-service` + `hpe-ezua/app` on the pod/Deployment/Service — PCAI's vendor-label convention | The pre-install hook fails on a cluster without the CRD → the release does not install at all | `kubectl get crd clusterpolicies.kyverno.io` |
| **Exec RoleBindings** (only when `exec.enabled`) | `pods/exec` `create` is granted **only** by namespaced RoleBindings bound to the chart's exec template ClusterRole. With `exec.autoRbac: true` (default) the server binds them at startup | `exec_in_pod` answers 403 Forbidden; the tool is registered but cannot act | `kubectl auth can-i create pods/exec -n <target-ns> --as=system:serviceaccount:<ns>:<sa>` → `yes` |
| **Audit PVC** (only when the exec trail must outlive the pod) | The container rootfs is read-only; the trail is written to `audit.mountPath` (`/var/lib/k8s-mcp`), which is an `emptyDir` by default | Default: the trail is lost on pod rescheduling (it survives container restarts only). No mount at all → the server degrades to stdout-only | `kubectl -n <ns> exec deploy/<name> -- ls -l /var/lib/k8s-mcp`; after a rollout, count the lines in `audit.jsonl` |
| **Prometheus Operator CRDs** (only when `metrics.enabled`) | Renders a `ServiceMonitor` (`monitoring.coreos.com/v1`) | Apply fails on an unknown kind; with the CRD present but `monitoring` outside the network zone, scrapes die **silently** | `kubectl get crd servicemonitors.monitoring.coreos.com`; then `kubectl -n <ns> get servicemonitor` |
| **Istio gateway** `istio-system/ezaf-gateway` | The VirtualService attaches to it | The release renders but no route exists → 404 at the public host | `kubectl -n istio-system get gateway ezaf-gateway` |
| **Unique endpoint host** (not an object — a site rule) | `ezua.virtualService.endpoint` is the route host **and** `MCP_HOSTNAME` | Two releases on one host split traffic between their backends (observed live) | `kubectl get virtualservice -A -o jsonpath='{range .items[*]}{.spec.hosts[0]}{"\n"}{end}' \| sort \| uniq -d` |

### 2.3 External dependencies

- **The Kubernetes API** — in-cluster by definition; the server uses the
  pod's ServiceAccount token. Prove the *permissions*, not just connectivity:
  `kubectl auth can-i list pods --as=system:serviceaccount:<ns>:<sa>` must be
  `yes`, and `… get secrets` must be **`no`** (that is the design).
- **Nothing else.** No database, no object store, no model endpoint. If you
  enable metrics, Prometheus must be able to reach the pod on the service
  port — verify from inside the cluster, not from your laptop.

## 3. Values

### 3.1 Required

| Key | Why it is required |
| --- | --- |
| `ezua.virtualService.endpoint` | Full public hostname, **unique per release** on the shared gateway. Both the VirtualService and the AuthorizationPolicy templates call Helm's `required` on it, so an empty endpoint aborts the render before anything is created: `Valid .Values.ezua.virtualService.endpoint is required !`. It is also injected as `MCP_HOSTNAME` — the DNS-rebinding pin — so a host mismatch is 421 at the gateway. Required whenever `ezua.enabled: true`; with `ezua.enabled: false` no VirtualService renders and the endpoint is never read. |
| `apiKey.existingSecret` / `existingSecretKey` | Names the pre-created API-key Secret (default name `<deployment.name>-apikey`, default key `api-key`). The Deployment fails loud until it exists. |
| `ezua.domainName` | **Informational only** — no template reads it (kept for chart-values compatibility); safe to leave as `${DOMAIN_NAME}` or delete. |

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

### 3.2 Optional (trusted chart — every knob)

Grouped by what the group is *for*; defaults are the chart's. The interaction
between them is explained in [FEATURES.md](FEATURES.md).

**Naming and scale** — `deployment.*` (`k8s-mcp`, 1 replica; the server is
stateless, so scale freely), `image.repository`/`tag`
(`ghcr.io/ai-solution-eng/k8s-mcp` / `v1.3.0` — keep in lockstep with the
chart's `appVersion`), `imagePullSecrets`, `service.port` (9090: `/mcp` +
`/ui/`), `resources` (100m/128Mi → 500m/**2Gi**; heavy `get_resource`
listing bursts OOMKilled ×4 at the old 512Mi, steady state is ~136Mi — the
burst is what counts), `serviceAccount.*` (create, name = deployment name; it
is the RBAC subject), `podAnnotations`/`nodeSelector`/`tolerations`/`affinity`.

**Cluster read access** — `rbac.create` / `rbac.scope` (`cluster` = read-only
ClusterRole; `namespace` = Role clamped to the release namespace, and
auto-RBAC is off in that mode because the server cannot bind cluster-scoped
templates), `rbac.extraResourceGroups` (CRD apiGroups the tools may read,
`resources: "*"`, get/list/watch only — add one entry per installed operator,
e.g. `serving.kserve.io`, `genai.hpe.com`; **never `["*"]`**, refused at
render because it would re-grant Secret reads;
`networking.istio.io/virtualservices` is always granted).

**Namespace governance** — `namespaces.allowed` / `namespaces.blocked`
(`K8S_MCP_ALLOWED_NAMESPACES` / `K8S_MCP_BLOCKED_NAMESPACES`: comma-separated
fnmatch globs, blacklist always wins, `""` = every namespace readable).
**Posture, not just a knob:** an empty `allowed` makes the policy
BLACKLIST-ONLY (or fully OPEN with both empty), so every namespace not
blocked — including namespaces created later — stays readable. The ALLOWLIST
is the recommended posture; the server prints a startup WARNING naming the
empty-allowlist state and the chart NOTES.txt repeats it. Silence it
deliberately with `K8S_MCP_WARN_EMPTY_ALLOWLIST=false`. RBAC remains the true
boundary.

**Hardened exec** — `exec.enabled` (`false`; off means the tool is not
registered at all), `exec.namespaces` (`""` = follow the general namespace
policy), `exec.requireLabel` (`true`; **the label is the boundary** — with it
`false` the server warns at startup, records `label_gate=off` in every audit
entry and appends the posture to every result, so an open deployment cannot
look hardened; never ship `false` outside a throwaway lab),
`exec.allowedCommands` (extra binaries beyond the read-only allowlist),
`exec.autoRbac` (`true`; cluster scope only — see §2.2).

**Per-user keys** — `clients.value` / `clients.existingSecret` (+
`existingSecretKey`): `name:key[:ns-patterns];…`. `clients.value` CONTAINS
SECRETS; prefer `existingSecret`.

**Audit trail** — `audit.mountPath` (`/var/lib/k8s-mcp`; the file is
`<mountPath>/audit.jsonl`), `audit.persistence.existingClaim` (`""` =
`emptyDir`; set a PVC name when the trail must outlive the pod — recommended
wherever exec is enabled).

**Listing guardrails** (performance only, never security) — `listing.maxItems`
(`K8S_MCP_MAX_LIST_ITEMS`, server default 100), `listing.concurrency`
(`K8S_MCP_LIST_CONCURRENCY`, 8), `listing.maxEvents` (`K8S_MCP_MAX_EVENTS`,
100), `listing.maxNsRewrite` (`K8S_MCP_MAX_NS_REWRITE`, 20). `""` renders no
env, so the default render is unchanged.

**Observability** — `metrics.enabled` / `metrics.interval` (`false` / `30s`;
serves the `/metrics` endpoint and renders the ServiceMonitor). Default off
keeps the default render byte-identical.

**Console** — `console.enabled` (`true`; the HPE ops console at `/ui/`,
`K8S_MCP_CONSOLE_ENABLED=false` underneath).

**Transport / exposure** — `extraAllowedHosts` (extra `Host` values the SDK's
DNS-rebinding protection accepts for **in-cluster callers** addressing the
server by service DNS, e.g. Open WebUI; entries verbatim or `host:*`; the
pinned public FQDN and loopback are always allowed; never bypasses the API-key
gate), `ezua.virtualService.enabled` (`true`; the hardened-admin kill-switch
— `false` un-renders the VirtualService while `ezua.enabled` keeps driving
every other gate; gate DIRECTLY on this key, never through Sprig's `default`,
which treats an explicit `false` as empty), `ezua.authorizationPolicy.*`
(`enabled: false`; flips a real oauth2-proxy `AuthorizationPolicy` on for this
host — tokens **on top of** the API key, never instead of it).

**Network zone** — `networkPolicy.*` (`enabled: false`; default-off ingress
allowlist, see [Enabling the network zone](#enabling-the-network-zone)).

**PodDisruptionBudget** — `podDisruptionBudget.enabled` / `minAvailable`
(`false` / 1). Default off keeps the render byte-identical. Single-replica
caveat: with `replicaCount: 1`, `minAvailable: 1` blocks **all** voluntary
evictions — a node drain waits. That is the intended safety trade; raise
`deployment.replicaCount` first if you want a drainable HA pair.

### 3.3 Paste-ready profiles

Pick the file that matches the target, paste it into the Helm Values editor
**whole** (the editor replaces the chart's bundled `values.yaml` entirely),
then adjust every `# SITE:` line:

| Target | File |
|---|---|
| Proxied corporate PCAI site (literal domain, exec on with the label gate) | [`helm/values-examples/values.g2.yaml`](../helm/values-examples/values.g2.yaml) |
| The same site with the network zone on | [`helm/values-examples/values-hardened-g2.yaml`](../helm/values-examples/values-hardened-g2.yaml) |
| Customer hosted trial (locked chart) | [`helm-customer/values-examples/values.hosted-trial.yaml`](../helm-customer/values-examples/values.hosted-trial.yaml) |

Real per-site values live in `helm/local/` and `helm-customer/local/` —
gitignored, `.helmignore`d, never packaged.

## 4. Import and install

1. **Create the prerequisites** — the API-key Secret (§2.1) and, if you use
   them, the audit PVC and exec plan (§2.2). Do this first: the pod cannot
   start without the Secret.
2. **Import the packaged chart** — `k8s-mcp-1.3.0.tgz` (or
   `k8s-mcp-customer-1.3.0-customer.tgz`) — into the PCAI catalog. Once per
   chart version.
3. **Create the deployment** from that chart, in the namespace you created the
   Secret in.
4. **Open the chart's Helm Values editor** and paste your values document (a
   §3.3 example, or your site file). Adjust the `# SITE:` lines: endpoint
   host, Secret name, exec posture, CRD groups.
5. **Apply.** PCAI renders and installs; the Kyverno pre-install hook stamps
   the vendor labels, the Deployment rolls, and exec RBAC (if enabled)
   provisions at startup.
6. **Verify** — follow [VERIFICATION.md](VERIFICATION.md). The two checks that
   matter first: unauthenticated `https://<endpoint>/mcp` returns **401**, and
   an authenticated `tools/list` returns 19 tools (20 with exec).

## 5. Upgrade, rollback and uninstall

**Upgrade (PCAI way).**

1. Import the new chart version into the catalog.
2. Re-apply the **same values document** (maps merge recursively; lists
   replace, not append).
3. Apply. PCAI re-renders and rolls the Deployment; exec RBAC re-provisions at
   startup.
4. Re-run [VERIFICATION.md](VERIFICATION.md).

**Rollback.** Re-import the previous chart version and re-apply the same
values. Nothing in the release is stateful except the audit PVC, which is
independent of the chart version — a rollback never loses the trail.

**Uninstall.** Delete the deployment from PCAI. The API-key Secret, the
`clients` Secret, the audit PVC and any exec RoleBindings are **yours** and
are not removed; delete them deliberately. Leftover RoleBindings are harmless
but should be cleaned up if exec was enabled.

**Plain-Helm equivalent (operators, not the PCAI path).**

```bash
helm template k8s-mcp helm/ -f helm/local/values.<site>.yaml   # eyeball the render
helm upgrade --install k8s-mcp helm/ -n <ns> -f helm/local/values.<site>.yaml
```

Releasing a new version is a maintainer flow: `./automation.sh <version>`
bumps both charts, builds/pushes the image, packages the charts, prunes old
archives. Manual straggler: `VERSION` in `server.py`.

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| HTTP 401 from the endpoint | API key missing or mismatched in the client header (`Authorization: Bearer` or `X-API-Key`). |
| Startup warning "endpoint is UNAUTHENTICATED" | `K8S_MCP_API_KEY` not set — never deploy that way; create the Secret and restart. |
| Pod `CreateContainerConfigError` (`couldn't find key api-key in Secret …`) | The API-key Secret is missing, in the wrong namespace, or the key name differs — create it (§2.1); the pod recovers on its own once it exists. |
| HTTP 421 "Invalid Host header" through the gateway | `MCP_HOSTNAME` (from `ezua.virtualService.endpoint`) does not match the host the client calls. |
| Render fails: `Valid .Values.ezua.virtualService.endpoint is required !` | `ezua.enabled: true` with an empty endpoint (or an unresolved `${DOMAIN_NAME}`) — set the host, or disable ezua for in-cluster-only. |
| Console at `/ui/` shows 404 | `console.enabled: false` (or `K8S_MCP_CONSOLE_ENABLED=false`), or the `ui/` directory is missing from the image — rebuild. |
| `list_virtual_services` returns 403 Forbidden | The deployed ClusterRole predates the Istio grant — re-apply so the chart's role includes `networking.istio.io/virtualservices`. |
| `RBAC provisioning: cannot read template ClusterRole: Forbidden` | The provisioner ClusterRole predates the `get` grant — upgrade/re-apply the chart. |
| `RBAC provisioning: template ClusterRole missing` | Exec enabled but the exec template role was not rendered — check `rbac.create` and `exec.enabled`, re-apply. |
| `exec policy: namespace 'x' is not in the exec namespace list` | Add it to `exec.namespaces` and re-apply/restart. |
| Exec fails 403 Forbidden | RoleBinding missing — check the startup provisioning summary, or bind manually (Day-2 operations). |
| `pod lacks label k8s-mcp.io/exec="true"` | Label the workload, or set `exec.requireLabel: false` (discouraged — the label is the boundary). |
| `binary 'x' is not in the exec allowlist` | Add it to `exec.allowedCommands` (shells/interpreters stay hard-denied). |
| `not covered by the caller's exec assignment` | The key's per-user pattern list does not include the namespace — an admin widens the assignment. |
| `not covered by the x-exec-namespaces header` / `invalid x-exec-namespaces header` → 400 | The client's own narrowing header excludes the namespace, or the patterns are malformed. |
| `refusing exec into the MCP server's own pod` | Working as intended (credential isolation). |
| Two releases fight over one hostname | Endpoints must be unique per release on the shared gateway — split traffic otherwise lands on both backends. |
| Exec audit trail empty after a pod move | The audit volume is an `emptyDir` — set `audit.persistence.existingClaim` (§2.2). |
| Re-applying wiped my env knobs (legacy path only) | `envsubst` renders `K8S_MCP_*` from the current shell — with the charts this cannot happen (values live in the release). |

## Deployment profiles: internal G2 vs hosted trial

The workflow sentence: pick the matching shipped example file, paste it into
the **Helm Values** editor, adjust the `# SITE:` lines, apply. The
G2-vs-trial choice is really the **two-chart choice** — which trust level the
audience gets.

| Concern | Internal G2 (trusted chart, `helm/`) | Hosted trial (locked chart, `helm-customer/`) |
| --- | --- | --- |
| Shipped example | `helm/values-examples/values.g2.yaml` (+ `values-hardened-g2.yaml` for the network-zone posture) | `helm-customer/values-examples/values.hosted-trial.yaml` |
| Endpoint / `${DOMAIN_NAME}` | The SE-G2 site files carry the **literal domain** for plain `helm -f` readability; PCAI would envsubst a pasted placeholder either way. The endpoint doubles as `MCP_HOSTNAME` and must be unique per release. | **`${DOMAIN_NAME}` placeholders stay verbatim** — PCAI resolves them before rendering on current builds; on a build that does not, substitute the literal domain (an unresolved placeholder registers a gateway host that matches nothing). |
| SSO / AuthorizationPolicy | `ezua.authorizationPolicy.enabled: false` (the accepted lab trade — the built-in API key is the gate). The template is real: flipping it on enforces oauth2-proxy/PCAI tokens **on top of** the API key. | Same key, same default-off posture, same template. |
| Secrets strategy | Out-of-band Secret **`k8s-mcp-2-0-apikey`** (the G2 example keeps its own Secret convention, key `api-key`) — created before the first apply. | Out-of-band Secret per the customer's key process (`apiKey.existingSecret`; `""` = `<deployment.name>-apikey`). |
| RBAC & exec posture | Values-configurable: `rbac.scope: cluster` + `extraResourceGroups: [serving.kserve.io, genai.hpe.com]`; the G2 lab runs exec ON cluster-wide with the per-pod label gate ON (`exec.enabled: true`, `namespaces: "*"`, `requireLabel: true`) — nothing opts in until an operator labels a workload. | **Baked**: read-only ClusterRole via a chart constant; exec RBAC is **never minted** — exec stays impossible until an operator runs the NOTES.txt runbook by hand. |
| Namespace policy | App-layer policy live: the G2 lab uses a blocked-only allowlist (`kube-system,kube-public,k8s-mcp-ops`). | The keys **do not exist** — no app-layer policy; the RBAC clamp is the only boundary. |
| Network zone (`networkPolicy.*`) | Available, default off; the hardened-G2 example turns it on. | **Not in the locked chart** — the zone is a trusted-operator knob. |
| Metrics | On (`metrics.enabled: true` + ServiceMonitor). | On (same key; the locked chart still exposes benign wiring). |

Fleet posture note: unlike applygate/logsearch (which point at the shared
`mcp-fleet-apikeys` Secret), the k8s-mcp G2 example keeps its own
`k8s-mcp-2-0-apikey` Secret — a deliberate site convention.

## Day-2 operations

### Namespace policy (read access)

Set `namespaces.allowed` / `namespaces.blocked` in values (or
`kubectl -n <ns> set env deploy/<name> K8S_MCP_ALLOWED_NAMESPACES=…`). Read
tools filter cluster-wide results to the policy; kubectl-backed tools require
`-n`, reject namespaced queries without it, and rewrite/reject `-A` per the
policy (glob expansion against live namespaces, cap 20; blacklist-only →
rejected, since raw kubectl output cannot be filtered reliably).

The allowlist is the recommended posture; an empty allowlist is a floor, not
a boundary (the startup warning says so — see §3.2). The policy is
application-layer defence in depth only; the authoritative boundary is the
ClusterRole (see [FEATURES §4](FEATURES.md#4-namespace-governance)).

### Exec in certain namespaces (opt-in)

Exec is **off by default** — the tool does not even exist for clients. Turn
on with `exec.enabled: true` + `exec.namespaces` in values. On startup the
server provisions the `pods/exec` RoleBindings itself for every namespace on
the list (globs expanded against live namespaces) and logs the summary:

```
RBAC provisioning: SA mcp-ns/k8s-mcp bound to 'k8s-mcp-pods-exec' —
created: ['team-a']; already present: none; skipped: [('debug-*', 'no live namespaces matched')]; refused: none
```

The provisioner is escalation-proof by construction: it may reference only
this chart's exec template role (`bind` is resourceNames-restricted), cannot
create or modify role content, never creates ClusterRoleBindings, and skips
blocked or nonexistent namespaces. Four gates must agree before an exec
succeeds: **exec list → general namespace policy → RoleBinding (RBAC) → pod
label**.

Manual RBAC fallback (`exec.autoRbac: false`) — bind each namespace yourself;
permissions stay namespaced:

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

(Standalone pre-provisioning also works without starting the server:
`kubectl -n <ns> exec deploy/<name> -- python -m k8s_mcp.server --provision-rbac`.)
Removing exec: set `exec.enabled: false` — the tool disappears on the next
rollout; leftover RoleBindings are harmless but can be deleted.

### Per-user keys with per-user exec assignments

`clients.value` (or better, `clients.existingSecret`) takes
`name:key[:exec-ns-patterns];…`:

```yaml
clients:
  existingSecret: k8s-mcp-clients   # Secret holding the whole K8S_MCP_CLIENTS string
  existingSecretKey: clients
```

Each client connects with their own key — no extra headers needed; the
optional `X-Exec-Namespaces` request header **narrows** (never widens) a
request's exec scope; every exec AUDIT line names the calling client; RBAC
provisioning binds the union of the deployment list and all client
assignments (restart after changing the map).

### API key rotation & audit trail

```bash
kubectl -n <ns> create secret generic <deployment-name>-apikey \
  --from-literal="api-key=<new>" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n <ns> rollout restart deploy/<name>      # then update clients
```

Every exec allow/deny and RBAC provisioning action is audit-logged:
`kubectl -n <ns> logs deploy/<name> | grep AUDIT`. With
`audit.persistence.existingClaim` set, the same records are the hash-chained
JSONL at `<audit.mountPath>/audit.jsonl` — ship it to logsearch like
applygate's trail if you need it searchable.

## Customer chart specifics (`helm-customer/`)

The locked chart's values deliberately contain only benign wiring: image,
naming, service, exposure endpoint, API-key Secret reference, console toggle,
listing caps, metrics. Pasting `exec: {enabled: true}` into the PCAI frontend
is inert — nothing consumes the key. Security changes are platform actions
from the kube command line; the chart's `NOTES.txt` prints the exact commands
(API-key Secret creation, `kubectl set env` for the locked env knobs, and the
manual exec-Role runbook if exec is ever deliberately enabled):

```bash
# a) enable exec on the Deployment
kubectl -n <ns> set env deploy/<name> K8S_MCP_EXEC_ENABLED=true \
  K8S_MCP_EXEC_TEMPLATE_ROLE=<name>-pods-exec K8S_MCP_EXEC_BINDING_NAME=<name>-exec \
  K8S_MCP_EXEC_NAMESPACES="<ns>" K8S_MCP_EXEC_REQUIRE_LABEL=true
# b) create the exec role (this chart ships WITHOUT it)
kubectl create clusterrole <name>-pods-exec --verb=get,create --resource=pods,pods/exec
# c) bind it per target namespace
kubectl -n <target-ns> create rolebinding <name>-exec \
  --serviceaccount=<ns>:<sa> --clusterrole=<name>-pods-exec
```

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

The full hardened profile is
[`helm/values-examples/values-hardened-g2.yaml`](../helm/values-examples/values-hardened-g2.yaml).

## Legacy envsubst path (v0.1.x parity)

`apply-mcp.sh` automates the legacy one-shot flow (envsubst a raw Deployment
manifest + `kubectl apply` + print the bearer token). The raw manifest
(`k8s-mcp-2-0-server.yaml`) is no longer tracked in this repo — the Helm
charts supersede it — so treat this script as historical unless you restore
the manifest. All `K8S_MCP_*` knobs it drives are the same env vars the charts
set from values.

## The MCP fleet

This server is one of seven; the fleet map, the shared
auth/namespace/read-verb pattern and the gateway's federated MCP surface are
documented once in [`../README.md`](../README.md). Short version for this
server's operator: it is the fleet's read half, applygate is the write half,
and both are registered in the same PCAI_LLM gateway with per-tool ACLs.
