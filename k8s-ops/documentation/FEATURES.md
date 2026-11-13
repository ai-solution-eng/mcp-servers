# FEATURES — k8s-mcp: capabilities and the design decisions behind them

Every capability this server has, and *why* it is built the way it is. The
README is the spine; this file is the depth. Where a decision was forced by a
real incident, the incident is stated — the guard is only maintainable if the
next engineer knows what it prevents.

## 1. The tool surface

19 tools are always registered (20 with exec enabled); names and parameters
are from `src/k8s_mcp/server.py`.

| Tool | Purpose |
| --- | --- |
| `triage` | One-call namespace health summary: attention list + pods + workloads + Warning events + PVCs (§3). |
| `cluster_health` | Node status, component health, resource usage — the "first tool" triage call. |
| `list_pods` / `list_workloads` / `list_services` / `list_pvcs` / `list_namespaces` | Cluster-wide or per-namespace listings with health/restarts/age. |
| `get_pod_logs` | Logs from a pod's container (`container`, `tail`, `previous` for crashed containers). **`namespace` is required** — there is no implicit "default". |
| `get_events` | Cluster/namespace events, filterable by resource and type (capped by `K8S_MCP_MAX_EVENTS`, default 100). |
| `get_resource` / `describe_resource` | Any built-in or custom resource, by type/name/namespace; describe adds events and conditions. Un-named `get_resource` listings of in-process-resolvable kinds render compactly (one `namespace/name (Age: …)` line per item, capped by `K8S_MCP_MAX_LIST_ITEMS`, default 100); named gets and `jsonpath=`/`custom-columns=`/`wide`/`name` output keep kubectl's exact bytes. |
| `list_api_resources` / `list_crds` / `get_custom_resource` | API/CRD discovery and dynamic-client reads of custom resources (e.g. InferenceServices). |
| `get_configmap` | ConfigMap data keys and values. |
| `list_secrets` | Secret names and types only — values are unreadable by design (no RBAC verbs). |
| `list_virtual_services` | Istio VirtualServices — summaries or the full definition. |
| `check_rbac` | Can the service account perform an action? (Surfaces "no" answers honestly.) |
| `run_kubectl` | Read-verb kubectl escape hatch: `get, describe, logs, top, explain, api-resources, api-versions, cluster-info, version, auth, events` only. |
| `exec_in_pod` *(opt-in)* | ONE read-only debugging command in a container — registered only when exec is enabled, hardened per §5. |

Two tool-level contracts are worth stating because agents depend on them:

- **`get_pod_logs` requires `namespace`.** The old silent `default` fallback
  was removed: reading the wrong pod is worse than a refusal.
- **`check_rbac` surfaces "no" answers.** `kubectl auth can-i` exits 1 for
  "no"; masking that as an error made a permission boundary look like a bug.

### Istio VirtualService visibility

`list_virtual_services` is fully read-only and has three shapes: with
`namespace` it returns one summary line-group per VirtualService (hosts,
gateways, http/tls/tcp route entries with weighted destinations, redirects,
delegates, fault injection, mirrors, age); with `name` + `namespace` it
returns the full definition as JSON; with no namespace it lists cluster-wide
with denied namespaces filtered by the policy.

Three implementation decisions worth knowing:

- It resolves the CRD across Istio API versions (`v1` → `v1beta1` →
  `v1alpha3`) through the dynamic client, falling back to kubectl on
  discovery/schema errors — a name without a namespace falls back with
  `--field-selector metadata.name=…`, never `name + -A`.
- The read-only role needed an explicit
  `networking.istio.io → virtualservices` grant (`get, list, watch`): the
  deployed role did not cover Istio, so the generic kubectl path returned 403
  before this tool existed. Minimal by design — gateways and destinationrules
  are NOT granted; extend the rule consciously if ever needed.
- Singular cluster-scoped forms (`clusterrole`, …) are recognized by
  `_CLUSTER_SCOPED_RESOURCES`, so `get clusterrole …` does not demand `-n`
  under an active namespace policy (it is cluster-scoped; RBAC still enforces
  the 403).

## 2. Read-only by construction

The audit that shipped with v1 closed the following attack surface. Do not
regress them — each row is a real vulnerability that existed in the pre-audit
build.

| # | Vulnerability (pre-audit) | Fix |
| --- | --- | --- |
| 1 | **Shell injection** — `subprocess.run(f"kubectl {command}", shell=True)`; the write-verb *denylist* checked only the first word, so `get pods && kubectl delete ns foo`, `;`, backticks, `$()`, `>`, newlines all executed | kubectl is invoked as an **argv list** via `asyncio.create_subprocess_exec` — never a shell. Metacharacters arrive as inert tokens (verified: `get pods; touch /tmp/pwned` never executes) |
| 2 | **`cluster-admin` ClusterRoleBinding** on a "read-only" server | Custom read-only `ClusterRole` enumerating exactly what the tools need. Secrets and RBAC objects deliberately excluded — `list_secrets` / `get secret` return **403 from the API server**, not from tool formatting |
| 3 | **Secret exfiltration by design** — `list_secrets` hid values client-side while the escape hatch allowed `get secret -o yaml` | Closed at the RBAC layer (no Secret verbs at all) |
| 4 | **Denylist gaps** — `run`, `exec`, `attach`, `cp`, `port-forward`, `proxy`, `debug`, `set`, `autoscale`, `certificate`, `plugin` all permitted | Deny-by-default **read-verb allowlist**: `get, describe, logs, top, explain, api-resources, api-versions, cluster-info, version, auth, events` |
| 5 | **Credential-redirection flags** — `--server`, `--token`, `--kubeconfig`, `--insecure-skip-tls-verify`, … could steal the SA token | Rejected outright before execution, together with the impersonation flags `--as`, `--as-group`, `--as-uid` in every `=`-joined form (defence in depth: the shipped ClusterRole grants no `impersonate`, so the attempt dies at parse time instead of at the API server) |
| 6 | **Unvalidated tool parameters** flowed into command strings | Namespace = DNS-label regex; names/types/output strictly validated (`yaml/json/wide/name/jsonpath=/custom-columns=`) |
| 7 | **Predictable `/tmp/kubeconfig`** written by every call (also broke local dev) | `tempfile.mkstemp` mode `0600`, created only when actually in-cluster |
| 8 | **Container ran as root, writable rootfs, unpinned kubectl** | Non-root uid/gid 10001, read-only root filesystem, all capabilities dropped, RuntimeDefault seccomp, `/tmp` as emptyDir; kubectl **pinned (v1.37.0) + sha256-verified** at build; `TARGETARCH`-aware downloads |
| 9 | **Unauthenticated HTTP endpoint** | API-key auth — see §3 below and §8 |

The invariants that follow from the table, stated for the operator:

1. **No shell.** Metacharacters are inert tokens.
2. **Read-verb allowlist.** A write verb only has to be forgotten once; that
   is why the direction is allow, not deny.
3. **Connection and impersonation flags rejected** before execution.
4. **Parameter validation** before anything reaches kubectl or the API.
5. **Least-privilege RBAC** — a custom read-only role, never `cluster-admin`;
   Secrets and RBAC excluded; `networking.istio.io/virtualservices` granted for
   `list_virtual_services`; extend CRD apiGroups per operator, never `["*"]`
   (refused at render time — it would re-grant Secret reads).
6. **Pod hardening** — non-root 10001, read-only rootfs, all capabilities
   dropped, RuntimeDefault seccomp, `/tmp` emptyDir. Fixed in both charts,
   not values-overridable: the audited baseline is not a site decision.
7. **Pinned toolchain** — kubectl pinned and sha256-verified at build.
8. **API-key auth.** Every request must present the key
   (`Authorization: Bearer <key>` or `X-API-Key: <key>`); constant-time
   comparison, 401 before the MCP app is reached. The key lives in an
   out-of-band Secret — the chart never creates or inlines it (an envsubst
   apply once emptied a live Secret) and the Deployment fails loud until it
   exists.
9. **Optional mesh identity.** The chart can render an Istio
   `AuthorizationPolicy` (oauth2-proxy) for the host. A shared API key
   authenticates the *key*, not a user; layer JWT when you need identity.
   Off by default: the rotating-token pain for machine MCP callers is an
   accepted lab trade, and the template is real when you flip it on.

### Authentication and multi-user support

- **API-key middleware** (pure ASGI, zero extra dependencies). Auth is
  disabled only when no key is configured (local dev), with a loud startup
  warning.
- **The key is out-of-band by design**: managed exclusively via
  `kubectl create secret …` — deliberately *removed* from the envsubst
  manifest after an unset `API_KEY` silently emptied the live Secret. The
  Deployment now **fails loud** (`CreateContainerConfigError`) until the
  Secret exists.
- **Per-user keys with per-user exec assignments** (`K8S_MCP_CLIENTS`):
  `name:key[:exec-ns-patterns];…` — capabilities travel with the credential;
  users cannot widen their own scope. The shared key keeps working alongside
  (deployment-wide ceiling).
- **`X-Exec-Namespaces` request header**: narrows a request's exec scope
  (intersected with the key's assignment); it can never widen. Malformed
  patterns → 400.
- Every exec attempt is audit-logged with the calling client's name.

### Host-header (DNS-rebinding) protection

`MCP_HOSTNAME` is set from `ezua.virtualService.endpoint` and pins the Host
header. Two lessons are baked into that: the public hostname must be unique
per deployment (two VirtualServices claiming one host split traffic between
their backends — observed live between two namespaces), and the protection
must be scoped to the *real* hostname. When it is not, the SDK's implicit
loopback-only default silently 421s every gateway-fronted request that
carries a valid key — and because requests that fail auth never reach it,
only real users find out. That was the v0.1.3 fix.

## 3. Fleet triage: one call, one governed path

`triage(namespace, app="")` answers "what needs attention in this
namespace?" in one call. It is a **composite read**: it assembles its summary
from this server's own governed read paths — the namespace policy is enforced
up front exactly as `list_pods` enforces it (a denied namespace gets the
identical refusal, before any underlying call), every Kubernetes read is one
of the namespaced Python-API calls the individual tools already make (never
kubectl, never a cluster-wide list), Warning events reuse `get_events`'
filter/sort/cap (the shared `K8S_MCP_MAX_EVENTS` knob, default 100), and the
whole render is bounded by **`K8S_MCP_TRIAGE_MAX_LINES`** (default 150; when
the cap bites, a marker naming the env is appended — narrow with `app=` or
raise it, so one triage call can never flood the context).

Output shape (attention first, so the cap can never hide the findings):

```
TRIAGE team-a: pods=4 workloads=2 warnings=1 pvcs=1 attention=2
ATTENTION (2):
  api-7d9f: CrashLoopBackOff — not Ready — 12 restarts
  indexer-c9d8: Pending
PODS (4): … name / phase / restarts / age / node …
WORKLOADS: … Deployment/StatefulSet/DaemonSet ready vs desired …
EVENTS (Warning) (1): …
PVCs (1): …
```

The **attention list** flags: any container waiting reason
(CrashLoopBackOff, OOMKilled, ImagePullBackOff, …), Pending/Failed/Unknown
phase, `Ready=False` on a Running pod, any restarts, and probe failures
(Warning events with reason `Unhealthy`) — Succeeded pods (completed Jobs)
are normal and stay unflagged. The optional `app` argument narrows the PODS
and WORKLOADS sections (and the attention list) with a label-first,
substring-fallback match (`app` / `app.kubernetes.io/name` labels, else the
pod/workload name — the `list_pods` label-selector convention); events and
PVCs stay namespace-wide context.

Two deliberate omissions:

- **Log correlation is not wired in.** Triage summarizes state; the
  prometheus → k8s-mcp → logsearch drill-down stays agent-orchestrated. A
  composite that pulled logs would either truncate them arbitrarily or blow
  the context budget it exists to protect.
- **Composite-only failure semantics.** If one non-core read fails, its
  section renders an honest `Error - …` line while the rest of the summary
  still renders; a failed pod read fails the whole triage exactly like
  `list_pods`. Partial truth is acceptable, silent partial truth is not.

### Design note: write-path identity pass-through (k8s-mcp ↔ applygate) — deferred

Fleet audit wiring item (c): cluster writes made through applygate should
inherit the **per-client identity** k8s-mcp already resolves. Design,
intentionally not built (no code, no chart change):

- k8s-mcp's `_ApiKeyAuthMiddleware` resolves each request to a `_Caller`
  (client name from `K8S_MCP_CLIENTS`, or the shared key) and stores it in a
  contextvar.
- When an agent's workflow chains a k8s-mcp read into an applygate write,
  applygate today records only *its* key fingerprint — the originating
  k8s-mcp client name is lost.
- Proposed pass-through: the shared middleware (`mcp_fleet_common`, which
  already plans to move caller identity into `mcp_auth.py` — applygate
  mirrors K8S-MCP's contextvar pattern locally) gains one forwarded identity
  header (e.g. `X-MCP-Caller: <name>`), set by k8s-mcp's middleware and
  honored by applygate **only as attribution, never as authorization**:
  applygate's own API-key check still applies, and the header can only widen
  the audit record, never the permission. Hash-chained applygate audit then
  reads `caller=<applygate-key-fp> via k8s-mcp:<client-name>`.
- Build it in the shared-middleware wave so both servers consume the one
  middleware instead of growing another point-to-point shim.

## 4. Namespace governance

`K8S_MCP_ALLOWED_NAMESPACES` / `K8S_MCP_BLOCKED_NAMESPACES` — comma-separated
`fnmatch` globs (`team-*`); **blacklist always wins**; empty = every
namespace readable.

- Fail-loud at startup on malformed values; malformed values at request time
  degrade to a clean denial.
- **Reads**: cluster-wide results are **filtered** to the policy;
  `list_namespaces` shows only policy-visible namespaces.
- **kubectl-backed tools**: explicit `-n` checked; namespaced queries without
  `-n` rejected with a hint; `-A`/`--all-namespaces` rewritten per allowed
  namespace (glob expansion against the live namespace list, cap 20) or
  rejected under a blacklist-only policy.
- **Cluster-scoped resources unaffected**; `auth can-i` is context-only by
  design.

**The allowlist is the recommended posture.** An empty
`namespaces.allowed` leaves a blacklist-only policy (or an open one, with no
blocked list either): every namespace you did not name is readable,
*including namespaces created later*. That is a floor, not a boundary. The
server is loud about it by design — at startup it prints the warning and a
posture banner:

```
WARNING: namespace policy is BLACKLIST-ONLY — K8S_MCP_ALLOWED_NAMESPACES is empty, so every namespace not in K8S_MCP_BLOCKED_NAMESPACES (…) is visible, including namespaces created later. …
POSTURE: namespaces: BLACKLIST-ONLY — … | exec: ENABLED — label gate ENFORCED …
```

Silence the warning deliberately with `K8S_MCP_WARN_EMPTY_ALLOWLIST=false`
(default: warn). RBAC remains the true boundary — this app-layer policy is
defence in depth and can never grant what RBAC denies.

## 5. Hardened container exec (opt-in)

Exec is arbitrary code execution inside a workload container — it can never
be made read-only. The design goal is therefore not "safe exec" but **keep
the debugging power, shrink the blast radius**, with eight independent layers
each enforceable on its own. Off unless `K8S_MCP_EXEC_ENABLED=true`; the tool
is not even registered otherwise.

| Layer | Mechanism | Enforced by |
| --- | --- | --- |
| 1. Opt-in | Tool not registered unless `exec.enabled: true` (`K8S_MCP_EXEC_ENABLED`) | server |
| 2. RBAC | `pods/exec` `create` granted only by **namespaced** RoleBindings (never a ClusterRoleBinding) | API server — the real boundary |
| 3. Per-pod opt-in | Target pod must carry label `k8s-mcp.io/exec: "true"` (`exec.requireLabel`, env `K8S_MCP_EXEC_REQUIRE_LABEL`) | server (RBAC cannot match labels; the app can) |
| 4. Argv-typed tool | `command: list[str]`, one command, passed after kubectl's `--` (flag parsing stops; no shell on either side) | server + kubectl flag parsing |
| 5. Binary allowlist | Basename of `command[0]` in a read-only set (`ps`, `ls`, `cat`, `tail`, `head`, `grep`, `df`, `du`, `free`, `ss`, `netstat`, `ip`, `stat`, `id`, …), extendable via `exec.allowedCommands`. `env`/`printenv` (Secret dumps) and `curl`/`wget`/`nc` (exfil channels) excluded by default | server |
| 6. Hard denies | Shells, interpreters, `su`/`sudo`, `nsenter`/`unshare`; `find -exec` / `ip netns` tokens rejected — never overridable | server |
| 7. Self-pod guard | Exec into the MCP server's own pod refused (its SA token is the crown jewels) | server |
| 8. Bounded + audited | No TTY/stdin, 30 s timeout with race-safe kill/reap, 50k output truncation, `AUDIT exec decision=…` for every allow *and* deny | server / log collection |

**The label is the boundary.** With exec enabled and `exec.requireLabel:
false`, the server prints a startup WARNING, records `label_gate=off` in
every exec audit entry, appends the posture to every denial *and* every
successful result, and advertises the open posture in the `exec_in_pod` tool
description — an open deployment cannot look hardened. Never ship `false`
outside a throwaway lab.

With exec enabled and a namespace list set, the server provisions its own
`pods/exec` RoleBindings at startup (`exec.autoRbac`, default on — §6).
`run_kubectl` **never** gains `exec`.

Honest limits: an allowlisted binary still runs with the container's
privileges (`cat` reads everything that container mounts) — treat every
exec-able pod as readable-by-the-key-holder and use the label as the
per-workload decision. For stronger isolation run a second, separate
deployment with an exec-only ServiceAccount and keep an API-server audit
policy on `pods/exec`.

### The exec audit trail

`exec_in_pod` runs an attacker-influenced argv inside a production
container, so its audit record is the only forensic trace of who ran what
where. Entries are written as JSONL through the fleet's shared
`HashChainedAuditLog` (the same writer applygate uses) and additionally
echoed to stdout in the legacy `AUDIT exec ...` shape, so existing log
matchers keep working.

The container filesystem is read-only, so the trail needs a writable mount
(`audit.mountPath`, default `/var/lib/k8s-mcp`; the file is
`<mountPath>/audit.jsonl` via `K8S_MCP_AUDIT_FILE`). With no writable path
the server degrades to stdout-only — best-effort, never blocking a tool call.
`emptyDir` survives container restarts but **not** pod rescheduling: set
`audit.persistence.existingClaim` when the trail must outlive the pod, which
is the recommended posture wherever exec is enabled.

## 6. Automatic exec RBAC provisioning

- At startup (or `python -m k8s_mcp.server --provision-rbac`), the server
  ensures its own `pods/exec` RoleBindings for every namespace on the exec
  list — glob patterns expanded against the live namespace list,
  general-policy-blocked and nonexistent namespaces skipped, idempotent
  (`already present`), foreign bindings never touched.
- **Escalation-proof by construction**: the provisioner permission is `bind`
  + `get` **resourceNames-scoped** to the single exec-template ClusterRole —
  it can never reference another role, create or modify role content, or
  create ClusterRoleBindings.
- **Migration-safe**: bindings whose subject points at a previous
  ServiceAccount/namespace are **repointed automatically** (the fix that made
  the dedicated-namespace migration a one-command operation — 236 bindings
  repointed in the live migration).
- **Degrade, don't die**: any provisioning failure is returned as a log line;
  it can never crash the server (a v0.1.0 crashloop taught this the hard way).
- **Per-release naming**: the provisioned role/binding are
  `<deployment.name>-pods-exec` / `<deployment.name>-exec`
  (chart-rendered `K8S_MCP_EXEC_TEMPLATE_ROLE` / `K8S_MCP_EXEC_BINDING_NAME`;
  `K8S_MCP_SA_NAME` / `K8S_MCP_SA_NAMESPACE` override the assumed SA identity
  for bare-local runs), so multiple chart releases coexist in one cluster
  without colliding.

## 7. Reliability, performance and cost control

- **Event loop never blocks**: kubectl via `asyncio.create_subprocess_exec` +
  bounded `wait_for`; every Kubernetes Python-client call via
  `asyncio.to_thread`. A hung kubectl is killed and reaped in bounded time,
  and concurrent tool calls do not serialize.
- **Concurrent fan-out** (`asyncio.gather`) in `cluster_health` and
  `list_workloads`; `get_custom_resource` uses the dynamic client with a
  kubectl fallback only on discovery/schema errors.
- **In-process `get_resource` listings.** An un-named listing
  (`get_resource("pods")`) no longer spawns kubectl and renders one full YAML
  document per item: kinds the dynamic client resolves in-process return one
  compact `namespace/name (Age: …)` line per item — policy-filtered, capped by
  `K8S_MCP_MAX_LIST_ITEMS` (default 100), with the same truncation marker as
  the other Python-API listings. Named gets and `jsonpath=`/`custom-columns=`/
  `wide`/`name` output keep kubectl's byte-exact output (tests and the console
  parse them; PyYAML and Go disagree on timestamp quoting and `< > &` JSON
  escaping, so a Python-rendered named get would not be byte-identical).
  Unknown kinds, group-qualified tokens, and any dynamic-path exception fall
  back to the kubectl path unchanged. Under a whitelist the compact listing is
  policy-FILTERED (like `list_pods`) rather than rewritten per namespace;
  under a blacklist-only policy it answers with filtered data where the kubectl
  `-A` path refuses.
- **Namespace-glob expansion is cached (30 s TTL).** A whitelist `-A` rewrite
  expands its globs against one live `list_namespace` result per 30-second
  window instead of one live lookup per query. Staleness ≤30 s is intentional
  (a namespace created/deleted in the window may be missing from / present in
  one rewrite plan — per-namespace results are still policy-filtered); API
  errors bypass the cache and behave exactly as before. Exact whitelists (no
  globs) never hit the cluster.
- **Parallel cluster-wide rewrites.** When a namespace whitelist rewrites one
  `-A` query into up to `K8S_MCP_MAX_NS_REWRITE` (default 20) per-namespace
  kubectl spawns, they run concurrently under a bounded semaphore —
  `K8S_MCP_LIST_CONCURRENCY` (default 8) bounds the width. The merged output
  is byte-identical to the old sequential loop (same sections,
  namespace-sorted order), proven by a mocked-kubectl test comparing widths 1
  and 8.
- **Cluster-wide list caps.** Listings without a namespace are capped at
  `K8S_MCP_MAX_LIST_ITEMS` items (default 100): the Python-API-backed tools
  (`list_pods`, `list_services`, `list_workloads`, `list_pvcs`,
  `list_virtual_services`, `get_custom_resource`) and the in-process
  `get_resource` fast path; when the cap bites, the output ends with a
  truncation marker naming the env — page per-namespace with `-n` or raise it.
  Namespaced listings and kubectl-backed output elsewhere (already bounded by
  the 50k-char truncation) are unchanged.
- **Event cap.** `get_events` and `triage`'s Warning section cap at
  `K8S_MCP_MAX_EVENTS` items (default 100; the same knob raises both
  consistently).
- **The caps are why this server is cheap to run.** A cluster with hundreds
  of namespaces must not turn "list the pods" into a megabyte of context; the
  default cap is a context-budget decision, and the marker is what makes it
  honest.
- **Memory posture**: steady state is ~136 MiB, but heavy `get_resource`
  listing bursts (full-YAML renders + concurrent kubectl fan-out) OOMKilled
  the old 512Mi limit four times. Both charts now default to a **2 GiB**
  limit — the burst is what counts, not the average.

## 8. Deployment experience

- **All knobs are values-driven** (`K8S_MCP_*` env vars underneath): chart
  values are the source of truth; `kubectl set env` still works day-2 and
  triggers a rollout.
- **The API key Secret is out-of-band** — an envsubst apply can no longer
  empty it.
- **`MCP_HOSTNAME` is derived from the endpoint**, so the public hostname is
  declared once and reaches the container (see §2).
- **Private ghcr images**: `imagePullSecrets` is a values key in both charts.
- **Wildcard apiGroups are refused at render time in both charts** — `""` or
  `"*"` in `extraResourceGroups` would re-grant Secret reads; the guard is
  enforced, not just documented.
- **Exec RBAC names are env-overridable** so multiple releases coexist in one
  cluster without RoleName collisions.
- **The read-only RBAC role is the deployment's real boundary** — a
  namespace clamp is `rbac.scope: namespace`, which also turns auto-RBAC off
  (the server cannot bind cluster-scoped templates there).
- **The browser path has a kill-switch, not a default.** `ezua.virtualService.enabled:
  false` un-renders the VirtualService (no gateway host at all) while
  `ezua.enabled` keeps driving every other gate. Templates must gate DIRECTLY
  on the key — Sprig's `default` treats an explicit `false` as empty, so a
  `default` wrapper silently ignores the operator's intent. Closing a console
  is an explicit per-chart, per-site decision, never a default.

## 9. Observability

`K8S_MCP_METRICS_ENABLED=true` (chart `metrics.enabled`, default **off**)
makes the same pod serve `GET /metrics` (Prometheus text exposition 0.0.4).
Counters only, nothing cluster-derived: `k8s_mcp_server_events_total{kind,outcome}`
— HTTP auth outcomes (`ok`/`unauthorized`/`bad_header`/`open`), kubectl batch
outcomes, exec allow/deny decisions, and cluster-wide list truncations.

When `prometheus-client` is importable it renders the exposition; otherwise a
dependency-free fallback registry emits the same counter family (no install
required, and metrics can never affect a tool call). The endpoint is
unauthenticated like the console shell — enable it only where the scrape path
is trusted. `metrics.enabled: true` also renders the Prometheus Operator
`ServiceMonitor` (scrape port `mcp`, path `/metrics`, `metrics.interval`
default 30 s).

Both are default-off so the default chart render is **byte-identical** to the
pre-metrics baseline — an opt-in feature that changes nothing when unused is
an opt-in feature nobody has to audit.

## 10. Built-in ops console

A static, HPE-branded single-page console served by the server itself
(`/` redirects to `/ui/`) — a **universal MCP client with curated dressing**:

- **Same endpoint, same guards.** The browser calls `/mcp` with your API key —
  the identical auth middleware, namespace policy, read-verb allowlist, exec
  gates and RBAC that MCP clients get. The console cannot do anything a client
  cannot.
- **Curated screens:** cluster health · namespaces · pods (click a row → logs,
  or straight into hardened exec when enabled) · workloads · events ·
  services · VirtualServices · ConfigMaps · PVCs · Secrets (names only) · CRD
  lookup · RBAC can-I · any-resource get/describe · a read-only kubectl
  console — with searchable comboboxes for namespaces/pods/containers and a
  full-screen output panel.
- **"Any tool" runner:** every registered tool, with a form generated from its
  `inputSchema` — future server tools appear without UI changes.
- **Exec screen only when the server has it:** the tool is gated by
  `K8S_MCP_EXEC_ENABLED` server-side; the UI mirrors the binary allowlist and
  offers the `X-Exec-Namespaces` narrowing header (it can only narrow).
- **Hygiene:** the shell carries no data (no key needed to load it); the key
  lives in memory/sessionStorage (never localStorage); all cluster output is
  rendered as text, never HTML; CSP `default-src 'none'` + nosniff +
  no-referrer on the shell; traversal-guarded static handler.
  `console.enabled: false` removes the shell entirely.
- **Speaks the 2026-07-28 envelope** (`Mcp-Method`/`Mcp-Name` headers +
  `_meta`) with an automatic legacy-2025 fallback; verified over real HTTP in
  both eras.

## 11. Two charts: trusted vs structurally locked

Two charts in the PCAI house style (values-driven naming, an `ezua:`
integration block, `/mcp`-first VirtualService routing, committed packaged
`.tgz`, `.helmignore`d `local/` site values). One image; the difference is
what the chart *reads*:

- **`helm/` (k8s-mcp) — trusted operators, fully frontend-configurable**:
  exec + exec namespaces, namespace policy, per-user clients, `rbac.scope`,
  `extraResourceGroups`, provisioner auto-RBAC, `ezua` exposure — all values.
- **`helm-customer/` (k8s-mcp-customer) — structurally locked**: the security
  keys (exec, namespace policy, per-user clients, RBAC scope and extra groups)
  **do not exist in its values and its templates never read them** — pasting
  `exec: {enabled: true}` into the PCAI frontend is inert. The read-only
  ClusterRole is baked via a chart constant (clamp a tenant by editing the
  constant and repackaging — a platform action); exec RBAC is never minted, so
  exec becomes possible only via the deliberate `NOTES.txt` runbook
  (`kubectl set env` + manual Role and RoleBinding creation).

**Why the split.** With one chart, the customer owns any `lockdown` flag
itself, so values-based guards were advisory. With two charts the locked
posture is structural — there is nothing to flip. (A values-controlled
`lockdown` flag existed in chart v0.2.0/0.2.1 and was removed in v0.2.2 in
favour of the split.)

Container hardening is fixed in **both** charts. The image build ends with a
permission neutralizer (`RUN chmod -R a+rX /app`) — files that land mode-0600
on the ops box are COPYed mode-preserving and would otherwise brick the
non-root pod at boot (the v0.2.3/v0.2.4 console crash class). Release tooling
(`./automation.sh <version>` / `./bump_version.sh <version>`) bumps **both**
charts, their `image.tag`, and packages them.

## 12. Verification

- `tests/test_namespace_policy.py` — **168 self-contained checks**: namespace
  policy and globs, precedence (blacklist wins), exec command guard
  (allowlist, hard-denies, token denials), argv building, per-client
  parsing/auth/narrowing, provisioning (create/keep/patch/skip/refuse/degrade),
  API-key middleware (401 paths, both headers, dev mode), MCP 2.0 envelope
  behavior, `check_rbac` answer surfacing, the VirtualServices tool
  (summary/detail rendering, policy filtering, kubectl fallback argv), and the
  built-in console (routing, CSP, traversal guards, disable knob). Runs
  without a cluster (the `kubernetes` package is stubbed when absent).
- `tests/test_wave3_hardening.py` — the hardening matrix: impersonation
  denylist including `=` forms, fan-out parity, list caps, `/metrics`.
- `tests/test_triage.py` — the triage matrix: exact mixed-health attention
  list, app narrowing, composite cap, policy refusals byte-identical to
  `list_pods`, governed-paths-only proof.
- `tests/test_perf_wave.py` — the performance wave: in-process listing
  rendering, caps, rewrite caching.
- `smoke_console.py` — boots the REAL ASGI wiring (uvicorn, kubernetes
  stubbed) and verifies over HTTP: static shell + CSP, traversal 404s, console
  toggle, modern `tools/list`/`tools/call` with the 2026-07-28 envelope,
  legacy SSE fallback, and the 401 gate with the console-shell exemption.
- `audit_forms.py` — schema↔form contract audit: **20 tools / 40 parameters**,
  every widget output type round-trips to its server schema type (run after
  ANY change to tool signatures or `ui/app.js` widgets).
- **Live protocol conformance**: stateless `tools/list`+`call`,
  `server/discover`, cache hints, legacy-era fallback, strict envelope
  rejection — verified over real HTTP.
- **Live deployment verification** (production cluster): 236 exec
  RoleBindings provisioned and repointed across a namespace migration;
  `can-i create pods/exec` yes in kept namespaces / no elsewhere; a real
  `ps` executed inside a running predictor through the full
  gateway→auth→policy→allowlist→RBAC chain; 401 gate and audit trail
  confirmed.

The post-deployment checklist against a running instance is
[`VERIFICATION.md`](VERIFICATION.md).

## 13. The MCP network zone (optional)

The trusted chart ships an ingress `NetworkPolicy` (`networkPolicy.enabled`,
default **false** — the default render is byte-identical to the baseline).
Once enabled it is a **default-deny ingress allowlist**: only the authorized
client namespaces, same-namespace pods, the node CIDRs in `probeCidrs` (kubelet
probes originate at the node) and — only if you flip
`allowEzafGatewayIngress` — the gateway pods can reach the server. Egress
stays unrestricted by design: admin MCPs legitimately reach the Kubernetes
API, Prometheus and the internet.

Three details are load-bearing:

- **Deny by absence.** There is no "deny rule" to read: anything not matched
  is denied. That is why the caller list is the whole policy.
- **The browser path stays open** through the SSO-gated edge gateway. The zone
  stops random *in-cluster* access; closing a console is an explicit
  per-chart, per-site decision, never a default.
- **An unlisted scrape namespace dies silently.** Prometheus scrapes
  `/metrics` on the same port; if you enable the zone and metrics, add
  `monitoring` to `authorizedClients.namespaces`.

The edge-pod label is live-verified as `app: istio-ingressgateway`
(`ezaf-gateway` is the Gateway CR name). `probeCidrs` must never be
`0.0.0.0/0` with an empty `probeExceptCidrs` — the template fails the render.
Procedure and the six steps: [DEPLOYMENT](DEPLOYMENT.md#enabling-the-network-zone).

## 14. MCP 2.0 conformance

- Stateless operation verified live against the real app: stateless
  `tools/list`, `tools/call`, `server/discover`
  (`supportedVersions=["2026-07-28"]`), legacy `2025-03-26` handshake honored
  on the same process, strict per-request envelope rejection (`-32602`
  naming the missing key), standard `-32602` for missing resources.
- **Cacheable list results enabled**: `tools/list` carries
  `ttlMs=300000, cacheScope=public` — safe because the tool list is static and
  identical for every caller.
- Header-based routing (`Mcp-Method`/`Mcp-Name`) validated against the body.
- Statelessness is why `deployment.replicaCount` can be raised freely: any
  replica serves any request.

## 15. Files

| File | Purpose |
| --- | --- |
| `src/k8s_mcp/server.py` | The server — 19 tools (20 with exec enabled), auth, policy, triage, exec, provisioning, console serving |
| `ui/` | Built-in HPE ops console (static: `index.html`, `style.css`, `app.js`) |
| `helm/` | Trusted-operator chart (k8s-mcp): every security knob values-configurable, `ezua:` exposure, Kyverno vendor labels; packaged `.tgz` at the repo root |
| `helm/values-examples/` | Secret-free paste-ready full-values examples (G2 lab, hardened G2) |
| `helm-customer/` | Locked customer distribution (k8s-mcp-customer): security keys absent from values and unread by templates, baked read-only RBAC, exec RBAC never minted — day-2 via the NOTES.txt kubectl runbook |
| `helm-customer/values-examples/` | Secret-free paste-ready examples for the locked chart |
| `helm/local/` | HPE-local site values — `.helmignore`d, never shipped |
| `helm-customer/local/` | Per-customer site values template — `.helmignore`d, never shipped |
| `k8s-mcp-2-0-server.yaml` | Legacy envsubst deployment manifest — no longer tracked (charts supersede it; `apply-mcp.sh` still references it) |
| `Dockerfile` | Pinned + checksum-verified kubectl, non-root, arch-aware, console included |
| [`documentation/DEPLOYMENT.md`](DEPLOYMENT.md) | Operator runbook (PCAI values-driven deploy, prerequisites, clients, exec, migration, rotation, troubleshooting) |
| `documentation/FEATURES.md` | This file |
| `documentation/VERIFICATION.md` | Post-deployment verification checklist |
| `tests/` | The pytest suites (no cluster required) |
| `smoke_console.py` | HTTP-level console + protocol smoke test (real ASGI wiring, kubernetes stubbed) |
| `audit_forms.py` | Schema↔form audit — boots the server (exec enabled), simulates the console's inferWidget/read matrix for every parameter of all 20 tools, exits non-zero on any widget↔schema type mismatch |

## 16. Release history

Historical record of the server line, oldest first. Current truth is
everything above this section; this table exists so a bisect has a story.

| Tag | Server | Highlights |
| --- | --- | --- |
| v0.0.1 | 2.0.0 | Original: shell injection, cluster-admin, no auth |
| v0.1.0 | 2.1.0 | Security audit fixes, API-key auth, namespace governance, hardened exec + auto-RBAC |
| v0.1.1 | 2.1.1 | Crashloop fix (dict bodies, degrade-don't-die provisioning), imagePullSecrets, `check_rbac` truth |
| v0.1.2 | 2.1.2 | `check_rbac` "no" surfacing, provisioner `get` grant, migration repointing, `MCP_HOSTNAME` knob |
| v0.1.3 | 2.1.3 | Host-header protection correctly scoped (the 421 fix), Secret out-of-band, `MCP_HOSTNAME` reaches the container |
| v0.2.0 | 2.2.0 | `list_virtual_services` (+ `networking.istio.io` RBAC grant), built-in ops console, Helm chart with internal / customer-lockdown profiles, env-overridable exec RBAC names, cluster-scoped singular forms in the kubectl guard |
| v0.2.1 | 2.2.0 | Chart conformed to the PCAI house style: `ezua:` integration block, `hpe-ezua/*` vendor labels via a Kyverno pre-install policy, values-driven naming, explicit `image.tag` in lockstep with appVersion, optional gateway AuthorizationPolicy, `.helmignore` for `helm/local/`, packaged `k8s-mcp-<ver>.tgz` |
| v0.2.2 | 2.2.0 | Two-chart split — `helm/` + `helm-customer/`; wildcard apiGroups refused at render in both; `lockdown` values flag removed |
| v0.2.3 | 2.2.0 | Console-serving fix attempt 1: `ui/*` shipped mode-600 in the image → uid-10001 got PermissionError; `COPY --chmod=0644` then stripped the execute bit from the `ui/` DIRECTORY |
| v0.2.4 | 2.2.0 | Console-serving fix attempt 2: `COPY ui/ ./ui/` + `RUN chmod -R a+rX /app` (dirs traversable, files readable); console verified live |
| v0.2.5 | 2.2.0 | Console dropdowns: namespace fields as real selects populated from the cluster, grouped object-of-interest select, pod-row → logs/exec prefills |
| v0.2.6 | 2.2.0 | Console form-engine fix: `readForm`/`prefillForm` queried *inside* the matched control, so every simple widget read `null` and every Run sent `{}` |
| v0.2.7 | 2.2.0 | Multi-container logs/exec: `get_pod_logs` parses the API's 400 detail into "set container to one of: …"; the console renders a container dropdown for 2+ containers |
| v0.2.9 | 2.2.0 | Searchable comboboxes for namespace / object / pod / container (type-to-filter, free text always valid) |
| v0.2.10 | 2.2.0 | Console polish: primary button rests in the deep brand green, hover/pressed/focus states |
| v0.2.11 | 2.2.0 | Container-picker fix: the pod-spec fetch used full `output=json`, which truncates at ~50k — a KServe predictor spec is ~56 KB and the picker silently never appeared; now `jsonpath={.spec.containers[*].name}` |
| v0.2.12 | 2.2.0 | Logs readability: some kubernetes-client versions return the log subresource as raw bytes, which rendered as a Python bytes repr (every newline escaped); now decoded, plus a full-screen output toggle |
| v0.2.13 | 2.2.0 | Wave-5 additive: `triage(namespace, app)` — §3; +40 tests |
| v0.2.14 | 2.2.0 | Performance wave: in-process `get_resource` listings, 30 s glob cache, `K8S_MCP_MAX_EVENTS` + `K8S_MCP_MAX_NS_REWRITE`, `get_pod_logs` namespace now REQUIRED; +35 tests |
| v1.2.0 / v1.3.0 | chart line | Gated `/metrics` + ServiceMonitor, the MCP network zone, 2 GiB memory limit, the hardened-admin VS kill-switch (`ezua.virtualService.enabled`) |
