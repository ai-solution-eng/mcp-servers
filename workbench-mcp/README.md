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
- Unchanged on purpose: `workspace_list` still shows the names/sizes of all
  workspaces (workspace discovery), and commands can still read pod paths
  outside the workbench root (`/etc/hosts`, ... — the rootfs is read-only
  and mounts no secrets).

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
| `WORKBENCH_EXEC_ALLOWLIST` | python3, pip, ls, cat, grep, git, ... | argv[0] allowlist (resolved against the server PATH, not a workspace PATH) |
| `WORKBENCH_EXEC_DENYLIST` | curl,wget,sudo,su,nc,ssh,... | hard deny (wins) |
| `WORKBENCH_EXEC_TIMEOUT_DEFAULT/MAX` | 60 / 600 s | per-command bounds |
| `WORKBENCH_MAX_FILE_BYTES` | 8 MiB | write cap |
| `WORKBENCH_MAX_OUTPUT_BYTES` | 200 KiB | stdout/stderr cap |
| `WORKBENCH_UI_ENABLED` | `true` | mount the web UI + `/api/*` routes |
| `WORKBENCH_TEMPLATES` | *(unset)* | JSON object of named workspace templates for `workspace_create(name, template=...)` — `{name: {description, extra_allowed, canned_setup}}`. **Opt-in**: unset/empty = the parameter is refused and behavior is byte-identical to the pre-template server. A template can only WIDEN the workspace's exec allowlist with these operator-supplied bare binary names (denylist still wins) and pre-run its canned setup argv commands inside the new workspace (same guards, audited with the template name). Chart: `workbench.templates` (rendered only when non-empty). See "Workspace templates". |
| `WORKBENCH_METRICS_ENABLED` | `false` | serve `GET /metrics` — Prometheus self-metrics: `workbench_mcp_tool_requests_total{tool,outcome}` (per-tool call counts, ok/error; nothing else is exported). Chart-gated default-OFF (`metrics.enabled: false` renders no env and no ServiceMonitor — the default pod has no `/metrics` route); when on, `/metrics` is key-free like the probes. See `helm/values.yaml` (which also documents the fleet audit-JSONL searchability convention for `<root>/.audit.jsonl`). |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` | — | passed through to `run_command` children (chart: `hpe_proxies`) so `pip` works behind the corporate proxy |
| `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`PIP_CERT` | — | corporate MITM CA for pip/requests (chart: `caCert` → `ezaf-root-ca`) |

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
