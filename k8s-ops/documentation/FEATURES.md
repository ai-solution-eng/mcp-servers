# FEATURES — Hardening & Capability changelog (v0.0.1 → v0.2.12)

This file summarizes everything that changed across the hardening session that took `k8s-mcp-2-0-server` from the original pre-audit build (v0.0.1: `shell=True` kubectl, `cluster-admin`, unauthenticated endpoint) to v0.2.12 — deployed, verified live, and in daily use from DSH. The chart line has since moved on (current: `helm/` 0.3.1 + `helm-customer/` 0.3.1-customer, see `helm/Chart.yaml`); the invariants below still hold.

---

## 1. Security audit — the original attack surface, closed

| # | Vulnerability (v0.0.1) | Fix |
| --- | --- | --- |
| 1 | **Shell injection** — `subprocess.run(f"kubectl {command}", shell=True)`; the write-verb *denylist* checked only the first word, so `get pods && kubectl delete ns foo`, `;`, `` ` ``, `$()`, `>`, newlines all executed | kubectl is invoked as an **argv list** via `asyncio.create_subprocess_exec` — never a shell. Metacharacters arrive as inert tokens (verified: `get pods; touch /tmp/pwned` never executes) |
| 2 | **`cluster-admin` ClusterRoleBinding** on a "read-only" server | Custom read-only `ClusterRole` enumerating exactly what the tools need. Secrets and RBAC objects deliberately excluded — `list_secrets` / `get secret` return **403 from the API server**, not from tool formatting |
| 3 | **Secret exfiltration by design** — `list_secrets` hid values client-side while the escape hatch allowed `get secret -o yaml` | Closed at the RBAC layer (no secret verbs at all) |
| 4 | **Denylist gaps** — `run`, `exec`, `attach`, `cp`, `port-forward`, `proxy`, `debug`, `set`, `autoscale`, `certificate`, `plugin` all permitted | Deny-by-default **read-verb allowlist**: `get, describe, logs, top, explain, api-resources, api-versions, cluster-info, version, auth, events` |
| 5 | **Credential-redirection flags** — `--server`, `--token`, `--kubeconfig`, `--insecure-skip-tls-verify`, … could steal the SA token | Rejected outright before execution |
| 6 | **Unvalidated tool parameters** flowed into command strings | Namespace = DNS-label regex; names/types/output strictly validated (`yaml/json/wide/name/jsonpath=/custom-columns=`) |
| 7 | **Predictable `/tmp/kubeconfig`** written by every call (also broke local dev) | `tempfile.mkstemp` mode `0600`, created only when actually in-cluster |
| 8 | **Container ran as root, writable rootfs, unpinned kubectl** | Non-root uid/gid 10001, read-only root filesystem, all capabilities dropped, RuntimeDefault seccomp, `/tmp` as emptyDir; kubectl **pinned (`v1.37.0`) + sha256-verified** at build; `TARGETARCH`-aware downloads |
| 9 | **Unauthenticated HTTP endpoint** | API-key auth — see §3 |

## 2. MCP 2.0 conformance (protocol `2026-07-28`)

- Stateless operation verified live against the real app: stateless `tools/list`, `tools/call`, `server/discover` (`supportedVersions=["2026-07-28"]`), legacy `2025-03-26` handshake honored on the same process, strict per-request envelope rejection (`-32602` naming the missing key), standard `-32602` for missing resources.
- **Cacheable list results enabled**: `tools/list` carries `ttlMs=300000, cacheScope=public` via `MCPServer(cache_hints=…)` — safe because the tool list is static and identical for every caller.
- Header-based routing (`Mcp-Method`/`Mcp-Name`) validated against the body.

## 3. Authentication & multi-user support

- **API-key auth middleware** (pure ASGI, zero extra dependencies): every request must present `Authorization: Bearer <key>` or `X-API-Key: <key>`; constant-time comparison; 401 before the MCP app is reached. Auth disabled only when no key is configured (local dev) — with a loud startup warning.
- **The key is out-of-band by design**: managed exclusively via `kubectl create secret …` — deliberately *removed* from the envsubst manifest after an unset `API_KEY` silently emptied the live Secret (incident, see §7). The Deployment now **fails loud** (`CreateContainerConfigError`) until the Secret exists.
- **Per-user keys with per-user exec assignments** (`K8S_MCP_CLIENTS`): `name:key[:exec-ns-patterns];…` — capabilities travel with the credential; users cannot widen their own scope. The shared key keeps working alongside (deployment-wide ceiling).
- **`X-Exec-Namespaces` request header**: narrows a request's exec scope (intersected with the key's assignment); can never widen. Malformed patterns → 400.
- Every exec attempt is audit-logged with the calling client's name.

## 4. Namespace governance

- `K8S_MCP_ALLOWED_NAMESPACES` / `K8S_MCP_BLOCKED_NAMESPACES` (comma-separated fnmatch globs; blacklist always wins). Fail-loud at startup on malformed values; malformed values at request time degrade to a clean denial.
- Reads: cluster-wide results are **filtered** to the policy; `list_namespaces` shows only policy-visible namespaces.
- kubectl-backed tools: explicit `-n` checked; namespaced queries without `-n` rejected with a hint; `-A`/`--all-namespaces` rewritten per allowed namespace (glob expansion against the live namespace list, cap 20) or rejected under blacklist-only policy.
- Cluster-scoped resources unaffected; `auth can-i` is context-only by design.

## 5. Hardened container exec (`exec_in_pod`) — opt-in

Off unless `K8S_MCP_EXEC_ENABLED=true` (the tool is not even registered). When enabled, eight independent layers:

1. **Opt-in registration** — unlisted tools cannot be called.
2. **RBAC** — `pods/exec` `create` granted only by **namespaced** Rolebindings (never a ClusterRole/ClusterRoleBinding) — the API server is the real boundary.
3. **Per-pod opt-in label** — `k8s-mcp.io/exec: "true"` required (RBAC cannot match labels; the app can). Gate configurable, default on.
4. **Argv-typed command** — `command: list[str]`, one command, passed after kubectl's `--` (flag parsing stops; no shell on either side).
5. **Binary allowlist** — read-only introspection set (`ps, ls, cat, tail, head, grep, df, du, free, ss, netstat, ip, stat, id, …`), basename-matched, extendable via `K8S_MCP_EXEC_ALLOWED_COMMANDS`. `env`/`printenv` (secret dumps) and `curl`/`wget`/`nc` (exfil channels) excluded by default.
6. **Hard denies** — shells, interpreters, `su`/`sudo`, `nsenter`/`unshare`; `find -exec` and `ip netns` argument tokens rejected. Not overridable by config.
7. **Self-pod guard** — exec into the MCP server's own pod refused (credential isolation).
8. **Bounded + audited** — no TTY/stdin, 30 s timeout with race-safe kill/reap, 50k output truncation, `AUDIT exec decision=…` log line for every allow *and* deny.

## 6. Automatic exec RBAC provisioning

- At startup (or `python server.py --provision-rbac`), the server ensures its own `pods/exec` RoleBindings for every namespace on the exec list — glob patterns expanded against the live namespace list, general-policy-blocked and nonexistent namespaces skipped, idempotent (`already present`), foreign bindings never touched.
- **Escalation-proof by construction**: the provisioner permission is `bind` + `get` **resourceNames-scoped** to the single exec-template ClusterRole — it can never reference another role, create or modify role content, or create ClusterRoleBindings.
- **Migration-safe**: bindings whose subject points at a previous ServiceAccount/namespace are **repointed automatically** (the fix that made the dedicated-namespace migration a one-command operation).
- **Degrade, don't die**: any provisioning failure is returned as a log line — it can never crash the server (a v0.1.0 crashloop taught us this the hard way).

## 7. Reliability & performance

- **Event loop never blocks**: kubectl via `asyncio.create_subprocess_exec` + bounded `wait_for`; Kubernetes client calls via `asyncio.to_thread`. A hung kubectl is killed and reaped in bounded time.
- **Concurrent fan-out** (`asyncio.gather`) in `cluster_health` and `list_workloads`.
- `get_custom_resource` uses the dynamic client (no kubectl subprocess) with kubectl fallback only on discovery/schema errors — the previous always-fallback dead code is gone.
- Duplicate `list_namespaces` registration removed; dead client vars removed.
- Output truncation on every kubectl path.

## 8. Deployment experience

- **[`documentation/DEPLOYMENT.md`](DEPLOYMENT.md)** — the deployment runbook: PCAI values-driven install, clients, namespace policy, exec enablement, per-user keys, rotation, troubleshooting.
- **All knobs values-driven in the charts** (`K8S_MCP_*` env vars underneath): chart values are the source of truth; `kubectl set env` still works day-2 and triggers a rollout.
- **`MCP_HOSTNAME` knob** — the public hostname is unique per deployment (two VirtualServices claiming one host split traffic between their backends — observed live between two namespaces) and is passed to the container for host validation.
- **Host-header (DNS-rebinding) protection correctly scoped**: when `MCP_HOSTNAME` is set, the SDK's protection stays **on** with the real hostname allowlisted. The implicit default (loopback-only) silently 421'd every gateway-fronted request that carried a valid key — requests that failed auth never reached it, which is why only real users found it (v0.1.3).
- **The API key Secret is out-of-band**: an envsubst apply can no longer empty it.
- Private ghcr images: `imagePullSecrets` is a values key in both charts.
- `check_rbac` surfaces `kubectl auth can-i` "no" answers (which exit 1) instead of masking them as errors.

## 9. Istio VirtualServices visibility (v0.2.0)

- **New tool `list_virtual_services`** — fully read-only:
  - `namespace` given: one summary line-group per VirtualService (hosts, gateways, http/tls/tcp route entries with weighted destinations, redirects, delegates, fault injection, mirrors, age);
  - `name` + `namespace`: the full VirtualService definition (JSON);
  - no namespace: cluster-wide listing with denied namespaces filtered by the policy.
- Resolves the CRD across Istio API versions (`v1` → `v1beta1` → `v1alpha3`) via the dynamic client and falls back to kubectl on discovery/schema errors (name without a namespace falls back with `--field-selector metadata.name=…`, never `name + -A`).
- **RBAC grant added** to the read-only role: `networking.istio.io` → `virtualservices` (`get, list, watch`) — the deployed role did not cover Istio, so the generic kubectl path returned 403 before this. Minimal by design: gateways / destinationrules are NOT granted; extend the rule consciously if ever needed.
- Bonus fix: `clusterrole` (and other singular cluster-scoped forms) are now recognized by `_CLUSTER_SCOPED_RESOURCES` — `get clusterrole …` no longer requires `-n` under an active namespace policy (it is cluster-scoped; RBAC still enforces the 403).

## 10. Built-in ops console (v0.2.0)

- **Same pod, same endpoint, one auth path**: a static HPE-branded single-page console served by the server at `/ui/` (`/` redirects there). The shell is inert — every data request is a browser `POST /mcp` with the API key through the SAME auth middleware / namespace policy / read-verb allowlist as MCP clients. No second backend, no drift, no CORS.
- **Powerful by construction**: forms are generated from each tool's `inputSchema` — a tool added server-side appears in the console with no UI change. Curated screens (cluster health, pods→logs→exec, workloads, events, services, VirtualServices, ConfigMaps, PVCs, secrets-names-only, CRD lookup, can-I, any-resource, kubectl console) are presets over one universal runner; "Any tool" exposes every registered tool.
- Exec screen only exists when the server registered `exec_in_pod`; its binary dropdown mirrors the server allowlist (server remains the boundary); optional `X-Exec-Namespaces` narrowing header per session (can only narrow).
- Hygiene: API key in memory/sessionStorage (never localStorage), all cluster output rendered as text (never HTML-interpolated), CSP `default-src 'none'` + nosniff + no-referrer on the shell, path-traversal-guarded static handler, `K8S_MCP_CONSOLE_ENABLED=false` removes the shell.
- Speaks the 2026-07-28 envelope (Mcp-Method/Mcp-Name headers + `_meta`) with an automatic legacy-2025 fallback; verified over real HTTP in both eras.

## 11. Helm charts — one trusted, one structurally locked (v0.2.2)

Two charts in the PCAI house style (values-driven naming, `ezua:` integration block with `hpe-ezua/*` vendor labels via a Kyverno pre-install policy, `/mcp`-first VirtualService routing, committed packaged `.tgz`, `.helmignore`d `local/` site values). One image; the difference is what the chart *reads*:

- **`helm/` (k8s-mcp) — trusted operators, fully frontend-configurable**: exec + exec namespaces, namespace policy, per-user clients, `rbac.scope`, `extraResourceGroups`, provisioner auto-RBAC, `ezua` exposure — all values. Site values in the HPE-local `helm/local/` (never ships); secret-free examples in `helm/values-examples/`.
- **`helm-customer/` (k8s-mcp-customer) — structurally locked**: the security keys (exec, namespace policy, per-user clients, RBAC scope and extra groups) **do not exist in its values and its templates never read them** — pasting `exec: {enabled: true}` into the PCAI frontend is inert. There is no `lockdown` flag to flip. The read-only ClusterRole is baked via a chart constant (clamp a tenant by editing the constant + repackaging — a platform action); exec RBAC is never minted, so exec becomes possible only via the deliberate NOTES.txt runbook (`kubectl set env` + manual Role and RoleBinding creation). Site values in `helm-customer/local/`; secret-free examples in `helm-customer/values-examples/`.
- **API key is out-of-band, always** (`apiKey.existingSecret`) — neither chart creates or inlines it (the envsubst empty-Secret incident must not repeat).
- **Wildcard apiGroups are refused at render time in both charts** (`""`/`"*"` in `extraResourceGroups` would re-grant secrets read — enforced, not just documented).
- Container hardening (non-root, read-only rootfs, dropped capabilities, seccomp, `/tmp` emptyDir) is fixed in both charts, not values-overridable.
- Exec RBAC names are env-overridable (`K8S_MCP_EXEC_TEMPLATE_ROLE/_BINDING_NAME`) so multiple releases coexist in one cluster without RoleName collisions.
- History note: v0.2.0/v0.2.1 shipped ONE chart with a values-controlled `lockdown` flag + fail-guards; superseded in v0.2.2 by the two-chart split (the flag was customer-removable, so the guards were advisory).

## 12. Verification

- `test_namespace_policy.py` — **168 self-contained checks**: namespace policy and globs, precedence (blacklist wins), exec command guard (allowlist, hard-denies, token denials), argv building, per-client parsing/auth/narrowing, provisioning (create/keep/patch/skip/refuse/degrade), API-key middleware (401 paths, both headers, dev mode), MCP 2.0 envelope behavior, `check_rbac` answer surfacing, the VirtualServices tool (summary/detail rendering, policy filtering, kubectl fallback argv), and the built-in console (routing, CSP, traversal guards, disable knob). Runs without a cluster (the `kubernetes` package is stubbed when absent).
- `smoke_console.py` — boots the REAL ASGI wiring (uvicorn, kubernetes stubbed) and verifies over HTTP: static shell + CSP, traversal 404s, console toggle, modern `tools/list`/`tools/call` with the 2026-07-28 envelope, legacy SSE fallback, and the 401 gate with the console-shell exemption.
- `audit_forms.py` — schema↔form contract audit: **19 tools / 38 parameters**, every widget output type round-trips to its server schema type (run after ANY change to tool signatures or `ui/app.js` widgets; exec enabled via env before import).
- **Live protocol conformance**: stateless tools/list+call, `server/discover`, cache hints, legacy-era fallback, strict envelope rejection — verified over real HTTP.
- **Live deployment verification** (production cluster): 236 exec RoleBindings provisioned and repointed across a namespace migration; `can-i create pods/exec` yes in kept namespaces / no elsewhere; real `ps` executed inside a running predictor through the full gateway→auth→policy→allowlist→RBAC chain; 401 gate and audit trail confirmed.

For the post-deployment verification checklist against a running instance, see [`VERIFICATION.md`](VERIFICATION.md).

## 13. Version history

| Tag | Server | Highlights |
| --- | --- | --- |
| v0.0.1 | 2.0.0 | Original: shell injection, cluster-admin, no auth |
| v0.1.0 | 2.1.0 | Security audit fixes, API-key auth, namespace governance, hardened exec + auto-RBAC |
| v0.1.1 | 2.1.1 | Crashloop fix (dict bodies, degrade-don't-die provisioning), imagePullSecrets, `check_rbac` truth |
| v0.1.2 | 2.1.2 | `check_rbac` "no" surfacing, provisioner `get` grant, migration repointing, `MCP_HOSTNAME` knob |
| v0.1.3 | 2.1.3 | Host-header protection correctly scoped (the 421 fix), Secret out-of-band, `MCP_HOSTNAME` reaches the container |
| v0.2.0 | 2.2.0 | `list_virtual_services` (read-only Istio visibility + `networking.istio.io` RBAC grant), built-in HPE ops console on the same pod, Helm chart with internal / customer-lockdown profiles, env-overridable exec RBAC names, cluster-scoped singular forms in the kubectl guard |
| v0.2.1 | 2.2.0 | Helm chart conformed to the PCAI house style: `ezua:` integration block (endpoint doubles as MCP_HOSTNAME), `hpe-ezua/*` vendor labels via a Kyverno pre-install policy, values-driven naming (no fullname helpers), explicit `image.tag` in lockstep with appVersion, optional gateway AuthorizationPolicy, `.helmignore` for `helm/local/`, packaged `k8s-mcp-<ver>.tgz` committed alongside the chart |
| v0.2.2 | 2.2.0 | Two-chart split — `helm/` (trusted operators, fully frontend-configurable) + `helm-customer/` (structurally locked: security keys don't exist in values and templates never read them, baked read-only RBAC via chart constant, exec RBAC never minted, day-2 runbook in NOTES); wildcard apiGroups refused at render in both; `lockdown` values flag removed (it was customer-removable, so its guards were advisory) |
| v0.2.3 | 2.2.0 | Console-serving fix, attempt 1: `ui/*` shipped mode-600 in the image (COPY preserves source modes; DSH workspace default) → uid-10001 got PermissionError and `/ui/` returned `{"error": "not found"}`; "fixed" with `COPY --chmod=0644` — which turned out to strip the execute bit from the `ui/` DIRECTORY (see v0.2.4) |
| v0.2.4 | 2.2.0 | Console-serving fix, attempt 2: `--chmod=0644` had left `/app/ui` untraversable (`d?????????` in exec ls); replaced with `COPY ui/ ./ui/` + `RUN chmod -R a+rX /app` (dirs traversable, files readable); console verified live on G2 |
| v0.2.5 | 2.2.0 | Console dropdowns: namespace fields render as real selects populated from the cluster (`list_namespaces`; `(all namespaces)` where optional, combobox fallback if the fetch fails), and `resource_type` renders a grouped object-of-interest select (curated Workloads/Networking/Config/Cluster groups + "Discovered on the cluster" from api-resources — CRDs included — plus a ✎ other type-it escape); pod-row → logs/exec prefills inject missing options |
| v0.2.6 | 2.2.0 | Console form-engine fix: `readForm`/`prefillForm` queried *inside* the matched control (`querySelector` on an `<input>`), so every simple widget read `null` — every Run sent `{}` (namespace "ignored", `pod_name Field required`) or crashed with `el is null`; now reads the matched control directly. Plus pod dropdowns: on Logs/Exec, choosing a namespace fetches its pods (`list_pods`, cached) and swaps pod_name for a select with phase labels and a ✎ type-it escape |
| v0.2.7 | 2.2.0 | Multi-container logs/exec: `get_pod_logs` now parses the API's 400 detail — a multi-container pod (e.g. queue-proxy + model container) returns an actionable "set container to one of: …" message instead of a bare `Bad Request`, and generic 400s carry the API's message; the console auto-fetches the picked pod's spec (`get_resource … output=json`, cached) and renders a container dropdown whenever there are 2+ containers |
| v0.2.9 | 2.2.0 | Searchable comboboxes (ModelDownloader-style): namespace (~230 options), object-of-interest, pod and container fields are type-to-filter combos — the typed term stays as the value (free text always valid), options filter live, Enter picks the top match; namespace commit re-fetches the pod list; replaces the native selects that couldn't scale to big clusters |
| v0.2.10 | 2.2.0 | Console polish: primary button rests in the deep brand green with white text (was the bright tint, which read as light green and flipped dark only after pressing); hover deepens, pressed darkens further, keyboard focus is a bright-green ring |
| v0.2.11 | 2.2.0 | Container-picker fix: the pod-spec fetch used full `output=json`, which truncates at ~50k — a KServe predictor's spec is ~56KB, JSON.parse threw, and the picker silently never appeared; now fetches `jsonpath={.spec.containers[*].name}` (tiny, truncation-proof). Logs/Exec auto-run also awaits the pickers, so the first click already carries the kserve-container default |
| v0.2.12 | 2.2.0 | Logs readability: some kubernetes-client versions return the log subresource as raw BYTES — returned as-is, the MCP layer rendered a Python bytes repr (`b'…\n…'`): every newline escaped, one flat line. `get_pod_logs` now decodes bytes (and defensively un-decodes bytes-reprs via ast.literal_eval). Plus a ⛶ Full screen toggle for the output panel (fixed overlay, Esc/exit button) |

## 14. Files

| File | Purpose |
| --- | --- |
| `server.py` | The server — 19 tools, auth, policy, exec, provisioning, console serving |
| `ui/` | Built-in HPE ops console (static: `index.html`, `style.css`, `app.js`) |
| `helm/` | Trusted-operator chart (k8s-mcp): every security knob values-configurable, `ezua:` exposure, Kyverno vendor labels; packaged `.tgz` at the repo root |
| `helm/values-examples/` | Secret-free paste-ready full-values examples (G2 lab, hosted trial) |
| `helm-customer/` | Locked customer distribution (k8s-mcp-customer): security keys absent from values and unread by templates, baked read-only RBAC, exec RBAC never minted — day-2 via the NOTES.txt kubectl runbook |
| `helm-customer/values-examples/` | Secret-free paste-ready examples for the locked chart |
| `helm/local/` | HPE-local site values — `.helmignore`d, never shipped |
| `helm-customer/local/` | Per-customer site values template — `.helmignore`d, never shipped |
| `k8s-mcp-2-0-server.yaml` | Legacy envsubst deployment manifest — no longer tracked in this repo (charts supersede it; `apply-mcp.sh` still references it) |
| `Dockerfile` | Pinned + checksum-verified kubectl, non-root, arch-aware, console included |
| [`documentation/DEPLOYMENT.md`](DEPLOYMENT.md) | Operator runbook (PCAI values-driven deploy, clients, exec, migration, rotation, troubleshooting) |
| `documentation/FEATURES.md` | This file |
| `documentation/VERIFICATION.md` | Post-deployment verification checklist (MCP handshake, tool test, operator checks) |
| `test_namespace_policy.py` | 168-check verification suite (no cluster required) |
| `smoke_console.py` | HTTP-level console + protocol smoke test (real ASGI wiring, kubernetes stubbed) |
| `audit_forms.py` | Schema↔form audit — boots the server (exec enabled), simulates the console's inferWidget/read matrix for every parameter of all 19 tools, exits non-zero on any widget↔schema type mismatch |
