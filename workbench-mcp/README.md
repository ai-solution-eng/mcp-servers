# Workbench MCP Server

Persistent, per-agent **scratch workspaces** for baseline agent harnesses
(opencode, DSH): the durable layer a stateless shell does not give the model.

**Deploy on PCAI:** import the packaged chart once, then edit the chart's
values in the PCAI **Helm Values** editor and apply — you never run
`helm install`/`kubectl apply` for the deployment (PCAI resolves
`${DOMAIN_NAME}` in ezua values before rendering). The values walkthrough —
required vs optional keys, the API-key Secret, exec governance, proxy/CA,
deployment targets (internal SE-G2 / hosted trial) — lives in
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md); verification in
[documentation/VERIFICATION.md](documentation/VERIFICATION.md); sanitized
paste-ready per-target values in
[helm/values-examples/](helm/values-examples/README.md).

## Why

Harness shells are stateless between calls — platform temp areas may not
survive, env vars vanish, background processes die.  A workbench is a named
directory on a persistent volume plus a governed command runner:

- `workspace_create` / `workspace_list` / `workspace_delete` — PVC-backed
  dirs that survive pod restarts and work across replicas (RWX volume).
  `workspace_create` takes an optional `template` (opt-in, see "Workspace
  templates" below).
- `write_file` / `read_file` / `list_files` / `delete_file` — path-confined
  file access (traversal and symlink escapes refused by design).
- `set_env` / `get_env` — per-workspace env vars that `run_command` injects.
- `run_command` — argv-list execution (no shell) with a binary allowlist /
  denylist, timeouts, output caps, and a JSONL audit log.
- `sandbox_run` (v1, opt-in) — run a python file from the workspace in a
  dedicated no-network, no-credentials, no-GPU executor pod (see "Sandbox
  exec" below) — the D18-legal python path.
- **Workspaces are isolated from each other** (D7): tools and commands
  resolve against the addressed workspace only; `WORKBENCH_SHARED_PATHS`
  is the operator escape hatch.  See "Workspace isolation" below.

## Trust model

The workbench pod is a **scratch pad by design**: it mounts no secrets, runs
non-root (uid 10001), and can only touch one volume.  `run_command` is
intentional RCE on that scratch pad — governed by
`WORKBENCH_EXEC_ALLOWLIST` / `WORKBENCH_EXEC_DENYLIST`
(`curl`, `wget`, `sudo`, `ssh`, ... denied by default), timeouts
(`WORKBENCH_EXEC_TIMEOUT_MAX`, default 600s), output caps, and audit.

## Workspace isolation (decision D7 — default change)

Every operation resolves against the **addressed workspace only**.  Before
D7, `run_command` confined only its cwd: any workspace's commands could read
every other workspace's files and env values (`cat ../other/.workbench-env.json`)
and the fleet-audit JSONL (`cat /data/.audit.jsonl`) — same-uid chmod is
theater when the pod runs uid 10001 everywhere.  Since D7:

- **File and env tools** (`read_file`, `write_file`, `list_files`,
  `delete_file`, `get_env`, `set_env`) touch the addressed workspace only.
  Traversal/symlink escapes were already refused; the error now also names
  the escape hatch below.
- **`run_command` screens its argv**: any path-shaped token that resolves
  into the workbench root but outside the addressed workspace is refused
  with a clear error (and audited as a `run_command_refused` event).  This
  covers direct path arguments, `--opt=path` forms, `../` traversals, bare
  `..`, and absolute/`../` paths embedded inside tokens (e.g. inside a
  `python3 -c` one-liner) — including the audit JSONL and other workspaces'
  `.workbench-env.json` files, in both relative and absolute form.
- **Honest limits**: argv screening is a policy layer at the MCP boundary,
  not a kernel sandbox.  An allow-listed interpreter can still compute paths
  at runtime (`python3` builds `'/data/' + 'other/f'` from string parts) —
  the pod's real defense-in-depth (no secrets mounted, read-only rootfs, no
  SA token, egress governed outside this server) is unchanged.  Screening is
  bypassable in exactly the ways the argv *allowlist* always was.
- **Escape hatch — `WORKBENCH_SHARED_PATHS`**: colon-separated absolute
  paths the operator explicitly shares with ALL workspaces (e.g. a staging
  directory).  Shared paths are readable/writable via commands *and* file
  tools from any workspace; the refusal error message names this variable.
  Listing `WORKBENCH_ROOT` itself would share everything — don't.
- Unchanged on purpose: commands can still read pod paths outside the
  workbench root (`/etc/hosts`, ... — the rootfs is read-only and mounts no
  secrets). `workspace_list` names workspaces without sizes by default now
  (lazy counts; `deep=true` opts into the full scan — see "Lazy workspace
  listing").

### Workspace templates (Wave-5 F3 — ADDITIVE, opt-in)

`workspace_create(name, template=...)` can bootstrap a workspace from a
named **operator-defined template**. Templates live in the
`WORKBENCH_TEMPLATES` env (chart `workbench.templates` → env, rendered ONLY
when non-empty — the default render has no such env at all):

```json
{
  "pytools": {
    "description": "git scratch with a prepared src/ tree",
    "extra_allowed": ["pytest"],
    "canned_setup": [["mkdir", "src"], ["git", "init", "-q"]]
  }
}
```

Creating `workspace_create("ws", template="pytools")` then:

- **widens the exec allowlist for THAT workspace only** — `run_command`
  inside it accepts base-allowlist ∪ `extra_allowed`.  Hard rules: entries
  are operator-supplied bare binary names (they match argv[0] basenames);
  the denylist still wins, so a template can never re-enable a denied
  binary; and the widened set is **re-derived from the current
  `WORKBENCH_TEMPLATES` on every call** — only the template NAME is
  persisted in the workspace (`.workbench-template.json`), so a workspace is
  never wider than what the operator's config defines right now (template
  removed → extras vanish; corrupt metadata → base allowlist).
- **pre-runs `canned_setup`** as argv commands INSIDE the new workspace
  through the exact `run_command` machinery: D7 argv confinement, allowlist
  resolution against the server PATH (PATH-erosion defense), timeouts,
  output caps, and the JSONL audit all apply.  Each setup run is audited as
  a `workspace_template_setup` event carrying the template name (a refused
  one as `workspace_template_setup_error`).  The create response reports
  every setup command (`setup[]` with argv/exit/stdout/stderr, `setup_ok`);
  a failing setup command never fails the create — the workspace exists and
  the failed command can simply be re-run via `run_command`.

Opt-in posture: with `WORKBENCH_TEMPLATES` unset or empty (the default), the
`template` parameter is **refused** with the configured template list, and
every other behavior — response shape, audit events — is byte-identical to
the pre-template server.  An unknown template name errors with the
configured ones.  Honest limits: this is convenience + an operator-gated
allowlist widening, not a sandbox change — the D7 "honest limits" paragraph
above applies unchanged to template setup runs.  One deliberate edge: the
metadata file lives inside the workspace, so a workspace command can edit
it — but the only field consulted is the template NAME, and the widened set
always comes from the operator's `WORKBENCH_TEMPLATES` at call time, so the
worst an agent can do is point its workspace at a *different*
operator-defined template — the same binaries it could get by simply
creating a new workspace with that template.

### PATH-erosion defense (exec allowlist)

The allowlist resolves **against the server's own PATH**, before any
workspace-env PATH is applied: a bare argv[0] is located with the server
PATH and spawned by its resolved absolute path, so a workspace
`PATH=/ws/evil:$PATH` override can never redirect an allow-listed name to a
look-alike binary.  A workspace PATH still flows into the child process's
environment (that is its only power).  A bare name that the server PATH
cannot find errors with a clear message instead of falling through to the
workspace PATH.

### read_file streaming

`read_file` streams only the requested slice (open → read `max_bytes`) —
identical response shape and edge behavior, but memory no longer scales with
file size: a multi-GB file no longer OOMs the pod for a 64 KiB read.  A
negative `max_bytes` is refused (it previously sliced off the file tail,
which required loading the whole file).

### write_file append (chunked writes)

`write_file` gains `append: bool = false`.  `append=true` ADDS to the file
instead of replacing it (creating it on first use) — this is the supported
way to write content larger than `WORKBENCH_MAX_FILE_BYTES` in chunks (the
old over-cap error told agents to "write in chunks" while every write
REPLACED the file, making chunking impossible).  The cap still applies
**cumulatively**: existing size + incoming payload must fit, so an append is
a chunking path, never a cap bypass.  An appending response carries
`"appended": true` and `"total_bytes"` (the file's new size).  The console's
`POST /api/ws/{ws}/file` accepts the same `append` flag.

### workspace_list lazy counts (deep flag)

`workspace_list` gains `deep: bool = false`.  The default is LAZY: workspace
names only, with `files: null` / `bytes: null` and
`"counts": "lazy (pass deep=true)"` — no recursive walk, so the listing
stays O(#workspaces) even on an NFS-backed PVC holding hundreds of thousands
of inodes (counting rglob+stats the ENTIRE tree on every call).  `deep=true`
preserves the exact pre-lazy behavior (per-workspace file count and total
bytes).  Response keys are present in BOTH modes (values may be null).  The
console's `GET /api/workspaces` keeps the deep listing so the UI is
unchanged.

### run_command concurrency bound

`run_command` executes on a DEDICATED pool of `WORKBENCH_EXEC_MAX_CONCURRENCY`
(default 4, env re-read per call) workers, not the default executor — a burst
of long runs (each may run to the 600s timeout) can no longer starve the
file/env tools' offload.  When every worker is busy, the call returns a
structured busy response WITHOUT executing — no queue, no reordering, the
model retries shortly — and the busy outcome is audit-logged
(`run_command_busy`).  File/env/list tools stay on the default executor.

### Audit JSONL rotation

The audit trail (`<root>/.audit.jsonl`) no longer grows forever: when the
active file reaches `WORKBENCH_AUDIT_MAX_BYTES` (default 100 MiB), the next
audited event rotates it — renamed to `.audit.jsonl.1` (single generation;
the previous `.1` is overwritten) and a fresh file starts with an
`audit_rotated` event whose `rotated_from` field carries the sha256 of the
rotated file's last line.  Honest limit: workbench's writer is a plain
best-effort JSONL appender, NOT the fleet's hash-chained shared writer, so
there is no cryptographic chain across generations — `rotated_from` is the
explicit, verifiable bridge (hash the last line of `.1` and compare).  The
console audit tail (`/api/audit`) spans both generations.  The logsearch
mount convention is unchanged: mount the root; search `.audit.jsonl` and
`.audit.jsonl.1`.

### Transport security (Host-header pinning — K8S-MCP fleet pattern)

The MCP endpoint's DNS-rebinding protection now comes from the fleet-shared
`mcp_auth.transport_security_from_env()`.  With `MCP_HOSTNAME` set (chart:
`ezua.virtualService.endpoint`, wired only when `ezua.enabled`), protection
is explicitly ON for that FQDN + loopback, with the https-only browser-form
Origin allowlist; `MCP_EXTRA_ALLOWED_HOSTS` (chart: `extraAllowedHosts`)
adds in-cluster service-DNS hosts for callers like an MCP relay.  The chart
AUTO-prepends this release's own service DNS
(`<deployment.name>-service.<ns>.svc.cluster.local:*`) as the FIRST
`MCP_EXTRA_ALLOWED_HOSTS` entry whenever transport security is active — the
gateway's relay hop arrives with exactly that Host header (confirmed in the
workbench pod logs: "Invalid Host header: workbench-mcp-service.
workbench-mcp.svc.cluster.local:9103"), and without the entry every relay
hop gets 421 Misdirected Request — so sites list only EXTRA hosts in
`extraAllowedHosts` (setting it never drops the own-svc entry).  With
neither set (local dev), the helper returns None and the SDK's implicit
default applies — on a loopback-bound dev server that is the SDK's
loopback-only auto-protection, so practical dev behavior is unchanged.

### API-key auth — MANDATORY (fleet pattern)

`run_command` is arbitrary process execution, so **every HTTP route except
`/health`/`/healthz` requires an API key** (`X-API-Key` or
`Authorization: Bearer`; the bundled console asks for it once and stores it
in sessionStorage). **The chart never creates the key Secret — you MUST
pre-deploy it in the target namespace before `helm install`, or the pod
sits in `CreateContainerConfigError`:**

```bash
kubectl -n <ns> create secret generic workbench-mcp-apikey \
  --from-literal="api-keys=$(openssl rand -hex 32)"
```

Keys are a comma-separated list (`api-keys=new,old`) — that is the rotation
mechanism: append the new key, move clients over, drop the old; the server
re-reads the env per request, so no restart is needed. The fleet-universal
`MCP_API_KEYS` env is honored too (either var works).

## Exec policy — no python by default (fleet decision D18, 2026-09-13)

The DEFAULT allowlist is narrow argv tools only (ls, cat, grep, find, tar, git,
sort, uniq — no shells by design). **No interpreters (python3) and no package
managers (pip/pip3)** ship in the default: an allow-listed interpreter can
compute paths at runtime and sidestep the argv-level workspace confinement
(a split-string path never appears in argv — the documented honest limit
below), and pip executes python code (setup.py). The refusal message and
`run_command`'s tool description tell the model this, so it reaches for the
specific argv tools instead. Operators re-add interpreters explicitly via
`WORKBENCH_EXEC_ALLOWLIST` / `values.execAllowlist` (or a template's
`extra_allowed`) when their threat model accepts the documented residual.

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `WORKBENCH_ROOT` | `/data` | PVC mount holding all workspaces |
| `WORKBENCH_SHARED_PATHS` | — | colon-separated absolute paths shared across ALL workspaces (D7 escape hatch; see "Workspace isolation") |
| `WORKBENCH_EXEC_ALLOWLIST` | ls, cat, head, tail, grep, find, wc, du, df, mkdir, touch, cp, mv, tar, git, diff, sort, uniq *(no interpreters — D18; operators re-add explicitly)* | argv[0] allowlist (resolved against the server PATH, not a workspace PATH) |
| `WORKBENCH_EXEC_DENYLIST` | curl,wget,sudo,su,nc,ncat,ssh,scp,setsid | hard deny (wins) |
| `WORKBENCH_EXEC_TIMEOUT_DEFAULT/MAX` | 60 / 600 s | per-command bounds |
| `WORKBENCH_MAX_FILE_BYTES` | 8 MiB | write cap — enforced on EVERY `write_file` payload AND cumulatively on `append=true` (existing size + incoming payload ≤ cap), so chunked appends can never bypass it |
| `WORKBENCH_MAX_OUTPUT_BYTES` | 200 KiB | stdout/stderr cap |
| `WORKBENCH_EXEC_MAX_CONCURRENCY` | 4 | worker width of the dedicated `run_command` pool (env re-read per call; a changed width rebuilds the pool). `run_command` jobs can run to the 600s timeout, so they get their own pool instead of the default executor — file/env tools are never starved by long runs. When all workers are busy, `run_command` returns `{"ok": false, "busy": true, "running": N, "hint": "retry shortly"}` WITHOUT executing (no queue, no reordering) and the busy outcome is audit-logged (`run_command_busy`). |
| `WORKBENCH_AUDIT_MAX_BYTES` | 100 MiB | rotation threshold for the audit JSONL (`<root>/.audit.jsonl`): when the active file is at/over it, the next audited event first renames it to `.audit.jsonl.1` (single generation — the previous `.1` is overwritten, no `.2` ever appears) and starts a fresh file whose first entry is an `audit_rotated` event carrying `rotated_from` (sha256 of the rotated file's last line). `rotated_from` lets a reader bridge generations explicitly: workbench's audit writer is a plain JSONL appender (NOT the hash-chained shared writer), so there is no cryptographic chain ACROSS generations — the break is by design and documented here. The active path stays `.audit.jsonl` (D7 argv tests and the logsearch-mount convention unchanged: mount the root; `.audit.jsonl` AND `.audit.jsonl.1` are searchable). |
| `WORKBENCH_UI_ENABLED` | `true` | mount the web UI + `/api/*` routes |
| `WORKBENCH_TEMPLATES` | *(unset)* | JSON object of named workspace templates for `workspace_create(name, template=...)` — `{name: {description, extra_allowed, canned_setup}}`. **Opt-in**: unset/empty = the parameter is refused and behavior is byte-identical to the pre-template server. A template can only WIDEN the workspace's exec allowlist with these operator-supplied bare binary names (denylist still wins) and pre-run its canned setup argv commands inside the new workspace (same guards, audited with the template name). Chart: `workbench.templates` (rendered only when non-empty). See "Workspace templates". |
| `WORKBENCH_METRICS_ENABLED` | `false` | serve `GET /metrics` — Prometheus self-metrics: `workbench_mcp_tool_requests_total{tool,outcome}` (per-tool call counts, ok/error; nothing else is exported). Chart-gated default-OFF (`metrics.enabled: false` renders no env and no ServiceMonitor — the default pod has no `/metrics` route); when on, `/metrics` is key-free like the probes. See `helm/values.yaml` (which also documents the fleet audit-JSONL searchability convention for `<root>/.audit.jsonl`). |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` | — | passed through to `run_command` children (chart: `proxy.http/https/noProxy` — each key wired only when non-empty) so `pip` works behind a corporate proxy |
| `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`PIP_CERT` | — | corporate MITM CA for pip/requests (chart: `caCert` → `ezaf-root-ca`) |
| `MCP_HOSTNAME` | *(unset)* | pinned public FQDN for the MCP SDK's DNS-rebinding (Host-header) protection — fleet-shared `mcp_auth.transport_security_from_env()`, the K8S-MCP pattern. With NEITHER this nor `MCP_EXTRA_ALLOWED_HOSTS` set (local dev), the helper returns None and the SDK's implicit default applies (loopback auto-protection on a loopback bind — dev behavior unchanged). Chart: `ezua.virtualService.endpoint`, wired ONLY when `ezua.enabled` — see "Transport security (Host-header pinning)". |
| `MCP_EXTRA_ALLOWED_HOSTS` | *(unset)* | comma-separated extra Host values the DNS-rebinding protection accepts for in-cluster callers addressing the server by service DNS (`http://<name>-service.<ns>.svc.cluster.local:9103/mcp`); entries match verbatim or as `host:*` (any port). When transport security is active, the chart AUTO-prepends the release's own service DNS (`workbench-mcp-service.<ns>.svc.cluster.local:*`) — the gateway relay hop's Host header (421 Misdirected Request without it) — so chart key `extraAllowedHosts` lists only EXTRA hosts. |
| `WORKBENCH_SANDBOX_EXEC` | `false` | master switch of the `sandbox_run` tool (see "Sandbox exec"): when off, the tool refuses self-describingly ("sandbox exec is not enabled (WORKBENCH_SANDBOX_EXEC) — see chart values executors.enabled"). Chart: `workbench.sandboxExec` (rendered `"1"` ONLY when true — the default render carries no sandbox env at all). |
| `WORKBENCH_SANDBOX_LABEL` | `app=workbench-exec` | pod label selector `sandbox_run` balances over (Ready pods only, least-loaded pick). Must match the chart's `executors.appName` if you rename it. |
| `WORKBENCH_SANDBOX_CONTAINER` | `python` | exec target container inside each executor pod. Must match the chart's `executors.containerName`. |
| `WORKBENCH_SANDBOX_TIMEOUT_S` | `120` | per-run wall clock (clamped 1..600); also drives the exec connect/read timeouts. On expiry a structured `{"ok": false, "timeout": true, "timeout_s": N, ...}` result returns (with any partial output) — the stateless executor pod is unharmed. |
| `WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS` | `50000` | stdout/stderr cap per run (the fleet k8s-mcp exec convention); each stream is truncated with an explicit ` ...[truncated N chars]` marker. |
| `WORKBENCH_SANDBOX_ARGV_MAX_BYTES` | `8192` | sandbox_run total argv bytes per exec (fleet k8s-mcp screen discipline); larger argument payloads belong in the script file |

### Migrating from hpe_proxies

The `hpe_proxies` boolean flag is removed. Configure `proxy.http`,
`proxy.https` and `proxy.noProxy` directly — a key is active only when
non-empty, and `proxy: {}` (or omitting the block entirely) means fully off.
A site that previously relied on `hpe_proxies: true` + the chart's built-in
proxy defaults now needs an explicit `proxy` block with its real addresses.
`caCert` is unaffected: it is independent of the proxy wiring (enable it
whenever workloads must trust a corporate MITM CA).

### Helm chart — standard Kubernetes knobs

Boilerplate workload values in `helm/values.yaml` — every path is settable in
the PCAI **Helm Values** editor; defaults are sensible, so none normally need
touching. (Behavior-bearing chart keys — `workbench.*`, `persistence.*`,
`apiKey.*`, `webui.enabled`, `metrics.*`, `caCert.*`, `ezua.*` — are covered
above and in `documentation/DEPLOYMENT.md`.)

| Key | Default | Effect |
|---|---|---|
| `deployment.appName` | `workbench-mcp` | Kubernetes object + pod/container name and label. |
| `image.tag` | `v0.2.1` | Image tag — kept in lockstep with `Chart.yaml` `appVersion`; a stale tag in a site values file is how an "old MCP server" pod happens. |
| `resources.requests.cpu` / `resources.limits.cpu` | `250m` / `1` | Container CPU requests/limits (memory: `256Mi` / `512Mi`). |
| `securityContext.runAsNonRoot` / `securityContext.runAsUser` | `true` / `10001` | Pod-level non-root posture; uid 10001 matches the image user, and `fsGroup: 10001` keeps the mounted PVC writable. |
| `containerSecurityContext.readOnlyRootFilesystem` | `true` | Root filesystem is read-only — `run_command` children can write only the PVC and `/tmp` (fleet-audit P0). |
| `containerSecurityContext.allowPrivilegeEscalation` | `false` | Privilege escalation disabled; all capabilities dropped (fleet-audit P0). |
| `executors.enabled` | `false` | Sandbox-exec v1 (see "Sandbox exec" below): renders the `workbench-exec` Deployment (same image, sleep command), its deny-all NetworkPolicy, and the same-namespace pods/pods-exec Role+RoleBinding. Default OFF — the default chart render is byte-identical to the baseline. |

## Sandbox exec (v1, opt-in)

`run_command` deliberately ships NO interpreter (D18): an allow-listed
interpreter inside the workbench pod could compute paths at runtime and
sidestep the argv-level workspace confinement. Agents still need python, so
v1 adds `sandbox_run(workspace, path, argv=[])` — it runs a python FILE from
the addressed workspace in a **dedicated executor pool** instead:

- **The pool**: a separate always-running Deployment (`workbench-exec`) in
  the SAME namespace, running the SAME container image as the workbench
  server (values `executors.image` defaults to the workbench image reference
  — one image, different command; nothing new is built) with the command
  `python3 -c "import time; time.sleep(2147483647)"` (no shell). Replicas
  default 2 (`executors.replicas`); an HPA is available behind
  `executors.autoscaling.enabled` (default off; min 2 / max 4 / CPU 70%).
- **The dispatch**: workbench execs into the least-loaded Ready executor via
  the Kubernetes exec subresource and pipes the script bytes to
  `python3 -I -- *argv` stdin (`-I` = isolated mode: no user site-packages,
  PYTHONPATH ignored). No filesystem mounts, no network, stateless runs.
- **Executor hardening** (all test-asserted): `automountServiceAccountToken:
  false` (zero credentials), `runAsNonRoot` uid 10001, no privilege
  escalation, ALL capabilities dropped, seccomp `RuntimeDefault`,
  read-only rootfs + per-pod emptyDir `/tmp`, modest resources (100m/128Mi →
  1 CPU/512Mi), **no tolerations block at all** (control-plane NoSchedule
  taints self-exclude the pool), a `node-role.kubernetes.io/control-plane:
  DoesNotExist` nodeAffinity (workers only, without knowing the worker
  label), a hostname topologySpread (maxSkew 1, ScheduleAnyway), and **no
  GPU resource keys anywhere** (no `nvidia.com/gpu` request = structurally
  no GPU access).
- **The network**: a NetworkPolicy selects the executors with
  `policyTypes: [Ingress, Egress]` and ZERO rules — deny-all both
  directions. Exec needs no network: workbench → API server → kubelet →
  container runtime, and the result rides the same connection back.
- **Response**: `{"ok": true, "exit_code": <int|null>, "stdout", "stderr",
  "truncated", "pod", "elapsed_s"}` — stdout/stderr capped at
  `WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS` with an explicit
  ` ...[truncated N chars]` marker. `exit_code` is null (recorded honestly)
  when the exec protocol yields no terminal status. Timeout → a structured
  `{"ok": false, "timeout": true, ...}` result with partial output; the
  stateless pod is unharmed. All executors busy → `{"ok": false, "busy":
  true, "running": N, "hint": "retry shortly"}` without executing; zero
  Ready pods → a self-describing error naming `executors.enabled`. Every
  outcome is audit-logged (`sandbox_run` / `sandbox_run_busy`).

**Honest limits**: the isolation tier is runc-tier — the container boundary
plus no-network (deny-all netpol) + no-credentials (no SA token) + no-GPU
(no request) + non-root + read-only rootfs. It is **NOT gVisor** (or any
VM-level sandbox); code that escapes a plain container could escape this —
run only code you would run in a container you own. v1 runs a **single
file** with argv; results come back via **stdout/stderr only**; runs are
**stateless** (nothing persists in the executor between runs).

**Trust posture** (the one change): enabling this grants the workbench
service account `get`/`list` on pods and `create` on `pods/exec` in its OWN
namespace (`helm/templates/executors.yaml`, gated by `executors.enabled`).
RBAC cannot select pods by label, so this permits exec into ANY pod in the
workbench namespace — which holds only workbench + executor pods by design,
and the workbench SA has no cluster-wide powers and holds no Secrets. The
blast radius is exactly one namespace. If the Role is absent (a cluster
where the chart could not create RBAC), the exec 403s and the tool surfaces
a self-describing RBAC error including the
`kubectl auth can-i create pods/exec` recipe — a "no" is a result.

**Enable recipe** (both switches — the pool AND the tool):

```yaml
# chart values
executors:
  enabled: true        # renders the pool + netpol + Role/RoleBinding
workbench:
  sandboxExec: true    # renders WORKBENCH_SANDBOX_EXEC=1 (the tool switch)
```

Tuning: `executors.replicas` / `executors.autoscaling.*` (pool size),
`executors.appName` + `WORKBENCH_SANDBOX_LABEL` (keep in sync if renaming),
`executors.containerName` + `WORKBENCH_SANDBOX_CONTAINER`,
`WORKBENCH_SANDBOX_TIMEOUT_S` (1..600), `WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS`.

## Web UI

With `WORKBENCH_UI_ENABLED` (chart `webui.enabled`, default true) the same
container serves a self-contained HPE-branded console at `/` (no CDN, no
build step — corporate-proxy safe): workspace switcher, file tree +
viewer/saver, per-workspace env table editor, a run-command console
(argv input with the allowlist hint, stdout/stderr/exit/duration), and an
audit tail. The `/api/*` endpoints call the **same core functions the MCP
tools use** — path confinement, caps, the argv allowlist and the JSONL
audit all still apply; the UI gets no new powers. Auth posture (fleet
K8S-MCP-console pattern): the console HTML at `/` and `/ui` is
**public-but-inert** — an unlock bar in the page collects the key, because
the browser cannot load the page behind a 401 — while every `/api/*` data
route (and `/mcp`) stays API-key-gated at the pod; the endpoint must also
sit behind gateway authn (PCAI Istio gateway).

## Run

```sh
# stdio (local MCP clients)
python server.py --transport stdio

# stateless streamable-http (fleet default), port 9103
python server.py --transport streamable-http --host 0.0.0.0 --port 9103
```

Health: `GET /health` / `/healthz`.

## Tests

```sh
python -m pytest tests/ -v     # offline: tmp dirs only, no cluster, no network
```

## Enabling the network zone

The chart ships an optional ingress NetworkPolicy (`networkPolicy.enabled`,
default **false** — the default render is byte-identical to the baseline).
When on, only the allowlisted callers reach the MCP: the authorized client
namespaces (plus same-namespace pods, the node IPs in `probeCidrs` for kubelet
probes, and — only if you flip `allowEzafGatewayIngress` — the gateway pods).
Six steps: (1) `kubectl get ns` to find your callers' namespaces; (2) append
any extra caller namespaces to `networkPolicy.authorizedClients.namespaces`
(keep `monitoring` — Prometheus scrapes `/metrics` on the same port, and an
unlisted scrape namespace dies **silently**); (3) put your cluster's pod CIDR
into `networkPolicy.probeExceptCidrs` (or real node IPs in `probeCidrs`);
(4) the browser path stays OPEN by fleet doctrine — do NOT set
`ezua.virtualService.enabled: false` (closing a console is an explicit
per-chart, per-site decision, never a default);
(5) apply via the PCAI values editor (`helm upgrade` for operators); (6)
verify — an allowed namespace gets HTTP 200 from `service:port/mcp`, any
other namespace times out. See `values-examples/values-hardened-g2.yaml`
for the full hardened profile.
