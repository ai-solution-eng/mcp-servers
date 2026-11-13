# Workbench MCP Server

Persistent, per-agent **scratch workspaces** with a governed command
runner, over MCP — chart `workbench-mcp` 0.4.0 for HPE Private Cloud AI
(PCAI / Ezmeral Unified Analytics): the durable layer a stateless shell
does not give the model.

## What it is

workbench-mcp is an **MCP server** — a Model Context Protocol endpoint that
exposes tools to LLM agents — that gives each agent a named directory on a
persistent volume (it survives pod restarts and replicas) plus a
tightly-governed way to run commands and edit files in it. **The workbench
pod is a scratch pad by design**: it mounts no secrets, runs non-root (uid
10001) with a read-only rootfs, automounts no service-account token, and
can only touch one volume. `run_command` is intentional RCE on that scratch
pad — every guard in the docs exists because of it: an argv allowlist (no
shells, no interpreters by default — fleet decision D18), a hard denylist,
timeouts, output caps, argv-level workspace isolation (D7), and a JSONL
audit log.

## The problem it solves

- **Your harness shells are stateless between calls** — platform temp
  areas may not survive, env vars vanish, background processes die.
  `workspace_create` gives the agent a PVC-backed directory that survives
  everything; `write_file`/`read_file`/`list_files`/`delete_file` are its
  durable memory.
- **The model needs real commands** (`git`, `grep`, `tar`), not simulated
  ones. `run_command` runs argv lists with cwd = the workspace root, a
  binary allowlist resolved against the SERVER's PATH (a workspace PATH
  override cannot redirect a name to a look-alike binary), per-command
  timeouts (60/600 s) and output caps (200 KiB).
- **One agent's scratch must not leak into another's.** Since D7, every
  operation resolves against the addressed workspace only — argv screening
  refuses path tokens that reach other workspaces or the audit JSONL;
  `WORKBENCH_SHARED_PATHS` is the explicit operator escape hatch.
- **Agents need python, but an in-pod interpreter breaks confinement.**
  D18 ships no interpreter; `sandbox_run` runs a python file in a dedicated
  executor pool instead — no network, no credentials, no GPU, deny-all
  netpol, stateless.
- **Governance you can prove.** Every audited event lands in
  `<root>/.audit.jsonl` (rotating at 100 MiB with an explicit sha256
  bridge across generations) — searchable by the fleet's logsearch server
  per the audit-JSONL convention.
- **Humans need the same surface without an MCP client.** The bundled
  console at `/` (workspace switcher, file tree, env editor, run-command
  console, audit tail) calls the same core functions — the UI gets no new
  powers.

## How it fits in PCAI

Deployed as chart `workbench-mcp` in its own namespace, exposed through
the PCAI Istio gateway (`ezaf-gateway`) next to the rest of the MCP fleet:

    browser / agents ──► ezaf-gateway (Istio) ──► workbench-mcp :9103
                                                     │  argv-governed exec
                                                     ▼
                     PVC (workspaces, RWX)  +  audit JSONL on pod logs

- **Depends on:** a bound PVC (`persistence.*`, RWX for multi-replica), the
  API-key Secret you pre-create, the Istio gateway, and — only for
  `sandbox_run` — the executor pool the same chart renders
  (`executors.enabled`).
- **Depends on it:** agent harnesses (the LLM gateway relays MCP calls),
  humans via the console, and — by convention — logsearch: the audit JSONL
  is written so the log server can search it. The fleet convention also
  shares a directory with logsearch's exports via `WORKBENCH_SHARED_PATHS`.
- **Pairing:** workbench is where logsearch's `export_matches` output lands
  so agents can read big search results back as files.

## Capabilities, by theme

**Workspace lifecycle** — `workspace_create` / `workspace_list` /
`workspace_delete` on a PVC-backed root; optional operator-defined
**templates** (opt-in) can widen one workspace's exec allowlist with bare
binary names and pre-run canned setup commands through the exact
`run_command` machinery — denylist always wins, and only the template NAME
persists, so a workspace is never wider than the operator's current config. →
[FEATURES.md#workspace-templates-wave-5-f3--opt-in](documentation/FEATURES.md#workspace-templates-wave-5-f3--opt-in)

**File and env tools** — path-confined (traversal and symlink escapes
refused), streaming `read_file` (a multi-GB file no longer OOMs the pod for
a small read), `write_file` with honest chunked appends (the 8 MiB cap
applies cumulatively — an append is a chunking path, never a bypass),
per-workspace env that `run_command` injects. →
[FEATURES.md#file-tools-streaming-reads-chunked-appends](documentation/FEATURES.md#file-tools-streaming-reads-chunked-appends)

**`run_command` — governed execution** — argv-list only (no shell), D18
narrow default allowlist (no interpreters/package managers — the refusal
tells the model why), hard denylist that always wins, PATH-erosion defense,
a dedicated 4-worker pool with a structured busy response (no silent
queueing), timeouts and output caps. →
[FEATURES.md#exec-policy--argv-allowlist-no-interpreters-by-default-fleet-decision-d18](documentation/FEATURES.md#exec-policy--argv-allowlist-no-interpreters-by-default-fleet-decision-d18)

**Workspace isolation (D7)** — every operation resolves against the
addressed workspace only; `run_command` screens its argv for escape paths;
the honest limit (an interpreter can still compute paths) is documented,
and the pod's real defense-in-depth does not depend on the screen. →
[FEATURES.md#workspace-isolation-fleet-decision-d7--default-change](documentation/FEATURES.md#workspace-isolation-fleet-decision-d7--default-change)

**Sandbox exec (v1, opt-in)** — `sandbox_run` executes a python file from
the workspace in a dedicated executor pool: same image, sleep command,
deny-all NetworkPolicy, no SA token, no GPU keys, control-plane affinity
exclusion, stateless runs; runc-tier isolation, honestly stated. Two gates:
`executors.enabled` AND `workbench.sandboxExec`. →
[FEATURES.md#sandbox-exec-v1-opt-in](documentation/FEATURES.md#sandbox-exec-v1-opt-in)

**Audit JSONL with rotation** — every event appended to
`<root>/.audit.jsonl`, rotated at 100 MiB into `.audit.jsonl.1` with a
`rotated_from` sha256 bridge (the writer is a plain appender, not the
hash-chained fleet writer — said plainly); logsearch can search both
generations. →
[FEATURES.md#audit-jsonl-and-its-rotation](documentation/FEATURES.md#audit-jsonl-and-its-rotation)

**Web console** — public-but-inert HTML with an in-page unlock bar (the
browser cannot load a page behind a 401 — the W7 fix caught live); every
`/api/*` data route and `/mcp` stays API-key-gated, and the console calls
the same core functions. →
[FEATURES.md#web-console](documentation/FEATURES.md#web-console)

**API-key auth — mandatory** — `run_command` is RCE, so every route except
`/health`/`/healthz` requires a key; the chart never creates the Secret
(comma-list rotation, env re-read per request). →
[FEATURES.md#api-key-auth--mandatory-fleet-pattern](documentation/FEATURES.md#api-key-auth--mandatory-fleet-pattern)

## Deploy on PCAI (one screen)

1. **Prerequisites:** create the API-key Secret (mandatory) and confirm the
   StorageClass story (RWX for multi-replica, or RWO + `replicaCount: 1`)
   — DEPLOYMENT.md §2.
2. **Import the chart once** into the PCAI catalog.
3. **Create the deployment** from the catalog entry and open the Helm
   Values editor.
4. **Paste values** — start from `helm/values-examples/values.g2.yaml` or
   `values.hosted-trial.yaml` (full documents), adjust every `# SITE:`
   line, keep `${DOMAIN_NAME}` as-is; decide the exec-policy posture here
   (D18-narrow default).
5. **Apply and verify:** `/healthz` answers; an unkeyed `/api/*` call
   answers 401 and a keyed call answers; create a workspace and write/read
   a file through it; MCP handshake per
   [documentation/VERIFICATION.md](documentation/VERIFICATION.md).

Full procedure — Secrets, PVC/StorageClass, executor pool, values,
troubleshooting: [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md).

## Prerequisites at a glance

| Prerequisite | Purpose | What breaks without it |
|---|---|---|
| API-key Secret (`workbench-mcp-apikey`, key `api-keys`) | Key every route except `/health`/`/healthz` — `run_command` is RCE | Pod `CreateContainerConfigError` |
| StorageClass + bound PVC (`persistence.*`, default 10Gi RWX at `/data`) | The workspaces ARE the product | PVC Pending or stranded replicas; no root to serve |
| `fsGroup: 10001` (chart default) | PVC group-writable by the server user | Pod starts, every write fails EACCES |
| Istio gateway + `ezua.virtualService.endpoint` (values) | The product path for external MCP clients + console | ClusterIP-only (`ezua.enabled: false`); unresolved `${DOMAIN_NAME}` matches nothing |
| Executor pool (opt-in: `executors.enabled` + `workbench.sandboxExec`) | The D18-legal python path (`sandbox_run`) | Tool refuses self-describingly (default); Role absent → exec 403 with the `can-i` recipe |
| `WORKBENCH_SHARED_PATHS` (env, only for cross-workspace sharing) | The D7 escape hatch for e.g. logsearch exports | Commands outside a workspace are refused (working as designed) |
| `hpe-ezua` vendor labels (opt-in Kyverno policy) | PCAI platform recognition | Cosmetic; off by default |

## Day-2

- **Upgrade** — import the newer chart, re-apply your values (DEPLOYMENT.md
  §5); workspaces survive the bump on the PVC (shrinking a PVC is not
  supported).
- **Health** — `GET /health`, `/healthz`; the console's audit tail doubles
  as a liveness-of-governance check.
- **Rotate the API key** — append to `api-keys=new,old` in the Secret; env
  re-reads per request, no restart.
- **Watch the audit trail** — `kubectl -n <ns> logs` or the console; rotate
  threshold is `workbench.auditMaxBytes` (100 MiB).
- **Uninstall** — delete the deployment from the PCAI UI; the PVC and its
  workspaces, and the Secret, survive until explicitly deleted.
- **Troubleshooting** — symptom → cause → fix table:
  [DEPLOYMENT.md §6](documentation/DEPLOYMENT.md).

## Documentation map

| File | The question it answers |
|---|---|
| [documentation/FEATURES.md](documentation/FEATURES.md) | What can it do, and why is each thing designed that way (D7, D18, sandbox tier)? |
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | How do I deploy, upgrade, and debug it the PCAI way — Secret, PVC, exec policy, values? |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | How do I prove the deployment works? |
| [helm/values-examples/README.md](helm/values-examples/README.md) | Which paste-ready values file matches my target, and what is in it? |
| [helm/values.yaml](helm/values.yaml) | Every chart key with its default and inline rationale |

## Developing locally

**Not the deployment path.** The server runs from the package tree:

```sh
# stdio (local MCP clients)
python -m workbench_mcp.server --transport stdio

# stateless streamable-http (fleet default), port 9103
python -m workbench_mcp.server --transport streamable-http --host 0.0.0.0 --port 9103
```

Health: `GET /health` / `/healthz`. Tests run offline (tmp dirs only, no
cluster, no network): `python -m pytest tests/ -v`. Transport security with
neither env set keeps the SDK's implicit loopback-only protection — dev
behavior unchanged.
