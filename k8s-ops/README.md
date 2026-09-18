# k8s-mcp — Read-Only Kubernetes Ops MCP Server

k8s-mcp is a **read-only Kubernetes ops MCP (Model Context Protocol) server** on MCP 2.0 (protocol `2026-07-28`, official MCP Python SDK v2, server name `k8s-ops-server`) for HPE PCAI (Private Cloud AI / Ezmeral Unified Analytics). It exposes a read-only tool surface — pods, workloads, events, logs, services, Istio VirtualServices, ConfigMaps, Secrets (names only), PVCs, CRDs, RBAC, and a generic read-verb-only `kubectl` escape hatch — plus an opt-in **hardened exec tool** and a **built-in HPE ops console** (static web UI on the same pod). Every request passes an API-key middleware, a namespace policy, and a read-verb allowlist before anything touches the cluster. It ships two Helm charts — a fully configurable trusted-operator chart (`helm/`) and a structurally locked customer distribution (`helm-customer/`) — deployed the PCAI way: import the chart, edit values, apply.

## What problem(s) it solves

- **"Is anything wrong in the cluster?" for agents and humans** — one MCP endpoint answers cluster health, pod/workload state, events, logs, services, VirtualServices, ConfigMaps, PVCs, CRD lookup, and `auth can-i` RBAC checks without handing anyone a kubeconfig or `cluster-admin`.
- **Read-only by construction, not by convention.** kubectl runs as an argv list (no shell), a deny-by-default read-verb allowlist rejects every write path, connection-redirection flags are refused, and the RBAC role has no secret/RBAC/write verbs — `list_secrets` and `get secret …` fail with 403 from the API server itself.
- **Governed blast radius.** Namespace whitelist/blacklist (glob patterns, blacklist always wins), per-user API keys with per-user exec assignments, and an `X-Exec-Namespaces` header that can only narrow — capabilities travel with the credential.
- **Debugging power where it is earned.** Container exec (`exec_in_pod`) is opt-in and layered: RBAC via namespaced RoleBindings only, per-pod opt-in label, binary allowlist (no shells/interpreters/env/curl), self-pod guard, 30s timeout, output truncation, and an `AUDIT` log line for every allow *and* deny.
- **A universal console in the same pod.** `https://<endpoint>/ui/` renders curated screens (pods → logs → exec, workloads, events, …) plus an "any tool" runner driven by each tool's `inputSchema` — screenshots lie, schemas don't — with no second backend or drift: the browser calls `/mcp` with the same key and the same guards.
- **One image, two trust levels.** Internal operators get every knob as a value; customer PCAI catalogs get a chart whose security keys do not exist in values and whose templates never read them — nothing to bypass because there is no flag.

## Tools

19 tools are always registered (20 with exec enabled); names and parameters below are from `server.py`:

| Tool | Purpose |
| --- | --- |
| `triage` | One-call namespace health summary: attention list + pods + workloads + Warning events + PVCs (see below). |
| `cluster_health` | Node status, component health, resource usage — the "first tool" triage call. |
| `list_pods` / `list_workloads` / `list_services` / `list_pvcs` / `list_namespaces` | Cluster-wide or per-namespace listings with health/restarts/age. |
| `get_pod_logs` | Logs from a pod's container (`container`, `tail`, `previous` for crashed containers). |
| `get_events` | Cluster/namespace events, filterable by resource and type. |
| `get_resource` / `describe_resource` | Any built-in or custom resource, by type/name/namespace; describe adds events and conditions. |
| `list_api_resources` / `list_crds` / `get_custom_resource` | API/CRD discovery and dynamic-client reads of custom resources (e.g. InferenceServices). |
| `get_configmap` | ConfigMap data keys and values. |
| `list_secrets` | Secret names and types only — values are unreadable by design (no RBAC verbs). |
| `list_virtual_services` | Istio VirtualServices — summaries or the full definition. |
| `check_rbac` | Can the service account perform an action? (Surfaces "no" answers honestly.) |
| `run_kubectl` | Read-verb kubectl escape hatch: `get, describe, logs, top, explain, api-resources, api-versions, cluster-info, version, auth, events` only. |
| `exec_in_pod` *(opt-in)* | ONE read-only debugging command in a container — registered only when exec is enabled, hardened per the security model below. |

## Fleet triage (`triage`) — one-call namespace summary

`triage(namespace, app="")` answers "what needs attention in this namespace?" in one call. It is a **composite read**: it assembles its summary from this server's own governed read paths — the namespace policy is enforced up front exactly as `list_pods` enforces it (a denied namespace gets the identical refusal, before any underlying call), every Kubernetes read is one of the namespaced Python-API calls the individual tools already make (never kubectl, never a cluster-wide list), Warning events reuse `get_events`' filter/sort/100-item cap, and the whole render is bounded by **`K8S_MCP_TRIAGE_MAX_LINES`** (default 150; when the cap bites, a marker naming the env is appended — narrow with `app=` or raise it, so one triage call can never flood the context).

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

The **attention list** flags: any container waiting reason (CrashLoopBackOff, OOMKilled, ImagePullBackOff, …), Pending/Failed/Unknown phase, `Ready=False` on a Running pod, any restarts, and probe failures (Warning events with reason `Unhealthy`) — Succeeded pods (completed Jobs) are normal and stay unflagged. The optional `app` argument narrows the PODS and WORKLOADS sections (and the attention list) with a label-first, substring-fallback match (`app` / `app.kubernetes.io/name` labels, else the pod/workload name — the `list_pods` label-selector convention); events and PVCs stay namespace-wide context. Log correlation is deliberately **not** wired in: triage summarizes state, and the prometheus→k8s-mcp→logsearch drill-down stays agent-orchestrated (fleet audit §3.5). Composite-only failure semantics: if one non-core read fails, its section renders an honest `Error - …` line while the rest of the summary still renders; a failed pod read fails the whole triage exactly like `list_pods`.

### Design note: write-path identity pass-through (k8s-mcp ↔ applygate) — deferred to Wave 6

Fleet audit wiring item (c): cluster writes made through applygate should inherit the **per-client identity** k8s-mcp already resolves. Design, intentionally not built yet (no code, no chart change in this wave):

- k8s-mcp's `_ApiKeyAuthMiddleware` resolves each request to a `_Caller` (client name from `K8S_MCP_CLIENTS`, or the shared key) and stores it in a contextvar.
- When an agent's workflow chains a k8s-mcp read into an applygate write, applygate today records only *its* key fingerprint — the originating k8s-mcp client name is lost.
- Proposed pass-through: the shared middleware (Wave-6 `mcp-fleet-common` consolidation, which already plans to move caller identity into `mcp_auth.py` — see audit Wave-3 note "applygate mirrors K8S-MCP's contextvar pattern locally") gains one forwarded identity header (e.g. `X-MCP-Caller: <name>`), set by k8s-mcp's middleware and honored by applygate **only as attribution, never as authorization**: applygate's own API-key check still applies, and the header can only widen the audit record, never the permission. Hash-chained applygate audit then reads `caller=<applygate-key-fp> via k8s-mcp:<client-name>`.
- Build it in W6 so both servers consume the one shared middleware instead of growing another point-to-point shim now.

## Architecture

- **One hardened container.** Non-root (uid/gid 10001), read-only root filesystem, all capabilities dropped, RuntimeDefault seccomp, `/tmp` as emptyDir; kubectl pinned and checksum-verified at image build. In-cluster it uses the pod's service-account token automatically.
- **Exposure.** The chart's Istio VirtualService (ezaf-gateway) routes `/mcp` (long timeout) and `/` → `/ui/` (the console) to one Service on 9090. The endpoint also becomes `MCP_HOSTNAME` in the container, pinning the Host header (DNS-rebinding protection). The Kyverno pre-install policy stamps the `hpe-ezua/*` vendor labels PCAI expects.
- **Protocol.** Stateless streamable-HTTP at `/mcp` — no handshake/session for modern clients, so any replica behind a plain round-robin LB serves any request; legacy 2025-era clients are still answered (same process, nothing to configure). `tools/list` is cacheable (`ttlMs=300000`). Verified live: stateless `tools/list`/`tools/call`, `server/discover`, strict envelope rejection (`-32602` naming the missing `_meta` key), header/body routing validation.

## Security model

The audit that shipped with v1 fixed the following; do not regress them (details and history in [documentation/FEATURES.md](documentation/FEATURES.md)):

1. **No shell.** kubectl is invoked as an argv list via `asyncio.create_subprocess_exec` — denylist-era bypasses (`&&`, `;`, backticks, `$()`, `>`, newlines) arrive as inert tokens.
2. **Read-verb allowlist.** The escape hatch accepts only `get, describe, logs, top, explain, api-resources, api-versions, cluster-info, version, auth, events` — the old denylist missed `run`, `exec`, `attach`, `cp`, `port-forward`, `proxy`, `debug`, `set`, …
3. **Connection + impersonation flags rejected.** `--server`, `--token`, `--kubeconfig`, `--client-certificate`, `--client-key`, `--username`, `--password`, `--certificate-authority`, `--insecure-skip-tls-verify` — they could redirect the API connection or swap credentials — plus the impersonation flags `--as`, `--as-group`, `--as-uid` in every `=`-joined form too (defense in depth: the shipped ClusterRole grants no `impersonate`, so the attempt dies at parse time instead of at the API server).
4. **Parameter validation.** Namespace (DNS label), names, types, and output formats are regex-validated before reaching kubectl or the API.
5. **Least-privilege RBAC.** A custom read-only role — **not** `cluster-admin`. Secrets and RBAC objects are deliberately excluded; `networking.istio.io/virtualservices` is granted for `list_virtual_services`; extend CRD apiGroups per operator, never with `["*"]` (refused at render time — it would re-grant secrets read).
6. **Pod hardening.** Non-root 10001, read-only rootfs, all capabilities dropped, RuntimeDefault seccomp, `/tmp` emptyDir — fixed in both charts, not values-overridable.
7. **Pinned toolchain.** kubectl pinned and sha256-verified at build.
8. **API-key auth.** Every request must present the key (`Authorization: Bearer <key>` or `X-API-Key: <key>`); constant-time comparison, 401 before the MCP app is reached. The key lives in an out-of-band Secret — the chart never creates or inlines it (an envsubst apply once emptied a live Secret) and the Deployment fails loud until it exists.
9. **Optional mesh identity.** The chart can render an Istio AuthorizationPolicy (oauth2-proxy) for the host — a shared API key authenticates the *key*, not a user; layer JWT when you need identity.

### API key authentication

The server requires a static API key on every request (item 8). Operators create it out of band and rotate via Secret replacement + rollout restart — the exact commands are in [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md#api-key-rotation--audit-trail). MCP clients configure it as a plain HTTP header:

```json
{
  "mcpServers": {
    "k8s-ops": {
      "url": "https://k8s-mcp.<your-domain>/mcp",
      "headers": { "Authorization": "Bearer <API_KEY>" }
    }
  }
}
```

`{"X-API-Key": "<API_KEY>"}` is accepted as an alternative header name. The key gates *who can call the tools*, not *which namespaces* — combine with the namespace policy below. Local development: `export K8S_MCP_API_KEY=dev-secret` before `python server.py` (unset = endpoint open, with a loud startup warning — never deploy that way).

### Namespace governance (optional whitelist/blacklist)

Values: `namespaces.allowed` / `namespaces.blocked` (comma-separated fnmatch globs such as `team-*`; blacklist always wins; empty = every namespace). Underlying env vars: `K8S_MCP_ALLOWED_NAMESPACES` / `K8S_MCP_BLOCKED_NAMESPACES`. When a policy is active: namespace-taking tools reject denied namespaces with an explanatory error; Python-API-backed tools **filter** denied namespaces from cluster-wide results; kubectl-backed tools enforce through the escape hatch (explicit `-n` checked, namespaced queries without `-n` rejected with a hint, `-A` rewritten per whitelisted namespace or rejected under blacklist-only policy). Cluster-scoped resources and `auth can-i` are unaffected.

### Per-user exec assignments (optional)

`clients.value` / `clients.existingSecret` maps keys to assignments (`name:key[:exec-ns-patterns];…`) so users cannot widen their own scope; each client connects with their own key; the shared key keeps working as the deployment-wide ceiling; RBAC provisioning covers the union; every exec AUDIT line names the calling client. The optional `X-Exec-Namespaces` request header narrows (never widens) a request's exec scope.

### Hardened kubectl exec (opt-in)

Exec is arbitrary code execution inside a workload container — it can never be made read-only. The design goal: keep the debugging power, shrink the blast radius with eight independent layers, each enforceable on its own:

| Layer | Mechanism | Enforced by |
| --- | --- | --- |
| 1. Opt-in | Tool not registered unless `exec.enabled: true` (`K8S_MCP_EXEC_ENABLED`) | server |
| 2. RBAC | `pods/exec` `create` granted only by **namespaced** RoleBindings (never a ClusterRole) | API server — the real boundary |
| 3. Per-pod opt-in | Target pod must carry label `k8s-mcp.io/exec: "true"` (`exec.requireLabel`) | server |
| 4. Argv-typed tool | `command: list[str]`, one command, passed after kubectl's `--` | server + kubectl flag parsing |
| 5. Binary allowlist | Basename of `command[0]` in a read-only set (`ps`, `ls`, `cat`, `tail`, `head`, `grep`, `df`, `du`, `free`, `ss`, `netstat`, `ip`, `stat`, `id`, …), extendable via `exec.allowedCommands` | server |
| 6. Hard denies | Shells, interpreters, `su`/`sudo`, `nsenter`/`unshare`; `find -exec` / `ip netns` tokens rejected — never overridable | server |
| 7. Self-pod guard | Exec into the MCP server's own pod refused (its SA token is the crown jewels) | server |
| 8. Bounded + audited | No TTY/stdin, 30s timeout, 50k output truncation, `AUDIT exec decision=…` for every allow and deny | server / log collection |

With exec enabled and a namespace list set, the server provisions its own pods/exec RoleBindings at startup (`exec.autoRbac`, default on) — escalation-proof by construction (template-role-scoped `bind`, no role-content writes, no ClusterRoleBindings, skips blocked/nonexistent namespaces); `run_kubectl` NEVER gains `exec`. Honest limits: an allowlisted binary still runs with the container's privileges (`cat` reads everything that container mounts) — treat every exec-able pod as readable-by-the-key-holder and use the label as the per-workload decision; for stronger isolation run a second, separate deployment with an exec-only ServiceAccount and keep an API-server audit policy on `pods/exec`.

## Deployment (PCAI way)

Users never run `helm install` or `kubectl apply` to deploy on PCAI: the packaged chart is imported into the PCAI catalog once, then the deployment is created from the PCAI UI (or PCAI API), and its **Helm Values** editor is where every knob is set. PCAI resolves `${DOMAIN_NAME}` in the `ezua` values before rendering.

| Chart | When | Notes |
| --- | --- | --- |
| `helm/` (k8s-mcp) | HPE / trusted operators | Every knob below is values-configurable |
| `helm-customer/` (k8s-mcp-customer) | Customer PCAI catalogs | Structurally locked: exec/policy/clients/RBAC keys don't exist in values; pasting them is inert |

**Required values** (both charts):

```yaml
apiKey:
  existingSecret: ""    # pre-created out of band (the chart never creates it)
ezua:
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "k8s-mcp.${DOMAIN_NAME}"   # unique per release on the gateway
```

**Optional values** (trusted chart) — `console.enabled`, `rbac.scope` + `rbac.extraResourceGroups`, `namespaces.allowed`/`blocked`, `exec.*`, `clients.*`, `listing.*`, `metrics.*`, `imagePullSecrets`, `resources`, `ezua.authorizationPolicy.*`. Full walkthrough: [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md); paste-ready examples: [`helm/values-examples/`](helm/values-examples/README.md) and [`helm-customer/values-examples/`](helm-customer/values-examples/README.md).

## Built-in ops console (`/ui/`)

A static, HPE-branded single-page console served by the server itself (`/` redirects to `/ui/`) — a **universal MCP client with curated dressing**:

- **Same endpoint, same guards.** The browser calls `/mcp` with your API key — the identical auth middleware, namespace policy, read-verb allowlist, exec gates and RBAC that MCP clients get. The console cannot do anything a client cannot.
- **Curated screens:** cluster health · namespaces · pods (click a row → logs, or straight into hardened exec when enabled) · workloads · events · services · VirtualServices · ConfigMaps · PVCs · secrets (names only) · CRD lookup · RBAC can-I · any-resource get/describe · a read-only kubectl console — with searchable comboboxes for namespaces/pods/containers and a full-screen output panel.
- **"Any tool" runner:** every registered tool, with a form generated from its `inputSchema` — future server tools appear without UI changes.
- **Exec screen only when the server has it:** the tool is gated by `K8S_MCP_EXEC_ENABLED` server-side; the UI mirrors the binary allowlist and offers the `X-Exec-Namespaces` narrowing header (it can only narrow).
- **Hygiene:** the shell carries no data (no key needed to load it); the key lives in memory/sessionStorage (never localStorage); all cluster output is rendered as text, never HTML; CSP `default-src 'none'` + nosniff + no-referrer on the shell; traversal-guarded static handler. `console.enabled: false` removes the shell entirely.

## Performance notes

- kubectl runs under `asyncio.create_subprocess_exec` and all Kubernetes Python client calls run under `asyncio.to_thread` — neither blocks the event loop, so concurrent tool calls don't serialize; every kubectl call is bounded by a 30s timeout.
- `cluster_health` and `list_workloads` issue their independent API calls concurrently; `get_custom_resource` uses the dynamic client and only falls back to kubectl on discovery/schema errors.
- **Parallel cluster-wide rewrites.** When a namespace whitelist rewrites one `-A` query into up to 20 per-namespace kubectl spawns, they now run concurrently under a bounded semaphore — `K8S_MCP_LIST_CONCURRENCY` (default 8) bounds the width. The merged output is byte-identical to the old sequential loop (same sections, namespace-sorted order), proven by a mocked-kubectl test comparing widths 1 and 8.
- **Cluster-wide list caps.** Python-API-backed listings without a namespace (`list_pods`, `list_services`, `list_workloads`, `list_pvcs`, `list_virtual_services`, `get_custom_resource`) are capped at `K8S_MCP_MAX_LIST_ITEMS` items (default 500); when the cap bites, the output ends with a truncation marker naming the env — page per-namespace with `-n` or raise it. Namespaced listings and kubectl-backed output (already bounded by the 50k-char truncation) are unchanged.

## Observability (`/metrics`, opt-in)

`K8S_MCP_METRICS_ENABLED=true` makes the same pod serve `GET /metrics` (Prometheus text exposition 0.0.4). Counters only, nothing cluster-derived: `k8s_mcp_server_events_total{kind,outcome}` — HTTP auth outcomes (`ok`/`unauthorized`/`bad_header`/`open`), kubectl batch outcomes, exec allow/deny decisions, and cluster-wide list truncations. When `prometheus-client` is importable it renders the exposition; otherwise a dependency-free fallback registry emits the same counter family (no install required, and metrics can never affect a tool call). The endpoint is unauthenticated like the console shell — enable it only where the scrape path is trusted. In the chart, set `metrics.enabled: true` to also render the Prometheus Operator `ServiceMonitor` (scrape port `mcp`, path `/metrics`, `metrics.interval`, default 30s); both are default-off, so the default chart render is byte-identical to the pre-metrics baseline.

## Requirements & development

`mcp>=2.0.0,<3`, `kubernetes>=31.0.0` (see `requirements.txt`). The adversarial suite runs without a cluster under pytest — `python3 -m pytest -q` in this directory collects **277 tests**: the 168-check namespace-policy/command-guard suite (one test per original check, `test_namespace_policy.py` — also still runnable standalone), the 26-check console smoke (`test_smoke_console.py`), the schema↔form contract audit (`test_audit_forms.py`), the Wave-3 hardening matrix (`test_wave3_hardening.py`: impersonation denylist incl. `=` forms, fan-out parity, list caps, `/metrics`), and the Wave-5 triage matrix (`test_triage.py`: exact mixed-health attention list, app narrowing, composite cap, policy refusals byte-identical to `list_pods`, governed-paths-only proof). `python3 smoke_console.py` and `python3 audit_forms.py` remain available as standalone scripts. To build the image: `docker buildx build -t ghcr.io/ai-solution-eng/k8s-mcp:v<ver> . --push`.

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Values walkthrough (required vs optional), out-of-band API key, namespace policy/exec/per-user keys day-2 ops, ezua/Istio, customer chart, upgrading, MCP fleet context, troubleshooting |
| [documentation/FEATURES.md](documentation/FEATURES.md) | Hardening & capability changelog (v0.0.1 → v0.2.12) — what was fixed and why |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | Auth gate check, MCP 2.0 handshake, one-tool test, optional operator kubectl, troubleshooting |
| [helm/values-examples/](helm/values-examples/README.md) | Paste-ready, secret-free full-values examples (trusted chart) |
| [helm-customer/values-examples/](helm-customer/values-examples/README.md) | Paste-ready, secret-free examples (locked customer chart) |
| [helm/values.yaml](helm/values.yaml) / [helm-customer/values.yaml](helm-customer/values.yaml) | Chart defaults — the authoritative list of every knob |
