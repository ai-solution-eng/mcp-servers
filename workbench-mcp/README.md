# Workbench MCP Server

Persistent, per-agent **scratch workspaces** for baseline agent harnesses
(opencode, DSH): the durable layer a stateless shell does not give the model.

## Why

Harness shells are stateless between calls — platform temp areas may not
survive, env vars vanish, background processes die.  A workbench is a named
directory on a persistent volume plus a governed command runner:

- `workspace_create` / `workspace_list` / `workspace_delete` — PVC-backed
  dirs that survive pod restarts and work across replicas (RWX volume).
- `write_file` / `read_file` / `list_files` / `delete_file` — path-confined
  file access (traversal and symlink escapes refused by design).
- `set_env` / `get_env` — per-workspace env vars that `run_command` injects.
- `run_command` — argv-list execution (no shell) with a binary allowlist /
  denylist, timeouts, output caps, and a JSONL audit log.

## Trust model

The workbench pod is a **scratch pad by design**: it mounts no secrets, runs
non-root (uid 10001), and can only touch one volume.  `run_command` is
intentional RCE on that scratch pad — governed by
`WORKBENCH_EXEC_ALLOWLIST` / `WORKBENCH_EXEC_DENYLIST`
(`curl`, `wget`, `sudo`, `ssh`, ... denied by default), timeouts
(`WORKBENCH_EXEC_TIMEOUT_MAX`, default 600s), output caps, and audit.

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `WORKBENCH_ROOT` | `/data` | PVC mount holding all workspaces |
| `WORKBENCH_EXEC_ALLOWLIST` | python3, pip, ls, cat, grep, git, ... | argv[0] allowlist |
| `WORKBENCH_EXEC_DENYLIST` | curl,wget,sudo,su,nc,ssh,... | hard deny (wins) |
| `WORKBENCH_EXEC_TIMEOUT_DEFAULT/MAX` | 60 / 600 s | per-command bounds |
| `WORKBENCH_MAX_FILE_BYTES` | 8 MiB | write cap |
| `WORKBENCH_MAX_OUTPUT_BYTES` | 200 KiB | stdout/stderr cap |
| `WORKBENCH_UI_ENABLED` | `true` | mount the web UI + `/api/*` routes |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` | — | passed through to `run_command` children (chart: `hpe_proxies`) so `pip` works behind the corporate proxy |
| `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`PIP_CERT` | — | corporate MITM CA for pip/requests (chart: `caCert` → `ezaf-root-ca`) |

## Web UI

With `WORKBENCH_UI_ENABLED` (chart `webui.enabled`, default true) the same
container serves a self-contained HPE-branded console at `/` (no CDN, no
build step — corporate-proxy safe): workspace switcher, file tree +
viewer/saver, per-workspace env table editor, a run-command console
(argv input with the allowlist hint, stdout/stderr/exit/duration), and an
audit tail. The `/api/*` endpoints call the **same core functions the MCP
tools use** — path confinement, caps, the argv allowlist and the JSONL
audit all still apply; the UI gets no new powers. It is unauthenticated at
the pod: the endpoint must sit behind gateway authn (PCAI Istio gateway).

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
