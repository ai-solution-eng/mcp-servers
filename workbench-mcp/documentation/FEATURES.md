# Features — workbench-mcp

Every capability this server has, with the design decision behind it.
Written for a reader who has never heard of workbench-mcp and only knows
that PCAI runs LLM applications. Start here if the README's one-paragraph
summary is not enough.

**MCP** — Model Context Protocol: an endpoint that exposes *tools* (typed
functions) to LLM agents over HTTP, so a model can call them the way it
would call an API. This server is one member of the PCAI MCP fleet; this
one owns **persistent scratch workspaces and a governed command runner**.

---

## The niche: the durable layer a stateless shell does not give the model

Harness shells are stateless between calls — platform temp areas may not
survive, env vars vanish, background processes die. A **workbench** is a
named directory on a persistent volume plus a governed command runner. The
design tension it lives in: the model needs a real filesystem and real
processes to be useful, and everything it touches must stay inside a
scratch pad that mounts no secrets and can be audited. Every feature below
is one resolution of that tension.

**Trust model up front:** the workbench pod is a **scratch pad by design**
— it mounts no secrets, runs non-root (uid 10001), has a read-only rootfs,
automounts no service-account token, and can only touch one volume.
`run_command` is intentional RCE on that scratch pad — governed by the
exec policy below.

---

## Tool catalog

| Tool | What it does | Why it is shaped this way |
|---|---|---|
| `workspace_create` / `workspace_list` / `workspace_delete` | PVC-backed directories that survive pod restarts and work across replicas (RWX volume). `create` takes an optional `template` (opt-in — see [Workspace templates](#workspace-templates-wave-5-f3--opt-in)). | A workspace is the unit of isolation: everything else is scoped to one. |
| `write_file` / `read_file` / `list_files` / `delete_file` | Path-confined file access (traversal and symlink escapes refused by design). | Files are the durable artifact; confinement is what makes an RCE-adjacent server acceptable. |
| `set_env` / `get_env` | Per-workspace env vars that `run_command` injects. | The workspace, not the pod, is the secret-free config scope. |
| `run_command` | argv-list execution (**no shell**) with a binary allowlist/denylist, timeouts, output caps, and a JSONL audit log. Commands run with cwd = the workspace root and a child env that names the addressed workspace (`WORKBENCH_WORKSPACE=<name>`) plus the operator's proxy/TLS passthrough — never the server's own credentials. | The one dangerous tool; every guard in this document exists because of it. |
| `sandbox_run` (v1, opt-in) | Run a python file from the workspace in a dedicated no-network, no-credentials, no-GPU executor pod. | The D18-legal python path — see [Sandbox exec](#sandbox-exec-v1-opt-in). |

---

## Workspace isolation (fleet decision D7 — default change)

Every operation resolves against the **addressed workspace only**. Before
D7, `run_command` confined only its cwd: any workspace's commands could
read every other workspace's files and env values
(`cat ../other/.workbench-env.json`) and the fleet-audit JSONL
(`cat /data/.audit.jsonl`) — same-uid chmod is theater when the pod runs
uid 10001 everywhere. Since D7:

- **File and env tools** (`read_file`, `write_file`, `list_files`,
  `delete_file`, `get_env`, `set_env`) touch the addressed workspace only.
  Traversal/symlink escapes were already refused; the error now also names
  the escape hatch below.
- **`run_command` screens its argv**: any path-shaped token that resolves
  into the workbench root but outside the addressed workspace is refused
  with a clear error (and audited as a `run_command_refused` event). This
  covers direct path arguments, `--opt=path` forms, `../` traversals, bare
  `..`, and absolute/`../` paths embedded inside tokens (e.g. inside a
  `python3 -c` one-liner) — including the audit JSONL and other workspaces'
  `.workbench-env.json` files, in both relative and absolute form.
- **Honest limits**: argv screening is a policy layer at the MCP boundary,
  not a kernel sandbox. An allow-listed interpreter can still compute paths
  at runtime (`python3` builds `'/data/' + 'other/f'` from string parts) —
  the pod's real defense-in-depth (no secrets mounted, read-only rootfs, no
  SA token, egress governed outside this server) is unchanged. Screening is
  bypassable in exactly the ways the argv *allowlist* always was.
- **Escape hatch — `WORKBENCH_SHARED_PATHS`**: colon-separated absolute
  paths the operator explicitly shares with ALL workspaces (e.g. a staging
  directory — the fleet convention points logsearch's
  `logsearch.exportRoot` here so agents can read exports back). Shared
  paths are readable/writable via commands *and* file tools from any
  workspace; the refusal error message names this variable. Listing
  `WORKBENCH_ROOT` itself would share everything — don't.
- **Unchanged on purpose**: commands can still read pod paths outside the
  workbench root (`/etc/hosts`, ... — the rootfs is read-only and mounts no
  secrets). `workspace_list` names workspaces without sizes by default
  (lazy counts; `deep=true` opts into the full scan — see
  [Lazy workspace listing](#lazy-workspace-listing-deep-flag)).

---

## Exec policy — argv allowlist, no interpreters by default (fleet decision D18)

**The default allowlist is narrow argv tools only** (`ls, cat, head, tail,
grep, find, wc, du, df, mkdir, touch, cp, mv, tar, git, diff, sort, uniq` —
no shells by design). **No interpreters (`python3`) and no package managers
(`pip`/`pip3`) ship in the default**: an allow-listed interpreter can
compute paths at runtime and sidestep the argv-level workspace confinement
(a split-string path never appears in argv — the honest limit above), and
pip executes python code (setup.py). The refusal message and
`run_command`'s tool description tell the model this, so it reaches for the
specific argv tools instead. Operators re-add interpreters explicitly via
`WORKBENCH_EXEC_ALLOWLIST` / values `workbench.execAllowlist` (or a
template's `extra_allowed`) when their threat model accepts the documented
residual.

**The hard denylist wins over the allowlist**
(`curl,wget,sudo,su,nc,ncat,ssh,scp,setsid` by default): a template or a
values edit can never re-enable a denied binary.

**PATH-erosion defense**: the allowlist resolves **against the server's own
PATH**, before any workspace-env PATH is applied — a bare argv[0] is
located with the server PATH and spawned by its resolved absolute path, so
a workspace `PATH=/ws/evil:$PATH` override can never redirect an
allow-listed name to a look-alike binary. A workspace PATH still flows into
the child process's environment (that is its only power). A bare name the
server PATH cannot find errors with a clear message instead of falling
through to the workspace PATH.

**Per-command bounds**: `WORKBENCH_EXEC_TIMEOUT_DEFAULT`/`MAX` (60 / 600 s
— per-call `timeout_s` is clamped into these) and
`WORKBENCH_MAX_OUTPUT_BYTES` (200 KiB stdout/stderr cap).

**Philosophy: make the dangerous tool narrow, and make the narrowness
legible.** The model is told what is allowed and why, in the refusal, at
the moment it matters.

---

## run_command concurrency bound

`run_command` executes on a DEDICATED pool of
`WORKBENCH_EXEC_MAX_CONCURRENCY` (default 4, env re-read per call) workers,
not the default executor — a burst of long runs (each may run to the 600 s
timeout) can no longer starve the file/env tools' offload. When every
worker is busy, the call returns a structured busy response WITHOUT
executing — no queue, no reordering, the model retries shortly —
`{"ok": false, "busy": true, "running": N, "hint": "retry shortly"}` —
and the busy outcome is audit-logged (`run_command_busy`). File/env/list
tools stay on the default executor.

**Philosophy: fail fast and honestly instead of queueing silently.** A
queued 600 s job behind four others looks like a hung tool; a busy result
looks like a busy result.

---

## File tools: streaming reads, chunked appends

- **`read_file` streaming** — streams only the requested slice (open →
  read `max_bytes`); memory no longer scales with file size: a multi-GB
  file no longer OOMs the pod for a 64 KiB read. A negative `max_bytes` is
  refused (it previously sliced off the file tail, which required loading
  the whole file).
- **`write_file` append** — `append: bool = false`. `append=true` ADDS to
  the file instead of replacing it (creating it on first use) — the
  supported way to write content larger than `WORKBENCH_MAX_FILE_BYTES`
  (8 MiB) in chunks (the old over-cap error told agents to "write in
  chunks" while every write REPLACED the file, making chunking impossible).
  The cap still applies **cumulatively**: existing size + incoming payload
  must fit, so an append is a chunking path, never a cap bypass. An
  appending response carries `"appended": true` and `"total_bytes"`. The
  console's `POST /api/ws/{ws}/file` accepts the same flag.
- **`list_files`** caps entries per response (`workbench.maxListEntries`,
  default 500).

---

## Workspace templates (Wave-5 F3 — opt-in)

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
  inside it accepts base-allowlist ∪ `extra_allowed`. Hard rules: entries
  are operator-supplied bare binary names (they match argv[0] basenames);
  the denylist still wins, so a template can never re-enable a denied
  binary; and the widened set is **re-derived from the current
  `WORKBENCH_TEMPLATES` on every call** — only the template NAME is
  persisted in the workspace (`.workbench-template.json`), so a workspace is
  never wider than what the operator's config defines right now (template
  removed → extras vanish; corrupt metadata → base allowlist).
- **pre-runs `canned_setup`** as argv commands INSIDE the new workspace
  through the exact `run_command` machinery: D7 argv confinement,
  allowlist resolution against the server PATH, timeouts, output caps, and
  the JSONL audit all apply. Each setup run is audited as a
  `workspace_template_setup` event carrying the template name (a refused
  one as `workspace_template_setup_error`). The create response reports
  every setup command (`setup[]` with argv/exit/stdout/stderr, `setup_ok`);
  a failing setup command never fails the create — the workspace exists and
  the failed command can simply be re-run via `run_command`.

Opt-in posture: with `WORKBENCH_TEMPLATES` unset or empty (the default),
the `template` parameter is **refused** with the configured template list,
and every other behavior — response shape, audit events — is
byte-identical to the pre-template server. An unknown template name errors
with the configured ones. One deliberate edge: the metadata file lives
inside the workspace, so a workspace command can edit it — but the only
field consulted is the template NAME, and the widened set always comes from
the operator's `WORKBENCH_TEMPLATES` at call time, so the worst an agent
can do is point its workspace at a *different* operator-defined template —
the same binaries it could get by simply creating a new workspace with that
template.

---

## Sandbox exec (v1, opt-in)

`run_command` deliberately ships NO interpreter (D18). Agents still need
python, so v1 adds `sandbox_run(workspace, path, argv=[])` — it runs a
python FILE from the addressed workspace in a **dedicated executor pool**
instead:

- **The pool**: a separate always-running Deployment (`workbench-exec`) in
  the SAME namespace, running the SAME container image as the workbench
  server (values `executors.image` defaults to the workbench image
  reference — one image, different command; nothing new is built) with the
  command `python3 -c "import time; time.sleep(2147483647)"` (no shell).
  Replicas default 2 (`executors.replicas`); an HPA is available behind
  `executors.autoscaling.enabled` (default off; min 2 / max 4 / CPU 70%).
- **The dispatch**: workbench execs into the least-loaded Ready executor
  via the Kubernetes exec subresource and pipes the script bytes to
  `python3 -I -- *argv` stdin (`-I` = isolated mode: no user site-packages,
  PYTHONPATH ignored). No filesystem mounts, no network, stateless runs.
- **Executor hardening** (all test-asserted): `automountServiceAccountToken:
  false` (zero credentials), `runAsNonRoot` uid 10001, no privilege
  escalation, ALL capabilities dropped, seccomp `RuntimeDefault`, read-only
  rootfs + per-pod emptyDir `/tmp`, modest resources (100m/128Mi →
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
  `WORKBENCH_SANDBOX_MAX_OUTPUT_CHARS` (default 50000) with an explicit
  ` ...[truncated N chars]` marker. `exit_code` is null (recorded honestly)
  when the exec protocol yields no terminal status. Timeout → a structured
  `{"ok": false, "timeout": true, ...}` result with partial output (per-run
  wall clock `WORKBENCH_SANDBOX_TIMEOUT_S`, clamped 1..600); the stateless
  pod is unharmed. All executors busy → the busy shape without executing;
  zero Ready pods → a self-describing error naming `executors.enabled`.
  Every outcome is audit-logged (`sandbox_run` / `sandbox_run_busy`).
  Total argv bytes per exec cap: `WORKBENCH_SANDBOX_ARGV_MAX_BYTES` (8192) —
  larger argument payloads belong in the script file.

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
executors:
  enabled: true        # renders the pool + netpol + Role/RoleBinding
workbench:
  sandboxExec: true    # renders WORKBENCH_SANDBOX_EXEC=1 (the tool switch)
```

Tuning: `executors.replicas` / `executors.autoscaling.*` (pool size),
`executors.appName` + `WORKBENCH_SANDBOX_LABEL` (keep in sync if renaming —
default `app=workbench-exec`), `executors.containerName` +
`WORKBENCH_SANDBOX_CONTAINER` (default `python`).

---

## Audit JSONL and its rotation

Every audited event appends one JSON line to `<root>/.audit.jsonl` — file
writes, env edits, command runs (and refusals: `run_command_refused`,
`run_command_busy`), template setup, sandbox runs. The trail no longer
grows forever: when the active file reaches `WORKBENCH_AUDIT_MAX_BYTES`
(default 100 MiB), the next audited event rotates it — renamed to
`.audit.jsonl.1` (single generation; the previous `.1` is overwritten) and
a fresh file starts with an `audit_rotated` event whose `rotated_from`
field carries the sha256 of the rotated file's last line. Honest limit:
workbench's writer is a plain best-effort JSONL appender, NOT the fleet's
hash-chained shared writer, so there is no cryptographic chain across
generations — `rotated_from` is the explicit, verifiable bridge (hash the
last line of `.1` and compare). The console audit tail (`/api/audit`)
spans both generations. The logsearch mount convention is unchanged: mount
the root; search `.audit.jsonl` AND `.audit.jsonl.1`.

**Philosophy: governance that cannot be searched is decoration.** The
audit trail is written so the fleet's log server can query it — that is
why rotation keeps the old generation visible and bridged, not deleted.

---

## Lazy workspace listing (deep flag)

`workspace_list` gains `deep: bool = false`. The default is LAZY: workspace
names only, with `files: null` / `bytes: null` and
`"counts": "lazy (pass deep=true)"` — no recursive walk, so the listing
stays O(#workspaces) even on an NFS-backed PVC holding hundreds of
thousands of inodes (counting rglob+stats the ENTIRE tree on every call).
`deep=true` preserves the exact pre-lazy behavior (per-workspace file count
and total bytes). Response keys are present in BOTH modes (values may be
null). The console's `GET /api/workspaces` keeps the deep listing so the UI
is unchanged.

**Philosophy: the listing that hangs is the one nobody runs.** Counting a
shared NFS tree is O(inodes); agents need names, not sizes.

---

## Web console

With `WORKBENCH_UI_ENABLED` (chart `webui.enabled`, default true) the same
container serves a self-contained HPE-branded console at `/` (no CDN, no
build step — corporate-proxy safe): workspace switcher, file tree +
viewer/saver, per-workspace env table editor, a run-command console (argv
input with the allowlist hint, stdout/stderr/exit/duration), and an audit
tail. The `/api/*` endpoints call the **same core functions the MCP tools
use** — path confinement, caps, the argv allowlist and the JSONL audit all
still apply; the UI gets no new powers.

**Auth posture** (fleet K8S-MCP-console pattern): the console HTML at `/`
and `/ui` is **public-but-inert** — an unlock bar in the page collects the
key, because the browser cannot load the page behind a 401 — while every
`/api/*` data route (and `/mcp`) stays API-key-gated at the pod; the
endpoint must also sit behind gateway authn (PCAI Istio gateway). Before
the W7 fix the key middleware covered the console HTML too, so a keyed
deployment served the browser raw 401 JSON at `/` and the console was
unreachable (caught live during deploy observation).

**Philosophy: one enforcement point.** Like every fleet console, the UI
calls the tool code — the browser surface adds convenience, not
guardrails.

---

## Self-metrics (GET /metrics — default OFF)

`WORKBENCH_METRICS_ENABLED=true` (chart `metrics.enabled`) serves
`GET /metrics`: `workbench_mcp_tool_requests_total{tool,outcome}` (per-tool
call counts, ok/error; nothing else is exported). Chart-gated default-OFF
(`metrics.enabled: false` renders no env and no ServiceMonitor — the
default pod has no `/metrics` route); when on, `/metrics` is key-free like
the probes. `helm/values.yaml` also documents the fleet audit-JSONL
searchability convention for `<root>/.audit.jsonl`.

---

## API-key auth — MANDATORY (fleet pattern)

`run_command` is arbitrary process execution, so **every HTTP route except
`/health`/`/healthz` requires an API key** (`X-API-Key` or
`Authorization: Bearer`; the bundled console asks for it once and stores it
in sessionStorage). **The chart never creates the key Secret** — it is
pre-deployed out-of-band and referenced by `apiKey.existingSecret` (default
name `workbench-mcp-apikey`, key `api-keys`); see DEPLOYMENT.md for the
failure mode. Keys are a comma-separated list (`api-keys=new,old`) — that
is the rotation mechanism: append the new key, move clients over, drop the
old; the server re-reads the env per request, so no restart is needed. The
fleet-universal `MCP_API_KEYS` env is honored too (either var works).

---

## Transport security (Host-header pinning — K8S-MCP fleet pattern)

The MCP endpoint's DNS-rebinding protection comes from the fleet-shared
`mcp_auth.transport_security_from_env()`. With `MCP_HOSTNAME` set (chart:
`ezua.virtualService.endpoint`, wired only when `ezua.enabled`), protection
is explicitly ON for that FQDN + loopback, with the https-only
browser-form Origin allowlist; `MCP_EXTRA_ALLOWED_HOSTS` (chart:
`extraAllowedHosts`) adds in-cluster service-DNS hosts for callers like an
MCP relay. The chart AUTO-prepends this release's own service DNS
(`<deployment.name>-service.<ns>.svc.cluster.local:*`) as the FIRST
`MCP_EXTRA_ALLOWED_HOSTS` entry whenever transport security is active —
the gateway's relay hop arrives with exactly that Host header (confirmed
in the workbench pod logs: "Invalid Host header: workbench-mcp-service.
workbench-mcp.svc.cluster.local:9103"), and without the entry every relay
hop gets 421 Misdirected Request — so sites list only EXTRA hosts in
`extraAllowedHosts` (setting it never drops the own-svc entry). With
neither set (local dev), the helper returns None and the SDK's implicit
default applies — on a loopback-bound dev server that is the SDK's
loopback-only auto-protection, so practical dev behavior is unchanged.
