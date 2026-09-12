# workbench-mcp

workbench-mcp is a persistent, per-agent **scratch workspace** MCP (Model
Context Protocol) server: it gives an agent named directories on a persistent
volume (they survive pod restarts), path-confined file read/write/list/delete
inside each workspace, a per-workspace environment-variable store, and a
governed argv-list command runner with a binary allowlist/denylist, timeouts,
output caps, and a JSONL audit trail. It is the durable layer a stateless
harness shell does not give the model, exposed over MCP 2.0 (stateless
streamable-HTTP at `/mcp`) through the PCAI Istio gateway.

**What problem(s) it solves**

- Baseline agent harnesses (opencode, DSH) give the model a stateless shell:
  each call is a fresh process, platform temp areas may not survive between
  calls, env vars vanish, background processes die. A workbench is the
  missing durable layer — files, env, and command state persist across calls
  and across pod restarts.
- Scratch work needs a home per task/agent: `workspace_create` mints an
  isolated directory on the PVC; `run_command` executes with the workspace as
  cwd, so tools like `git`, `pip`, and `tar` operate on exactly that state.
- Command execution from a model is dangerous by default: `run_command` is
  argv-only (no shell interpolation), argv[0] must be on an operator
  allowlist, a hard denylist (`curl`, `wget`, `sudo`, `su`, `nc`, `ssh`, ...)
  always wins, commands are time-bounded and output-capped, and every
  mutating call is audit-logged.
- Path escapes: file tools refuse absolute paths, `..` traversal, and symlink
  escapes by design — a workspace cannot read or write outside itself.
- Persistent storage with the right shape: an RWX PVC so every replica sees
  the same workspaces (MCP is stateless — any replica serves any request);
  the pod mounts no secrets, runs non-root, and touches only that volume.

## Tools

All tool results are JSON strings; errors are self-describing and surfaced
verbatim to the model.

| Tool | Mutates | Purpose |
|---|---|---|
| `workspace_create` | yes | Create a persistent named workspace (directory under the PVC root); the container for everything else. |
| `workspace_list` | no | List workspaces with file counts and total bytes. |
| `workspace_delete` | yes | Delete a workspace and everything in it — requires `confirm=true`. |
| `write_file` | yes | Write a UTF-8 text file (parents auto-created); workspace-relative paths only, 8 MiB cap. |
| `read_file` | no | Read a UTF-8 text file, capped at `max_bytes` (default 64 KiB). |
| `list_files` | no | List files/dirs under a workspace path (recursive, bounded at 500 entries). |
| `delete_file` | yes | Delete a file or directory inside the workspace — requires `confirm=true`. |
| `set_env` | yes | Persist an env var for the workspace (`run_command` injects it; its entries win over pod passthrough). |
| `get_env` | no | Read one persisted env var. |
| `run_command` | yes | Run an argv command in the workspace (cwd = workspace root): allowlisted argv[0], denylist wins, timeout bounds, capped output, JSONL-audited. |

## Architecture

A single Python service (Starlette, MCP 2.0 stateless, JSON responses) whose
backend is the **filesystem**: one PVC mounted at `/data` (the
`WORKBENCH_ROOT`) holding every workspace, plus bounded subprocesses spawned
inside it. It talks to **no Kubernetes API and no other service** — no RBAC,
no ServiceAccount grants. Operator-configured proxy/TLS-trust env
(`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`, `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/
`PIP_CERT`) is passed through to `run_command` children so `pip` works behind
a corporate proxy — those names come from the deployment, not the caller, so
passing them through leaks nothing. HTTP surface: `/mcp` (MCP
streamable-HTTP), `/health` + `/healthz`, and — when `webui.enabled` — the
HPE-branded console at `/` (workspace switcher, file tree, env editor,
run-command console, audit tail) whose `/api/*` endpoints call the same core
functions the MCP tools use.

## Deploy on PCAI (HPE Private Cloud AI)

Import the packaged chart once into PCAI, then edit the chart's values in the
PCAI **Helm Values** editor and apply — you never run `helm install` or
`kubectl apply` for the deployment itself. Every `helm --set a.b=c`
corresponds 1:1 to a values key. PCAI resolves `${DOMAIN_NAME}` in the
editor on current builds; if your build does not, substitute the literal
cluster domain (an unresolved placeholder registers a gateway host that
matches nothing).

**Required values**:

```yaml
ezua:
  enabled: true                      # SITE: expose through the PCAI Istio gateway
  domainName: <your-domain>          # SITE: literal cluster domain
  virtualService:
    endpoint: workbench-mcp.<your-domain>   # SITE: /mcp -> MCP server; / -> web UI
    istioGateway: istio-system/ezaf-gateway
    timeout: 660s                    # generous: run_command may legitimately run to its max
```

Everything else has working defaults — `persistence` (10Gi RWX PVC, the
point of the server), `workbench.*` (root, allow/denylists, caps), `webui.enabled`,
`resources`, `securityContext`, `hpe_proxies`/`proxy.*`/`caCert` (needed when
the cluster sits behind the HPE corporate proxy and `pip` inside
`run_command` must reach PyPI), `kyverno.enabled`. Complete paste-ready
documents:
[helm/values-examples/values.g2.yaml](helm/values-examples/values.g2.yaml)
and
[helm/values-examples/values.hosted-trial.yaml](helm/values-examples/values.hosted-trial.yaml).

## Connect an MCP client

Any MCP client that speaks streamable-HTTP connects to `/mcp` (stateless —
no session header needed); humans use the console at `/`:

```json
{
  "mcpServers": {
    "workbench-mcp": {
      "url": "https://workbench-mcp.<your-domain>/mcp"
    }
  }
}
```

Clients that want the transport spelled out accept `"type": "http"`
(Claude Code / Claude Desktop) or `"transport": "streamable-http"` (DSH
profile, opencode). In-cluster consumers can use the service DNS instead:
`http://workbench-mcp-service.<namespace>.svc.cluster.local:9103/mcp`. The
web UI is read/write and unauthenticated at the pod — the endpoint must sit
behind gateway authn (the PCAI Istio gateway); don't expose the root route
without it.

## Documentation

| Document | Contents |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Values walkthrough (required vs optional), persistence, exec allowlist governance, proxy/CA wiring, ezua/Istio gateway, upgrading |
| [documentation/VERIFICATION.md](documentation/VERIFICATION.md) | MCP handshake + first tool test, optional operator kubectl checks, troubleshooting |
| [helm/values-examples/README.md](helm/values-examples/README.md) | What the example values files are, how to use them (PCAI editor or `helm -f`) |
